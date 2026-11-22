"""CSV and Excel exports of the current items, their price changes and the run history."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from .store import Store


def item_rows(store: Store, spec: str | None = None) -> tuple[list[str], list[dict[str, Any]]]:
    items = store.items(spec)
    fields: list[str] = []
    for item in items:
        for k in item["data"]:
            if k not in fields:
                fields.append(k)
    header = ["key", *fields, "url", "first_seen_run", "last_changed_run"]
    rows = [{"key": i["key"], **{f: _cell(i["data"].get(f)) for f in fields}, "url": i["url"],
             "first_seen_run": i["first_seen_run"], "last_changed_run": i["last_changed_run"]} for i in items]
    return header, rows


def _cell(value: Any) -> Any:
    if isinstance(value, list):
        return "; ".join(map(str, value))
    return value


def write_csv(store: Store, path: str | Path, spec: str | None = None) -> int:
    header, rows = item_rows(store, spec)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:   # BOM so Excel opens UTF-8 correctly
        writer = csv.DictWriter(fh, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def _sheet(ws, header: list[str], rows: list[list[Any]]) -> None:
    ws.append(header)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="B45309")
    for row in rows:
        ws.append(row)
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = ws.dimensions
    for i, name in enumerate(header, start=1):
        width = max([len(str(name))] + [len(str(r[i - 1])) for r in rows[:500]]) + 2
        ws.column_dimensions[get_column_letter(i)].width = min(width, 60)


def write_xlsx(store: Store, path: str | Path, spec: str | None = None) -> int:
    header, rows = item_rows(store, spec)
    wb = Workbook()
    ws = wb.active
    ws.title = "Items"
    _sheet(ws, header, [[r[h] for h in header] for r in rows])

    changes = []
    for c in store.changes(limit=100_000):
        if spec and c["spec"] != spec:
            continue
        for fname, (old, new) in c["changes"].items():
            pct = round((new - old) / old * 100, 1) if isinstance(old, int | float) and isinstance(new, int | float) \
                and old else ""
            changes.append([c["run_id"], c["key"], c["data"].get("title", ""), fname, old, new, pct, c["url"]])
    _sheet(wb.create_sheet("Changes"), ["Run", "Key", "Title", "Field", "Old", "New", "Change %", "URL"], changes)

    runs = []
    for r in store.runs(limit=1000):
        s = r["stats"]
        runs.append([r["id"], r["spec"], r["status"], s.get("pages", 0), s.get("seconds", 0),
                     s.get("pages_per_sec", 0), s.get("status_counts", {}).get("304", 0),
                     s.get("items", {}).get("new", 0), s.get("items", {}).get("changed", 0),
                     s.get("items", {}).get("removed", 0)])
    _sheet(wb.create_sheet("Runs"), ["Run", "Spec", "Status", "Pages", "Seconds", "Pages/s", "304s", "New items",
                                     "Changed items", "Removed items"], runs)
    wb.save(path)
    return len(rows)
