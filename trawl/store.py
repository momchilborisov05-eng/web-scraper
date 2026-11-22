"""SQLite persistence: run history, per-URL validators and fingerprints, items and their version history.

Only the crawler's scheduler thread writes (fetch workers never touch the database), so a single connection with
WAL is enough and there is no write contention. The dashboard opens its own read connections.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    spec TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL DEFAULT 'running',
    seeds TEXT NOT NULL,
    stats TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS pages (
    url TEXT PRIMARY KEY,
    status INTEGER,
    etag TEXT,
    last_modified TEXT,
    fingerprint TEXT,
    links TEXT NOT NULL DEFAULT '[]',
    item_key TEXT,
    first_seen_run INTEGER,
    last_fetched_run INTEGER,
    last_changed_run INTEGER,
    fetched_at REAL
);
CREATE TABLE IF NOT EXISTS fetches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    url TEXT NOT NULL,
    status INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    elapsed_ms REAL NOT NULL DEFAULT 0,
    bytes INTEGER NOT NULL DEFAULT 0,
    attempt INTEGER NOT NULL DEFAULT 1,
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS fetches_run ON fetches(run_id);
CREATE TABLE IF NOT EXISTS items (
    spec TEXT NOT NULL,
    key TEXT NOT NULL,
    url TEXT NOT NULL,
    data TEXT NOT NULL,
    first_seen_run INTEGER NOT NULL,
    last_seen_run INTEGER NOT NULL,
    last_changed_run INTEGER NOT NULL,
    removed_run INTEGER,
    PRIMARY KEY (spec, key)
);
CREATE TABLE IF NOT EXISTS item_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    spec TEXT NOT NULL,
    key TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    data TEXT NOT NULL,
    changes TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS item_versions_key ON item_versions(spec, key);
CREATE INDEX IF NOT EXISTS item_versions_run ON item_versions(run_id);
"""


@dataclass
class PageRecord:
    url: str
    status: int | None
    etag: str | None
    last_modified: str | None
    fingerprint: str | None
    links: list[str]
    item_key: str | None


def diff_fields(old: dict[str, Any], new: dict[str, Any]) -> dict[str, list[Any]]:
    """Field-level changes ``{field: [old, new]}`` between two versions of an item."""
    return {k: [old.get(k), new.get(k)] for k in sorted(set(old) | set(new)) if old.get(k) != new.get(k)}


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


