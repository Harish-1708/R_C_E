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

from typing import Dict, List, Optional, Protocol

from gspread.utils import rowcol_to_a1


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


def sync_tab(
    client: SheetsClient,
    known_columns: List[str],
    target_rows: Dict[str, dict],
    id_col: str = "id",
    sort_key: Optional[str] = None,
) -> dict:
    """Rewrite one tab from `target_rows` (id -> row dict, using
    `known_columns` as keys), preserving any extra manual columns already
    present in the sheet.

    Returns a small summary dict, useful for logging:
        {"rows_written": int, "extra_columns_preserved": [str, ...]}
    """
    existing = client.read_all()
    existing_header = existing[0] if existing else list(known_columns)
    existing_data = existing[1:] if len(existing) > 1 else []

    extra_columns = [c for c in existing_header if c not in known_columns]
    existing_by_id = _index_by_id(existing_header, existing_data, id_col) if extra_columns else {}

    final_header = list(known_columns) + extra_columns

    ids = list(target_rows.keys())
    if sort_key:
        ids.sort(key=lambda i: target_rows[i].get(sort_key, ""))
    else:
        ids.sort()

    out_rows = [final_header]
    for row_id in ids:
        row = target_rows[row_id]
        line = [str(row.get(col, "")) for col in known_columns]
        if extra_columns:
            preserved = existing_by_id.get(row_id, {})
            line += [str(preserved.get(col, "")) for col in extra_columns]
        out_rows.append(line)

    client.overwrite_all(out_rows)

    return {
        "rows_written": len(ids),
        "extra_columns_preserved": extra_columns,
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


def get_or_create_worksheet(spreadsheet, title: str, cols: int = 30):
    """Fetch a worksheet by title, creating it (blank) if missing. Used
    for the one-time "create the sheet from scratch" setup step."""
    try:
        return spreadsheet.worksheet(title)
    except Exception:
        return spreadsheet.add_worksheet(title=title, rows=1000, cols=cols)
