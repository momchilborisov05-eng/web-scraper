"""Command line: ``python -m trawl <command>`` (or ``trawl <command>`` after ``pip install .``)."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from . import __version__, urls
from .crawler import Crawler, RunReport
from .export import write_csv, write_xlsx
from .fetch import HttpFetcher
from .robots import Robots
from .spec import Spec, SpecError
from .store import Store

DEFAULT_SPEC = Path(__file__).resolve().parent.parent / "specs" / "books.yaml"


def _summary(r: RunReport) -> str:
    s = r.to_stats()
    codes = ", ".join(f"{k}: {v}" for k, v in s["status_counts"].items())
    items = s["items"]
    return (f"run #{r.run_id} {r.status}: {r.pages} pages in {r.seconds:.1f}s ({r.pages_per_sec:.1f} pages/s)\n"
            f"  status codes   {codes or '-'}\n"
            f"  pages          new {r.outcomes['new']}, changed {r.outcomes['changed']}, "
            f"unchanged {r.outcomes['unchanged']}, 304 not modified {r.outcomes['not_modified']}\n"
            f"  items          new {items.get('new', 0)}, changed {items.get('changed', 0)}, "
            f"unchanged {items.get('unchanged', 0)}, removed {items.get('removed', 0)}\n"
            f"  politeness     robots-blocked {r.blocked_by_robots}, filtered/off-site {r.offsite_or_filtered}, "
            f"retries {r.retries}, failed {r.failed}\n"
            f"  latency        p50 {s['latency_ms']['p50']} ms, p95 {s['latency_ms']['p95']} ms")


def _progress(r: RunReport) -> None:
    print(f"  ... {r.pages} pages, {sum(r.items.values())} items", file=sys.stderr)


def cmd_crawl(args: argparse.Namespace) -> int:
    spec = Spec.load(args.spec)
    if args.seed:
        spec = spec.with_seeds(args.seed)
    if args.concurrency:
        spec.concurrency = args.concurrency
    if args.rate is not None:
        spec.rate = args.rate if args.rate > 0 else None
    store = Store(args.db)
    fetcher = HttpFetcher(user_agent=spec.user_agent)
    print(f"crawling {', '.join(spec.seeds)} with spec '{spec.name}' "
          f"(rate {spec.rate or 'unlimited'}/s per host, {spec.concurrency} workers)")
    report = Crawler(spec, store, fetcher).run(max_pages=args.max_pages, progress=_progress)
    print(_summary(report))
    return 0 if report.status != "failed" else 1


def cmd_demo(args: argparse.Namespace) -> int:
    from .fixture import BookStore, FixtureServer, FixtureSite

    db = Path(args.db)
    if db.exists() and not args.keep:
        for suffix in ("", "-wal", "-shm"):
            Path(str(db) + suffix).unlink(missing_ok=True)
    site = FixtureSite(BookStore(n_books=args.books, seed=args.seed), flaky=0.02, retry_after=None)
    store = Store(db)
    with FixtureServer(site, latency=args.latency) as server:
        spec = Spec.load(DEFAULT_SPEC).with_seeds([server.url])
        spec.rate, spec.per_host, spec.concurrency = None, 8, 8
        spec.backoff_base = 0.05
        print(f"fixture bookstore with {args.books} books at {server.url}")
        for version in range(1, args.versions + 1):
            site.version = version
            report = Crawler(spec, store, HttpFetcher(user_agent=spec.user_agent)).run()
            print(f"\n[site version {version}] " + _summary(report))
            if args.pause:
                time.sleep(args.pause)
    print(f"\ndatabase: {db}\nnext: python -m trawl serve --db {db}")
    return 0


def cmd_fixture(args: argparse.Namespace) -> int:
    from .fixture import BookStore, FixtureServer, FixtureSite

    site = FixtureSite(BookStore(n_books=args.books), version=args.version, crawl_delay=args.crawl_delay)
    server = FixtureServer(site, port=args.port, latency=args.latency)
    print(f"fixture bookstore (version {args.version}, {args.books} books) on {server.url}  - Ctrl+C to stop")
    try:
        server.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    store = Store(args.db)
    out = Path(args.out)
    n = write_xlsx(store, out, args.spec) if out.suffix.lower() == ".xlsx" else write_csv(store, out, args.spec)
    print(f"wrote {n} items to {out}")
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    store = Store(args.db)
    print(f"{'run':>4}  {'spec':<10} {'status':<10} {'pages':>6} {'pages/s':>8} {'304s':>5} {'new':>5} "
          f"{'changed':>7} {'removed':>7}")
    for r in reversed(store.runs(args.limit)):
        s = r["stats"]
        it = s.get("items", {})
        print(f"{r['id']:>4}  {r['spec']:<10} {r['status']:<10} {s.get('pages', 0):>6} {s.get('pages_per_sec', 0):>8} "
              f"{s.get('status_counts', {}).get('304', 0):>5} {it.get('new', 0):>5} {it.get('changed', 0):>7} "
              f"{it.get('removed', 0):>7}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .web import create_app

    app = create_app(args.db)
    print(f"dashboard on http://{args.host}:{args.port}  (database {args.db})")
    app.run(host=args.host, port=args.port, debug=False)
    return 0


def cmd_robots(args: argparse.Namespace) -> int:
    url = urls.normalize(args.url)
    if not url:
        print("not an http(s) URL", file=sys.stderr)
        return 2
    resp = HttpFetcher().fetch(urls.origin_of(url) + "/robots.txt")
    robots = Robots.from_status(resp.status, resp.text())
    allowed = robots.can_fetch(args.user_agent, urls.path_and_query(url))
    print(f"robots.txt: HTTP {resp.status or resp.error}")
    print(f"{'ALLOWED' if allowed else 'DISALLOWED'} for {args.user_agent}: {url}")
    delay = robots.crawl_delay(args.user_agent)
    if delay:
        print(f"crawl-delay: {delay:g}s")
    for sm in robots.sitemaps:
        print(f"sitemap: {sm}")
    return 0 if allowed else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trawl", description="Polite crawler and scraper with change tracking.")
    p.add_argument("--version", action="version", version=f"trawl {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("crawl", help="run a crawl spec")
    c.add_argument("spec", nargs="?", default=str(DEFAULT_SPEC))
    c.add_argument("--seed", action="append", help="override the spec's seed URLs (repeatable)")
    c.add_argument("--db", default="trawl.db")
    c.add_argument("--max-pages", type=int)
    c.add_argument("--concurrency", type=int)
    c.add_argument("--rate", type=float, help="requests/s per host (0 = unlimited)")
    c.set_defaults(func=cmd_crawl)

    d = sub.add_parser("demo", help="crawl the bundled fixture site through several versions (offline)")
    d.add_argument("--db", default="trawl.db")
    d.add_argument("--books", type=int, default=1000)
    d.add_argument("--versions", type=int, default=4)
    d.add_argument("--seed", type=int, default=7)
    d.add_argument("--latency", type=float, default=0.0)
    d.add_argument("--pause", type=float, default=0.0, help="seconds between runs")
    d.add_argument("--keep", action="store_true", help="keep an existing database instead of starting fresh")
    d.set_defaults(func=cmd_demo)

    f = sub.add_parser("fixture", help="serve the fake bookstore over HTTP")
    f.add_argument("--port", type=int, default=8765)
    f.add_argument("--books", type=int, default=1000)
    f.add_argument("--version", type=int, default=1, dest="version")
    f.add_argument("--latency", type=float, default=0.0)
    f.add_argument("--crawl-delay", type=float)
    f.set_defaults(func=cmd_fixture)

    e = sub.add_parser("export", help="export items to .csv or .xlsx")
    e.add_argument("out")
    e.add_argument("--db", default="trawl.db")
    e.add_argument("--spec")
    e.set_defaults(func=cmd_export)

    r = sub.add_parser("runs", help="list crawl runs")
    r.add_argument("--db", default="trawl.db")
    r.add_argument("--limit", type=int, default=20)
    r.set_defaults(func=cmd_runs)

    s = sub.add_parser("serve", help="start the dashboard")
    s.add_argument("--db", default="trawl.db")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=cmd_serve)

    rb = sub.add_parser("robots", help="check whether a URL may be crawled")
    rb.add_argument("url")
    rb.add_argument("--user-agent", default="TrawlBot")
    rb.set_defaults(func=cmd_robots)
    return p


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    try:
        return args.func(args)
    except SpecError as e:
        print(e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
