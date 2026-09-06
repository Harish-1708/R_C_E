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
from typing import Dict, List, Optional, Protocol

from gspread.utils import rowcol_to_a1
import gspread.exceptions


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


def _index_by_id(header: List[str], data_rows: List[List[str]], id_col: str) -> Dict[str, Dict[str, str]]:
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
        existing_by_id_full = _index_by_id(existing_header, existing_data, id_col)
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
    existing_by_id = _index_by_id(existing_header, existing_data, id_col) if extra_columns else {}

    final_header = list(known_columns) + extra_columns

    ids = list(target_rows.keys())
    if sort_key:
        ids.sort(key=lambda i: target_rows[i].get(sort_key, ""), reverse=sort_reverse)
    else:
        ids.sort()

    out_rows = [final_header]
    for row_id in ids:
        row = target_rows[row_id]
        line = [_flatten_cell(row.get(col, "")) for col in known_columns]
        if extra_columns:
            preserved = existing_by_id.get(row_id, {})
            line += [_flatten_cell(preserved.get(col, "")) for col in extra_columns]
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
        return self._ws.get_all_values()

    def overwrite_all(self, rows: List[List[str]]) -> None:
        self._ws.clear()
        if not rows:
            return
        # value_input_option="RAW" avoids Sheets trying to reinterpret
        # things like a caption that starts with "=" as a formula.
        self._ws.update(rows, value_input_option="RAW")

        # Uneven row heights (a long caption wrapping to several lines
        # right next to single-line rows) were flagged as hard to read.
        # CLIP keeps every row a single, consistent height -- the full
        # value is still there, just truncated visually until the
        # column's widened or the cell's opened.
        last_row = len(rows)
        last_col = len(rows[0]) if rows else 1
        full_range = f"A1:{rowcol_to_a1(last_row, last_col)}"
        self._ws.format(full_range, {"wrapStrategy": "CLIP"})

        # Bold header + frozen header row, so it stays visible on scroll
        # and reads clearly as a header rather than a data row.
        header_range = f"A1:{rowcol_to_a1(1, last_col)}"
        self._ws.format(header_range, {"textFormat": {"bold": True}})
        try:
            self._ws.freeze(rows=1)
        except Exception:
            pass  # cosmetic only -- never worth failing the whole sync over

    def update_single_cell(self, row_id: str, column_name: str, value: str, id_col: str = "id") -> bool:
        """Update just one cell (column_name, for whichever row has
        row_id in id_col) WITHOUT rewriting the whole tab. Used for
        incremental updates during long-running steps like email
        scraping, so progress is saved as it happens rather than only
        at the very end -- if the run gets interrupted partway, emails
        already found aren't lost.

        Returns True if the row was found and updated, False if no row
        with that id exists in the sheet yet.
        """
        header = self._ws.row_values(1)
        if id_col not in header or column_name not in header:
            return False
        id_col_idx = header.index(id_col) + 1  # gspread is 1-indexed
        target_col_idx = header.index(column_name) + 1

        id_values = self._ws.col_values(id_col_idx)
        for row_num, val in enumerate(id_values[1:], start=2):  # skip header row
            if val == row_id:
                self._ws.update_cell(row_num, target_col_idx, value)
                return True
        return False


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
        return spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=1000, cols=cols)
