"""
build_preview_workbook.py

Runs the real parse_refunnel.py logic against your sample CSVs and writes
the result out as an actual .xlsx with the 5 tabs, so you can see exactly
what the data looks like before it's wired up to live Google Sheets.

This is a PREVIEW, not the production path -- the real system writes to
Google Sheets via sheets_sync.py + a service account, not to a local
xlsx. This script exists purely so you can visually check the tab
structure and column layout against real data right now.
"""

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

from parse_refunnel import (
    parse_media_csv,
    parse_payments_csv,
    build_human_review_rows,
    MASTER_COLUMNS,
    PAYMENT_COLUMNS,
    HUMAN_REVIEW_COLUMNS,
)

HEADER_FONT = Font(name="Arial", bold=True, color="FFFFFF")
HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
BODY_FONT = Font(name="Arial", size=10)


def write_tab(wb, title, columns, rows_dict, sort_key="id"):
    ws = wb.create_sheet(title=title)
    ws.append(columns)
    for cell in ws[1]:
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")

    sorted_rows = sorted(rows_dict.values(), key=lambda r: str(r.get(sort_key, "")))
    for row in sorted_rows:
        ws.append([row.get(col, "") for col in columns])

    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = BODY_FONT

    # reasonable column widths instead of Excel's tiny default
    for idx, col in enumerate(columns, start=1):
        width = max(12, min(40, len(col) + 4))
        ws.column_dimensions[get_column_letter(idx)].width = width

    ws.freeze_panes = "A2"
    return ws


def main():
    result = parse_media_csv("tests/fixtures/sample_media.csv")
    result = parse_payments_csv("tests/fixtures/sample_payments.csv", result=result)

    wb = Workbook()
    wb.remove(wb.active)  # drop the default blank sheet

    write_tab(wb, "Master Data", MASTER_COLUMNS, result.master)
    write_tab(wb, "Usage Rights - Approved", MASTER_COLUMNS, result.rights_approved)
    write_tab(wb, "Usage Rights - Requested", MASTER_COLUMNS, result.rights_requested)
    write_tab(wb, "Usage Rights - Declined", MASTER_COLUMNS, result.rights_declined)
    write_tab(wb, "Human Review", HUMAN_REVIEW_COLUMNS, build_human_review_rows(result))
    write_tab(wb, "Payments", PAYMENT_COLUMNS, result.payments)

    out_path = "refunnel_sync_preview.xlsx"
    wb.save(out_path)

    print("Wrote", out_path)
    print("Master Data:", len(result.master), "rows")
    print("Usage Rights - Approved:", len(result.rights_approved), "rows")
    print("Usage Rights - Requested:", len(result.rights_requested), "rows")
    print("Usage Rights - Declined:", len(result.rights_declined), "rows")
    print("Human Review:", len(build_human_review_rows(result)), "rows")
    print("Payments:", len(result.payments), "rows")


if __name__ == "__main__":
    main()
