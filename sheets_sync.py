"""
sheets_sync.py

Pushes the row-sets produced by parse_refunnel.py into Google Sheets.

Sync strategy: full rewrite of the "known" columns per tab, every run.
Each run already recomputes the complete correct target state from
Refunnel's own CSVs (parse_refunnel.py), so rewriting the tab from that
target state is simpler and safer than trying to diff/patch individual
cells -- there's no path for a half-applied update or a stale leftover
row from a status that changed (e.g. a post moving from Requested to
Approved is just: not in this run's Requested set, is in this run's
Approved set -- the rewrite makes that correct automatically).

The one thing a wholesale rewrite must NOT destroy: if Harish (or anyone)
adds an extra manual column in the sheet -- e.g. "Notes" or "Assigned to"
-- that column is not something Refunnel or this script knows about, but
it should survive the next sync. So before rewriting, we read the
existing header + rows, detect any columns beyond our known schema, and
carry those values forward keyed by id.

This module is written against a small SheetsClient protocol rather than
gspread directly, so the sync logic can be fully unit-tested with an
in-memory fake (see test_sheets_sync.py) without needing live Google
credentials. GspreadSheetsClient below is the real implementation --
it can't be exercised in this sandbox (no live Sheets access), so it
should get a manual smoke test once real credentials are available.
"""

from __future__ import annotations

import re
import time
from typing import Dict, List, Optional, Protocol

from gspread.utils import rowcol_to_a1
import gspread.exceptions

# Confirmed real: a scheduled run failed entirely on a single 503 from
# Google's own API, at the very first step (connecting to the
# spreadsheet) -- completely unrelated to Refunnel, which was
# confirmed working fine at the time. gspread has no built-in retry for
# this, and Google's APIs are known to have occasional brief hiccups
# that usually resolve within seconds -- retrying a few times with a
# short backoff is the standard fix, not something to just accept as
# "the run fails sometimes".
#
# Matched by STRING, not by inspecting gspread's internal exception
# object structure -- confirmed real, exact format from a live failure:
# "APIError: [503]: The service is currently unavailable." String
# matching on that confirmed format is safer than guessing at
# gspread's internal APIError attributes without being able to verify
# them against a live install.
_TRANSIENT_ERROR_MARKERS = ("[429]", "[500]", "[502]", "[503]", "[504]")


def is_transient_gspread_error(exception: Exception) -> bool:
    """True if this looks like a temporary Google-side hiccup worth
    retrying (rate limit or a 5xx server error), not a real, permanent
    problem (bad credentials, sheet doesn't exist, a genuine bug) that
    retrying would never fix."""
    return any(marker in str(exception) for marker in _TRANSIENT_ERROR_MARKERS)