class Store:
    def __init__(self, path: str | Path = "trawl.db", wall: Callable[[], float] = time.time):
        self.path = str(path)
        self.wall = wall
        self.conn = connect(self.path)
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- runs ------------------------------------------------------------------------------------------------
    def start_run(self, spec: str, seeds: list[str]) -> int:
        cur = self.conn.execute("INSERT INTO runs (spec, started_at, seeds) VALUES (?, ?, ?)",
                                (spec, self.wall(), json.dumps(seeds)))
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, stats: dict[str, Any]) -> None:
        self.conn.execute("UPDATE runs SET finished_at = ?, status = ?, stats = ? WHERE id = ?",
                          (self.wall(), status, json.dumps(stats), run_id))
        self.conn.commit()

    def runs(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_run_dict(r) for r in rows]

    def run(self, run_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return _run_dict(row) if row else None

    # --- pages and fetch log ---------------------------------------------------------------------------------
    def page(self, url: str) -> PageRecord | None:
        row = self.conn.execute("SELECT * FROM pages WHERE url = ?", (url,)).fetchone()
        if row is None:
            return None
        return PageRecord(row["url"], row["status"], row["etag"], row["last_modified"], row["fingerprint"],
                          json.loads(row["links"]), row["item_key"])

    def save_page(self, run_id: int, url: str, status: int, *, etag: str | None, last_modified: str | None,
                  fingerprint: str | None, links: list[str], item_key: str | None, changed: bool) -> None:
        self.conn.execute(
            """INSERT INTO pages (url, status, etag, last_modified, fingerprint, links, item_key, first_seen_run,
                                  last_fetched_run, last_changed_run, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(url) DO UPDATE SET
                   status = excluded.status,
                   etag = COALESCE(excluded.etag, pages.etag),
                   last_modified = COALESCE(excluded.last_modified, pages.last_modified),
                   fingerprint = COALESCE(excluded.fingerprint, pages.fingerprint),
                   links = excluded.links,
                   item_key = COALESCE(excluded.item_key, pages.item_key),
                   last_fetched_run = excluded.last_fetched_run,
                   last_changed_run = CASE WHEN ? THEN excluded.last_changed_run ELSE pages.last_changed_run END,
                   fetched_at = excluded.fetched_at""",
            (url, status, etag, last_modified, fingerprint, json.dumps(links), item_key, run_id, run_id, run_id,
             self.wall(), int(changed)))

    def log_fetch(self, run_id: int, url: str, status: int, outcome: str, elapsed: float, nbytes: int,
                  attempt: int, at: float) -> None:
        self.conn.execute(
            "INSERT INTO fetches (run_id, url, status, outcome, elapsed_ms, bytes, attempt, at) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, url, status, outcome, round(elapsed * 1000, 3), nbytes, attempt, at))

    def commit(self) -> None:
        self.conn.commit()

    # --- items -----------------------------------------------------------------------------------------------
    def upsert_item(self, run_id: int, spec: str, key: str, url: str, data: dict[str, Any]) -> tuple[str, dict]:
        """Store the latest version of an item. Returns ``(outcome, changes)``, outcome in new/changed/unchanged."""
        row = self.conn.execute("SELECT data, removed_run FROM items WHERE spec = ? AND key = ?",
                                (spec, key)).fetchone()
        payload = json.dumps(data, sort_keys=True, ensure_ascii=False)
        if row is None:
            self.conn.execute(
                """INSERT INTO items (spec, key, url, data, first_seen_run, last_seen_run, last_changed_run)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""", (spec, key, url, payload, run_id, run_id, run_id))
            self.conn.execute("INSERT INTO item_versions (spec, key, run_id, data) VALUES (?, ?, ?, ?)",
                              (spec, key, run_id, payload))
            return "new", {}
        changes = diff_fields(json.loads(row["data"]), data)
        if not changes and row["removed_run"] is None:
            self.touch_item(run_id, spec, key)
            return "unchanged", {}
        self.conn.execute(
            """UPDATE items SET url = ?, data = ?, last_seen_run = ?, last_changed_run = ?, removed_run = NULL
               WHERE spec = ? AND key = ?""", (url, payload, run_id, run_id, spec, key))
        self.conn.execute("INSERT INTO item_versions (spec, key, run_id, data, changes) VALUES (?, ?, ?, ?, ?)",
                          (spec, key, run_id, payload, json.dumps(changes, ensure_ascii=False)))
        return ("changed" if changes else "restored"), changes

    def touch_item(self, run_id: int, spec: str, key: str) -> None:
        self.conn.execute("UPDATE items SET last_seen_run = ? WHERE spec = ? AND key = ?", (run_id, spec, key))

    def mark_removed(self, run_id: int, spec: str) -> int:
        cur = self.conn.execute(
            "UPDATE items SET removed_run = ? WHERE spec = ? AND last_seen_run < ? AND removed_run IS NULL",
            (run_id, spec, run_id))
        return cur.rowcount

    def items(self, spec: str | None = None, include_removed: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM items WHERE 1=1"
        args: list[Any] = []
        if spec:
            sql += " AND spec = ?"
            args.append(spec)
        if not include_removed:
            sql += " AND removed_run IS NULL"
        rows = self.conn.execute(sql + " ORDER BY key", args).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    def history(self, spec: str, key: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT v.run_id, v.data, v.changes, r.started_at FROM item_versions v JOIN runs r ON r.id = v.run_id
               WHERE v.spec = ? AND v.key = ? ORDER BY v.run_id""", (spec, key)).fetchall()
        return [{"run_id": r["run_id"], "started_at": r["started_at"], "data": json.loads(r["data"]),
                 "changes": json.loads(r["changes"])} for r in rows]

    def changes(self, run_id: int | None = None, limit: int = 500) -> list[dict[str, Any]]:
        """Changed items (versions with a non-empty diff), newest first."""
        sql = """SELECT v.spec, v.key, v.run_id, v.data, v.changes, i.url FROM item_versions v
                 JOIN items i ON i.spec = v.spec AND i.key = v.key WHERE v.changes != '{}'"""
        args: list[Any] = []
        if run_id is not None:
            sql += " AND v.run_id = ?"
            args.append(run_id)
        rows = self.conn.execute(sql + " ORDER BY v.id DESC LIMIT ?", (*args, limit)).fetchall()
        return [{"spec": r["spec"], "key": r["key"], "run_id": r["run_id"], "url": r["url"],
                 "data": json.loads(r["data"]), "changes": json.loads(r["changes"])} for r in rows]


def _run_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["seeds"] = json.loads(d["seeds"])
    d["stats"] = json.loads(d["stats"])
    return d
