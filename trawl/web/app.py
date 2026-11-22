"""Read-only Flask dashboard over the crawl database: runs, throughput, status codes, latency and item changes.

The dashboard never starts crawls. Accepting URLs over HTTP would turn it into an open proxy (SSRF), so crawling
stays a CLI action and the web app only reads SQLite.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from flask import Flask, abort, g, jsonify, request, send_from_directory

from .. import __version__
from ..store import SCHEMA, connect

STATIC = Path(__file__).parent / "static"
LATENCY_BUCKETS = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000]   # upper bounds in ms


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["DB"] = db_path or os.environ.get("TRAWL_DB", "trawl.db")
    app.config["STARTED"] = time.time()
    init = connect(app.config["DB"])
    init.executescript(SCHEMA)
    init.close()

    def db() -> sqlite3.Connection:
        if "db" not in g:
            g.db = connect(app.config["DB"])
        return g.db

    @app.teardown_appcontext
    def _close(_exc: BaseException | None) -> None:
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.after_request
    def _headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://cdnjs.cloudflare.com; "
            "style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data:")
        return resp

    @app.errorhandler(404)
    def _404(_e):
        return jsonify(error="not found"), 404

    @app.errorhandler(400)
    def _400(e):
        return jsonify(error=str(e.description)), 400

    def run_row(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d["seeds"] = json.loads(d["seeds"])
        d["stats"] = json.loads(d["stats"])
        d["duration"] = (d["finished_at"] - d["started_at"]) if d["finished_at"] else None
        return d

    def int_arg(name: str, default: int, lo: int, hi: int) -> int:
        raw = request.args.get(name, default)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            abort(400, f"{name} must be an integer")
        if not lo <= value <= hi:
            abort(400, f"{name} must be between {lo} and {hi}")
        return value

    @app.get("/health")
    def health():
        n = db().execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        return jsonify(status="ok", version=__version__, runs=n, uptime_s=round(time.time() - app.config["STARTED"]))

    @app.get("/api/summary")
    def summary():
        conn = db()
        runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        items = conn.execute("SELECT COUNT(*) FROM items WHERE removed_run IS NULL").fetchone()[0]
        pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        fetches = conn.execute("SELECT COUNT(*) FROM fetches WHERE outcome NOT IN ('retry', 'blocked')").fetchone()[0]
        last = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        changes = conn.execute("SELECT COUNT(*) FROM item_versions WHERE changes != '{}'").fetchone()[0]
        return jsonify(runs=runs, items=items, pages=pages, fetches=fetches, changes=changes,
                       last_run=run_row(last) if last else None)

    @app.get("/api/runs")
    def runs():
        limit = int_arg("limit", 50, 1, 500)
        rows = db().execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return jsonify(runs=[run_row(r) for r in rows])

    @app.get("/api/runs/<int:run_id>")
    def run_detail(run_id: int):
        conn = db()
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            abort(404)
        fetches = conn.execute("SELECT status, outcome, elapsed_ms, at FROM fetches WHERE run_id = ? "
                               "AND outcome != 'blocked'", (run_id,)).fetchall()
        buckets = [0] * (len(LATENCY_BUCKETS) + 1)
        for f in fetches:
            idx = next((i for i, ub in enumerate(LATENCY_BUCKETS) if f["elapsed_ms"] <= ub), len(LATENCY_BUCKETS))
            buckets[idx] += 1
        timeline: dict[int, int] = {}
        if fetches:
            t0 = min(f["at"] for f in fetches)
            for f in fetches:
                if f["outcome"] != "retry":
                    sec = int(f["at"] - t0)
                    timeline[sec] = timeline.get(sec, 0) + 1
        span = max(timeline) + 1 if timeline else 0
        return jsonify(
            run=run_row(row),
            latency_histogram={"bounds_ms": LATENCY_BUCKETS, "counts": buckets},
            throughput=[timeline.get(s, 0) for s in range(span)],
        )

    @app.get("/api/changes")
    def changes():
        limit = int_arg("limit", 100, 1, 1000)
        run_id = request.args.get("run")
        sql = """SELECT v.key, v.spec, v.run_id, v.data, v.changes, i.url FROM item_versions v
                 JOIN items i ON i.spec = v.spec AND i.key = v.key WHERE v.changes != '{}'"""
        args: list[Any] = []
        if run_id:
            if not run_id.isdigit():
                abort(400, "run must be an integer")
            sql += " AND v.run_id = ?"
            args.append(int(run_id))
        rows = db().execute(sql + " ORDER BY v.id DESC LIMIT ?", (*args, limit)).fetchall()
        out = []
        for r in rows:
            data, ch = json.loads(r["data"]), json.loads(r["changes"])
            price = ch.get("price")
            pct = None
            if price and isinstance(price[0], int | float) and isinstance(price[1], int | float) and price[0]:
                pct = round((price[1] - price[0]) / price[0] * 100, 1)
            out.append({"key": r["key"], "spec": r["spec"], "run_id": r["run_id"], "url": r["url"],
                        "title": data.get("title") or r["key"], "changes": ch, "price_change_pct": pct})
        return jsonify(changes=out)

    @app.get("/api/items")
    def items():
        q = (request.args.get("q") or "").strip().lower()
        limit = int_arg("limit", 50, 1, 500)
        rows = db().execute(
            """SELECT i.spec, i.key, i.url, i.data, i.last_changed_run, i.removed_run,
                      (SELECT COUNT(*) FROM item_versions v WHERE v.spec = i.spec AND v.key = i.key) AS versions
               FROM items i WHERE (? = '' OR lower(i.data) LIKE ?) ORDER BY versions DESC, i.key LIMIT ?""",
            (q, f"%{q}%", limit)).fetchall()
        return jsonify(items=[{**{k: r[k] for k in ("spec", "key", "url", "last_changed_run", "removed_run",
                                                      "versions")}, "data": json.loads(r["data"])} for r in rows])

    @app.get("/api/items/<spec>/<key>/history")
    def history(spec: str, key: str):
        rows = db().execute(
            """SELECT v.run_id, v.data, v.changes, r.started_at FROM item_versions v JOIN runs r ON r.id = v.run_id
               WHERE v.spec = ? AND v.key = ? ORDER BY v.run_id""", (spec, key)).fetchall()
        if not rows:
            abort(404)
        return jsonify(spec=spec, key=key, versions=[
            {"run_id": r["run_id"], "started_at": r["started_at"], "data": json.loads(r["data"]),
             "changes": json.loads(r["changes"])} for r in rows])

    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/static/<path:name>")
    def static_files(name: str):
        return send_from_directory(STATIC, name)

    return app