def retry_on_transient_error(func, *args, max_attempts: int = 5, initial_delay_seconds: float = 2.0, **kwargs):
    """Calls func(*args, **kwargs), retrying with exponential backoff
    (2s, 4s, 8s, 16s by default) if it fails with a transient-looking
    Google API error. A non-transient error (bad credentials, sheet not
    found, a real bug) is NOT retried -- it raises immediately, since
    waiting wouldn't help and would just delay a real failure for no
    reason. Used to wrap every real Google Sheets API call in this
    project (opening a spreadsheet, reading/writing a tab, etc.) so a
    single transient blip doesn't kill an entire run.
    """
    delay = initial_delay_seconds
    for attempt in range(1, max_attempts + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if not is_transient_gspread_error(e) or attempt == max_attempts:
                raise
            print(f"Transient Google Sheets API error (attempt {attempt}/{max_attempts}): "
                  f"{type(e).__name__}: {e}. Retrying in {delay:.0f}s...")
            time.sleep(delay)
            delay *= 2


class SheetsClient(Protocol):
    """Minimal interface a tab-writer needs. One implementation per tab
    (or one client scoped to a (spreadsheet_id, tab_name) pair)."""

    def read_all(self) -> List[List[str]]:
        """Return all rows including the header row, as raw strings.
        Empty list (or a list with just a header) if the tab is empty."""
        ...

    def overwrite_all(self, rows: List[List[str]]) -> None:
        """Replace the tab's entire contents with `rows` (header + data),
        clearing anything currently there first."""
        ...


def _flatten_cell(value) -> str:
    """Stringify a cell value and collapse any embedded newlines to a
    single space. Confirmed real cause of "one row huge, next one
    normal" row heights: some fields (captions, in particular) contain
    genuine `\\n` characters from the original post's own line breaks --
    CLIP wrap strategy only stops Sheets from wrapping text that's too
    WIDE for the column, it does nothing for a cell whose value already
    contains real newlines, which forces multi-line rendering
    regardless. Stripping them here means every row is uniformly
    single-line, with CLIP handling the "too wide" case on top."""
    text = str(value) if value is not None else ""
    return re.sub(r"\s*[\r\n]+\s*", " ", text).strip()


def index_by_id(header: List[str], data_rows: List[List[str]], id_col: str) -> Dict[str, Dict[str, str]]:
    """Turn a sheet's raw (header, data_rows) into id -> {column: value}.
    Public -- used internally by sync_tab for extra-column/never_delete
    merging, and by other scripts (e.g. build_content_tracker.py) that
    need to read an existing tab's current state the same way.
    """
    if id_col not in header:
        return {}
    id_idx = header.index(id_col)
    out: Dict[str, Dict[str, str]] = {}
    for raw_row in data_rows:
        if id_idx >= len(raw_row):
            continue
        row_id = raw_row[id_idx]
        if not row_id:
            continue
        row_dict = {header[i]: (raw_row[i] if i < len(raw_row) else "") for i in range(len(header))}
        out[row_id] = row_dict
    return out


def read_column_values(client: SheetsClient, column_name: str, id_col: str = "id") -> Dict[str, str]:
    """Read one column's current values from a sheet, keyed by id.

    Used for reading back a MANUAL column that isn't part of our own
    schema (e.g. a 'Reviewed' flag you type into Master Data yourself)
    so its values can actually influence this run's logic -- not just
    be preserved as inert extra data the way sync_tab()'s own
    extra-column preservation does.

    Returns {} if the sheet is empty or doesn't have that column yet
    (e.g. you haven't added it, or it's the very first run) -- this is
    treated as "nothing marked yet", not an error.
    """
    existing = client.read_all()
    if not existing:
        return {}
    header = existing[0]
    if id_col not in header or column_name not in header:
        return {}
    id_idx = header.index(id_col)
    col_idx = header.index(column_name)
    out: Dict[str, str] = {}
    for raw_row in existing[1:]:
        if id_idx >= len(raw_row):
            continue
        row_id = raw_row[id_idx]
        if not row_id:
            continue
        out[row_id] = raw_row[col_idx] if col_idx < len(raw_row) else ""
    return out


class SuspiciousShrinkError(RuntimeError):
    pass


def sync_tab(
    client: SheetsClient,
    known_columns: List[str],
    target_rows: Dict[str, dict],
    id_col: str = "id",
    sort_key: Optional[str] = None,
    sort_reverse: bool = False,
    max_shrink_fraction: Optional[float] = None,
    never_delete: bool = False,
    preserve_columns: Optional[List[str]] = None,
) -> dict:
    """Rewrite one tab from `target_rows` (id -> row dict, using
    `known_columns` as keys), preserving any extra manual columns already
    present in the sheet.

    never_delete: for tabs that should ONLY ever gain rows or have
    existing ones updated -- never lose one (Master Data, Payments). If
    True, any id that's in the sheet already but NOT in this run's
    target_rows is carried forward unchanged rather than dropped. This
    is a stronger guarantee than max_shrink_fraction (which just refuses
    a suspiciously large drop) -- with never_delete, a row can only be
    removed by an explicit "forget this" action elsewhere, never by
    simply not appearing in one day's pull. Do NOT set this for tabs
    that are supposed to lose rows by design (Usage Rights tabs when a
    status changes; Human Review's source tabs when something's
    reviewed) -- those still need the normal full-rewrite behavior.

    max_shrink_fraction: opt-in safety net for tabs that should only
    ever grow or hold steady -- NOT for tabs that are expected to shrink
    by design (Usage Rights tabs lose rows when you mark them
    Reviewed; that's normal). Ignored when never_delete is True, since
    a shrink is already structurally impossible in that mode. If set,
    and the existing tab already has rows, and target_rows is smaller
    than existing by more than this fraction, raises
    SuspiciousShrinkError and does NOT touch the sheet at all. This
    exists because every tab here is a full rewrite -- an incomplete
    data pull (e.g. a browser scroll that stalled early) would
    otherwise silently delete real rows a previous, complete run had
    correctly written.

    sort_key / sort_reverse: which field to sort rows by, and whether to
    reverse it (e.g. sort_key="created_at", sort_reverse=True puts the
    newest posts first). Only safe for fields whose string form sorts
    the same as its real chronological/numeric order -- ISO-format
    timestamps like created_at/updated_at do; a human-formatted date
    like "Sep 4, 2026" does NOT (alphabetically "Dec" sorts before
    "Jan", which is backwards across a year boundary), so don't use
    sort_key for a field like that without parsing it into a sortable
    form first.

    Returns a small summary dict, useful for logging:
        {"rows_written": int, "extra_columns_preserved": [str, ...],
         "rows_carried_forward": int}
    """
    existing = client.read_all()
    existing_header = existing[0] if existing else list(known_columns)
    existing_data = existing[1:] if len(existing) > 1 else []

    rows_carried_forward = 0
    if never_delete:
        existing_by_id_full = index_by_id(existing_header, existing_data, id_col)
        merged = dict(target_rows)
        for row_id, old_row in existing_by_id_full.items():
            if row_id not in merged:
                merged[row_id] = {col: old_row.get(col, "") for col in known_columns}
                rows_carried_forward += 1
        target_rows = merged
    elif max_shrink_fraction is not None:
        existing_count = len(existing_data)
        new_count = len(target_rows)
        if existing_count > 0 and new_count < existing_count * (1 - max_shrink_fraction):
            raise SuspiciousShrinkError(
                f"Refusing to overwrite -- new row count ({new_count}) is more than "
                f"{max_shrink_fraction:.0%} smaller than what's already in the sheet "
                f"({existing_count}). This usually means an incomplete data pull, not "
                f"a real drop, so the sheet was NOT touched. If this shrink is genuinely "
                f"expected, call sync_tab without max_shrink_fraction for this tab, or "
                f"investigate why the real count dropped before re-running."
            )

    # Blank-named header cells are never treated as a manual column to
    # preserve -- confirmed real: renaming a known column (impressions
    # -> views, then reverted) left stray blank-header cells trailing
    # in a real sheet, which this same logic would otherwise carry
    # forward indefinitely as "extra columns". A real manual column
    # (like a "Notes" header you add yourself) always has a name.
    extra_columns = [c for c in existing_header if c not in known_columns and c.strip()]
    # preserve_columns: columns that sit in known_columns (so they get a
    # PROPER, stable position in the header) but whose values are
    # carried forward from the sheet rather than overwritten by
    # target_rows. Confirmed real need: "campaigns" had to be a real
    # Master column to be placed next to "collections" -- extra columns
    # are always appended at the END (see final_header below), and any
    # mid-sheet placement would be moved back there on the next sync.
    # But as a plain known column, every daily sync would overwrite it
    # with the CSV's blank value (the export has no campaign field),
    # wiping the freeze-once-set campaign data. Preserving it gets both.
    preserve = set(preserve_columns or [])
    need_existing = bool(extra_columns) or bool(preserve)
    existing_by_id = index_by_id(existing_header, existing_data, id_col) if need_existing else {}

    final_header = list(known_columns) + extra_columns

    ids = list(target_rows.keys())
    if sort_key:
        ids.sort(key=lambda i: target_rows[i].get(sort_key, ""), reverse=sort_reverse)
    else:
        ids.sort()

    out_rows = [final_header]
    for row_id in ids:
        row = target_rows[row_id]
        existing_row = existing_by_id.get(row_id, {})
        line = []
        for col in known_columns:
            if col in preserve:
                # the sheet's own value wins; target_rows only fills a blank
                value = existing_row.get(col, "") or row.get(col, "")
            else:
                value = row.get(col, "")
            line.append(_flatten_cell(value))
        if extra_columns:
            line += [_flatten_cell(existing_row.get(col, "")) for col in extra_columns]
        out_rows.append(line)

    client.overwrite_all(out_rows)

    return {
        "rows_written": len(ids),
        "extra_columns_preserved": extra_columns,
        "rows_carried_forward": rows_carried_forward,
    }


# ---------------------------------------------------------------------
# Real implementation. Not exercised in this sandbox -- needs a live
# service account with edit access to the target spreadsheet. Smoke-test
# manually once credentials exist: see README "Testing the Sheets layer".
# ---------------------------------------------------------------------

class GspreadSheetsClient:
    """Wraps a single gspread Worksheet to satisfy the SheetsClient protocol.

    Usage:
        import gspread
        gc = gspread.service_account(filename="service_account.json")
        sh = gc.open_by_key(SPREADSHEET_ID)
        ws = sh.worksheet("Master Data")
        client = GspreadSheetsClient(ws)
    """

    def __init__(self, worksheet):
        self._ws = worksheet

    def read_all(self) -> List[List[str]]:
        return retry_on_transient_error(self._ws.get_all_values)

    def overwrite_all(self, rows: List[List[str]]) -> None:
        retry_on_transient_error(self._ws.clear)
        if not rows:
            return
        # value_input_option="RAW" avoids Sheets trying to reinterpret
        # things like a caption that starts with "=" as a formula.
        retry_on_transient_error(self._ws.update, rows, value_input_option="RAW")

        # Uneven row heights (a long caption wrapping to several lines
        # right next to single-line rows) were flagged as hard to read.
        # CLIP keeps every row a single, consistent height -- the full
        # value is still there, just truncated visually until the
        # column's widened or the cell's opened.
        last_row = len(rows)
        last_col = len(rows[0]) if rows else 1
        full_range = f"A1:{rowcol_to_a1(last_row, last_col)}"
        retry_on_transient_error(self._ws.format, full_range, {"wrapStrategy": "CLIP"})

        # Bold header + frozen header row, so it stays visible on scroll
        # and reads clearly as a header rather than a data row.
        header_range = f"A1:{rowcol_to_a1(1, last_col)}"
        retry_on_transient_error(self._ws.format, header_range, {"textFormat": {"bold": True}})
        try:
            retry_on_transient_error(self._ws.freeze, rows=1)
        except Exception:
            pass  # cosmetic only -- never worth failing the whole sync over

    def _ensure_column(self, header: List[str], column_name: str) -> List[str]:
        """Appends column_name to the header if it isn't there yet, and
        returns the (possibly extended) header.

        CONFIRMED REAL, silent data-loss bug this replaces: both
        update_single_cell and update_cells_by_id used to just return
        "nothing written" when the target column didn't exist. A live
        campaign run found 59 posts across 5 campaigns and wrote ZERO
        of them -- the "campaigns" column had never been created, and
        nothing said so. The same flaw meant "drive_uploaded_at" could
        never be written on a sheet lacking it, so every Drive upload
        would stay unmarked and be re-uploaded as a duplicate on every
        single run.

        Opt-in via create_if_missing=True, NOT the default -- confirmed
        real, deliberate distinction: system-owned columns ("campaigns",
        "drive_uploaded_at") must be created on demand, but "Reviewed"
        is a MANUAL column you add yourself, and Content Tracker's
        propagation must NOT invent it on your behalf. The default
        no-create path now prints a loud warning instead of silently
        returning, so this class of bug can't hide again.

        Appended at the END, matching where sync_tab itself places extra
        columns, so the next sync_tab keeps it there rather than moving
        it. (A column meant to sit mid-sheet belongs in known_columns.)
        """
        if column_name in header:
            return header
        new_col = len(header) + 1
        col_count = getattr(self._ws, "col_count", None)
        if isinstance(col_count, int) and new_col > col_count:
            retry_on_transient_error(self._ws.add_cols, new_col - col_count)
        retry_on_transient_error(self._ws.update_cell, 1, new_col, column_name)
        print(f"Created missing '{column_name}' column in the sheet (it didn't exist yet).")
        return list(header) + [column_name]

    def update_single_cell(self, row_id: str, column_name: str, value: str, id_col: str = "id",
                           create_if_missing: bool = False) -> bool:
        """Update just one cell (column_name, for whichever row has
        row_id in id_col) WITHOUT rewriting the whole tab. Used for
        incremental updates during long-running steps like email
        scraping, so progress is saved as it happens rather than only
        at the very end -- if the run gets interrupted partway, emails
        already found aren't lost.

        Returns True if the row was found and updated, False if no row
        with that id exists in the sheet yet.
        """
        header = retry_on_transient_error(self._ws.row_values, 1)
        if id_col not in header:
            return False
        if column_name not in header:
            if not create_if_missing:
                # Never silent again -- see _ensure_column's docstring.
                print(f"WARNING: '{column_name}' column doesn't exist in this sheet, so nothing "
                      f"was written for {row_id!r}.")
                return False
            header = self._ensure_column(header, column_name)
        id_col_idx = header.index(id_col) + 1  # gspread is 1-indexed
        target_col_idx = header.index(column_name) + 1

        id_values = retry_on_transient_error(self._ws.col_values, id_col_idx)
        for row_num, val in enumerate(id_values[1:], start=2):  # skip header row
            if val == row_id:
                retry_on_transient_error(self._ws.update_cell, row_num, target_col_idx, value)
                return True
        return False

    def update_cells_by_id(self, updates: Dict[str, str], column_name: str, id_col: str = "id",
                           create_if_missing: bool = False) -> int:
        """Batch version of update_single_cell: writes ONE column across
        MANY rows, keyed by id, in a small, fixed number of API calls no
        matter how many rows are involved -- one read for the header,
        one read for the id column, and one write (gspread's
        batch_update, a single HTTP request for every changed cell).

        CONFIRMED REAL BUG this fixes: propagate_reviewed_to_master()
        called update_single_cell() once per reviewed row, and THAT
        re-read the entire header and id column from scratch on every
        single call -- 2 full-sheet reads per row, every run, forever,
        even for rows whose value hadn't changed since yesterday. With
        enough reviewed rows accumulated over time, a real run hit
        Google's 60-reads-per-minute quota and failed outright with a
        429. Callers should also skip ids whose value hasn't actually
        changed before calling this, so this only ever does real work.

        Returns how many cells were written (ids not found in the
        sheet are silently skipped, same as update_single_cell).
        """
        if not updates:
            return 0
        header = retry_on_transient_error(self._ws.row_values, 1)
        if id_col not in header:
            return 0
        if column_name not in header:
            if not create_if_missing:
                if updates:
                    print(f"WARNING: '{column_name}' column doesn't exist in this sheet, so "
                          f"{len(updates)} value(s) were NOT written.")
                return 0
            header = self._ensure_column(header, column_name)
        id_col_idx = header.index(id_col) + 1  # gspread is 1-indexed
        target_col_idx = header.index(column_name) + 1

        id_values = retry_on_transient_error(self._ws.col_values, id_col_idx)
        row_by_id = {val: row_num for row_num, val in enumerate(id_values[1:], start=2) if val}

        batch = []
        for media_id, value in updates.items():
            row_num = row_by_id.get(media_id)
            if row_num is not None:
                batch.append({"range": rowcol_to_a1(row_num, target_col_idx), "values": [[value]]})

        if batch:
            retry_on_transient_error(self._ws.batch_update, batch, value_input_option="RAW")
        return len(batch)


def get_or_create_worksheet(spreadsheet, title: str, cols: int = 30):
    """Fetch a worksheet by title, creating it (blank) if missing. Used
    for the one-time "create the sheet from scratch" setup step.

    Only catches WorksheetNotFound specifically -- confirmed from a real
    run that catching a bare Exception here is a real bug: a transient
    503 from Google's side (nothing to do with whether the sheet
    exists) was being misread as "doesn't exist yet", triggering an
    attempt to create a duplicate, which then failed for real because
    the sheet was there all along. Any other error (a genuine outage, a
    permissions problem, etc.) now propagates and fails the run
    honestly instead of masking itself as a confusing "already exists"
    error one level down.
    """
    try:
        return retry_on_transient_error(spreadsheet.worksheet, title)
    except gspread.exceptions.WorksheetNotFound:
        return retry_on_transient_error(spreadsheet.add_worksheet, title=title, rows=1000, cols=cols)
