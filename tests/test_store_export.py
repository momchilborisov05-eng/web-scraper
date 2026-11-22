import csv

import pytest
from openpyxl import load_workbook

from trawl.export import write_csv, write_xlsx
from trawl.store import Store, diff_fields

from .conftest import books_spec, make_crawler


def test_diff_fields_reports_old_and_new_values():
    assert diff_fields({"a": 1, "b": 2}, {"a": 1, "b": 3, "c": 4}) == {"b": [2, 3], "c": [None, 4]}
    assert diff_fields({"a": 1}, {"a": 1}) == {}


def test_item_lifecycle_new_changed_unchanged_removed_restored(tmp_path):
    s = Store(tmp_path / "s.db")
    r1 = s.start_run("x", ["http://a/"])
    assert s.upsert_item(r1, "x", "k", "http://a/k", {"price": 10}) == ("new", {})
    r2 = s.start_run("x", ["http://a/"])
    assert s.upsert_item(r2, "x", "k", "http://a/k", {"price": 10}) == ("unchanged", {})
    r3 = s.start_run("x", ["http://a/"])
    assert s.upsert_item(r3, "x", "k", "http://a/k", {"price": 12}) == ("changed", {"price": [10, 12]})
    r4 = s.start_run("x", ["http://a/"])
    assert s.mark_removed(r4, "x") == 1 and s.items("x") == []
    r5 = s.start_run("x", ["http://a/"])
    assert s.upsert_item(r5, "x", "k", "http://a/k", {"price": 12})[0] == "restored"
    assert [h["run_id"] for h in s.history("x", "k")] == [r1, r3, r5]
    assert [c["run_id"] for c in s.changes()] == [r3]
    s.close()


def test_runs_are_recorded_with_stats(store, site, clock):
    make_crawler(books_spec(), store, site, clock)[0].run()
    run = store.runs()[0]
    assert run["status"] == "completed" and run["finished_at"] is not None
    assert run["stats"]["pages"] > 0 and run["stats"]["status_counts"]["200"] > 0
    page = store.page("http://shop.test/")
    assert page.status == 200 and page.fingerprint and page.links


@pytest.fixture
def two_runs(store, site, clock):
    spec = books_spec()
    make_crawler(spec, store, site, clock)[0].run()
    site.version = 2
    make_crawler(spec, store, site, clock)[0].run()
    return store


def test_csv_export_round_trips(two_runs, tmp_path, bookstore):
    out = tmp_path / "items.csv"
    n = write_csv(two_runs, out, "books")
    assert n == len(bookstore.books(2))
    with open(out, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    truth = bookstore.ground_truth(2)
    assert {r["key"] for r in rows} == set(truth)
    row = rows[0]
    assert float(row["price"]) == truth[row["key"]]["price"] and row["title"] == truth[row["key"]]["title"]


def test_xlsx_export_has_items_changes_and_runs(two_runs, tmp_path, bookstore):
    out = tmp_path / "items.xlsx"
    write_xlsx(two_runs, out, "books")
    wb = load_workbook(out)
    assert wb.sheetnames == ["Items", "Changes", "Runs"]
    assert wb["Items"].max_row - 1 == len(bookstore.books(2))
    changed, _ = bookstore.diff(1, 2)
    change_keys = {row[1] for row in wb["Changes"].iter_rows(min_row=2, values_only=True)}
    assert change_keys == changed
    assert wb["Runs"].max_row - 1 == 2
