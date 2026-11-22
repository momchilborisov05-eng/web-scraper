"""Benchmark trawl against the bundled fixture bookstore served over real HTTP on localhost.

Everything runs offline and is reproducible (seeded catalogue). Measures:
1. cold crawl throughput at several worker counts, with simulated server latency and rate limiting off
2. a re-crawl of an unchanged site: how many requests end as 304 and how many bytes that saves
3. change detection against ground truth after the site changes (precision / recall of changed + removed items)
4. politeness: the request rate the server actually observes under a per-host limit and under robots Crawl-delay
5. resilience: crawl with injected 503s, how many retries it took and whether every item still arrived
6. micro-benchmarks of the hand-written parts: HTML parsing + extraction, CSS selectors, robots matching, frontier

    python scripts/benchmark.py                      # full run, writes examples/benchmark.json and .md
    python scripts/benchmark.py --books 200 --quick  # CI smoke run
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trawl import __version__  # noqa: E402
from trawl.crawler import Crawler  # noqa: E402
from trawl.css import select  # noqa: E402
from trawl.dom import parse_html  # noqa: E402
from trawl.fetch import HttpFetcher  # noqa: E402
from trawl.fixture import BookStore, FixtureServer, FixtureSite  # noqa: E402
from trawl.frontier import Frontier  # noqa: E402
from trawl.robots import Robots  # noqa: E402
from trawl.spec import Spec  # noqa: E402
from trawl.store import Store  # noqa: E402

SPEC = ROOT / "specs" / "books.yaml"


class TimedSite(FixtureSite):
    """Records when the server saw each request (server-side truth for the politeness numbers)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seen_at: list[tuple[float, str]] = []

    def handle(self, path, headers=None):
        self.seen_at.append((time.perf_counter(), path))
        return super().handle(path, headers)


class TimedFetcher(HttpFetcher):
    """Records when each request left the crawler (client-side view of the same gaps)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent_at: list[tuple[float, str]] = []

    def fetch(self, url, headers=None, timeout=15.0):
        self.sent_at.append((time.perf_counter(), url))
        return super().fetch(url, headers, timeout)


def spec_for(url: str, **overrides) -> Spec:
    spec = Spec.load(SPEC).with_seeds([url])
    spec.rate, spec.per_host, spec.concurrency = None, 8, 8
    spec.backoff_base = 0.05
    for k, v in overrides.items():
        setattr(spec, k, v)
    return spec


def crawl(spec: Spec, db: Path, max_pages: int | None = None, fetcher: HttpFetcher | None = None):
    store = Store(db)
    fetcher = fetcher or HttpFetcher(user_agent=spec.user_agent)
    t0 = time.perf_counter()
    report = Crawler(spec, store, fetcher).run(max_pages=max_pages)
    wall = time.perf_counter() - t0
    fetcher.close()
    items = {i["key"]: i["data"] for i in store.items(spec.name)}
    store.close()
    return report, wall, items


def bench_throughput(books: BookStore, latency: float, workers: list[int], tmp: Path) -> list[dict]:
    rows = []
    for n in workers:
        site = FixtureSite(books)
        with FixtureServer(site, latency=latency) as srv:
            report, wall, items = crawl(spec_for(srv.url, per_host=n, concurrency=n), tmp / f"tp{n}.db")
        exact = items == books.ground_truth(1)
        rows.append({"workers": n, "pages": report.pages, "seconds": round(wall, 2),
                     "pages_per_sec": round(report.pages / wall, 1), "items": len(items),
                     "items_match_ground_truth": exact, "p50_ms": round(report.percentile(0.5) * 1000, 1),
                     "p95_ms": round(report.percentile(0.95) * 1000, 1)})
        print(f"  {n:>2} workers: {report.pages} pages in {wall:.2f}s = {report.pages / wall:.1f} pages/s "
              f"(items exact: {exact})")
    return rows


def bench_recrawl_and_changes(books: BookStore, latency: float, workers: int, tmp: Path) -> dict:
    site = FixtureSite(books)
    db = tmp / "changes.db"
    with FixtureServer(site, latency=latency) as srv:
        spec = spec_for(srv.url, per_host=workers, concurrency=workers)
        cold, cold_wall, _ = crawl(spec, db)
        warm, warm_wall, _ = crawl(spec, db)
        site.version = 2
        v2, v2_wall, items_v2 = crawl(spec, db)
    changed_truth, removed_truth = books.diff(1, 2)
    detected = set(v2.changed_keys)
    tp = len(detected & changed_truth)
    precision = tp / len(detected) if detected else 1.0
    recall = tp / len(changed_truth) if changed_truth else 1.0
    store = Store(db)
    removed_detected = {i["key"] for i in store.items("books", include_removed=True) if i["removed_run"] == v2.run_id}
    store.close()
    product_pages = len(books.books(1))
    out = {
        "cold": {"pages": cold.pages, "seconds": round(cold_wall, 2), "bytes_on_wire": cold.bytes},
        "recrawl_unchanged": {
            "pages": warm.pages, "seconds": round(warm_wall, 2), "bytes_on_wire": warm.bytes,
            "not_modified_304": warm.status_counts.get(304, 0), "product_pages": product_pages,
            "fingerprint_unchanged": warm.outcomes.get("unchanged", 0),
            "items_reported_changed": len(warm.changed_keys),
            "bytes_saved_pct": round(100 * (1 - warm.bytes / cold.bytes), 1) if cold.bytes else 0.0,
            "speedup_vs_cold": round(cold_wall / warm_wall, 2) if warm_wall else None},
        "after_site_change": {
            "seconds": round(v2_wall, 2), "pages": v2.pages,
            "changed_truth": len(changed_truth), "changed_detected": len(detected), "true_positives": tp,
            "precision": round(precision, 4), "recall": round(recall, 4),
            "removed_truth": len(removed_truth), "removed_detected": len(removed_detected),
            "removed_exact": removed_detected == removed_truth,
            "items_match_ground_truth_v2": items_v2 == books.ground_truth(2)},
    }
    w = out["recrawl_unchanged"]
    c = out["after_site_change"]
    print(f"  re-crawl unchanged: {w['not_modified_304']}/{product_pages} product pages answered 304, "
          f"{w['bytes_saved_pct']}% fewer bytes, {w['items_reported_changed']} false changes")
    print(f"  after change: precision {c['precision']}, recall {c['recall']} ({tp}/{len(changed_truth)}), "
          f"removed {len(removed_detected)}/{len(removed_truth)} exact={c['removed_exact']}")
    return out


def gaps(stamps: list[tuple[float, str]]) -> list[float]:
    times = sorted(t for t, p in stamps if not p.endswith("/robots.txt"))
    return [b - a for a, b in zip(times, times[1:], strict=False)]


def polite_run(site: TimedSite, spec_overrides: dict, db: Path, pages: int) -> dict:
    with FixtureServer(site) as srv:
        spec = spec_for(srv.url, **spec_overrides)
        fetcher = TimedFetcher(user_agent=spec.user_agent)
        crawl(spec, db, max_pages=pages, fetcher=fetcher)
    server, client = gaps(site.seen_at), gaps(fetcher.sent_at)
    return {"requests": len(server) + 1, "server_min_gap_s": round(min(server), 4),
            "server_mean_gap_s": round(statistics.mean(server), 4), "client_min_gap_s": round(min(client), 4),
            "observed_rate": round(1 / statistics.mean(server), 3)}


def bench_politeness(books: BookStore, rate: float, crawl_delay: float, pages: int, tmp: Path) -> dict:
    out = {
        "token_bucket": {"configured_rate": rate, "expected_gap_s": round(1 / rate, 4),
                         **polite_run(TimedSite(books), {"rate": rate, "burst": 1, "per_host": 1, "concurrency": 4},
                                      tmp / "pol.db", pages)},
        "robots_crawl_delay": {"crawl_delay_s": crawl_delay,
                               **polite_run(TimedSite(books, crawl_delay=crawl_delay),
                                            {"rate": None, "per_host": 4, "concurrency": 4}, tmp / "delay.db",
                                            max(4, pages // 4))},
    }
    tb, cd = out["token_bucket"], out["robots_crawl_delay"]
    print(f"  token bucket {rate}/s: observed {tb['observed_rate']}/s, min gap client {tb['client_min_gap_s']}s / "
          f"server {tb['server_min_gap_s']}s")
    print(f"  Crawl-delay {crawl_delay}s: min gap client {cd['client_min_gap_s']}s / server {cd['server_min_gap_s']}s "
          f"over {cd['requests']} requests (4 workers, rate limit off)")
    return out


def bench_resilience(books: BookStore, flaky: float, tmp: Path) -> dict:
    site = FixtureSite(books, flaky=flaky, flaky_failures=2, retry_after=None)
    with FixtureServer(site) as srv:
        report, wall, items = crawl(spec_for(srv.url, max_attempts=4), tmp / "flaky.db")
    injected = sum(site._failures.values())
    out = {"flaky_fraction": flaky, "failures_per_url": 2, "injected_503s": injected, "retries": report.retries,
           "failed_pages": report.failed, "seconds": round(wall, 2),
           "items_match_ground_truth": items == books.ground_truth(1)}
    print(f"  {injected} injected 503s -> {report.retries} retries, {report.failed} failed, "
          f"items exact: {out['items_match_ground_truth']}")
    return out


def timeit(fn, repeat: int) -> float:
    best = float("inf")
    for _ in range(3):
        t0 = time.perf_counter()
        for _ in range(repeat):
            fn()
        best = min(best, (time.perf_counter() - t0) / repeat)
    return best


def bench_micro(books: BookStore, quick: bool) -> dict:
    site = FixtureSite(books)
    book = next(iter(books.books(1).values()))
    product = site.handle(f"/catalogue/{book.slug}/index.html").body.decode()
    listing = site.handle("/catalogue/page-1.html").body.decode()
    spec = spec_for("http://shop.test/")
    rule = spec.items[0]
    url = f"http://shop.test/catalogue/{book.slug}/index.html"
    n = 50 if quick else 300
    parse_s = timeit(lambda: parse_html(product), n)
    root = parse_html(product)
    extract_s = timeit(lambda: rule.extract(root, url), n)
    lroot = parse_html(listing)
    links_s = timeit(lambda: spec.links(lroot, "http://shop.test/catalogue/page-1.html"), n)
    sel_s = timeit(lambda: select(lroot, "article.product_pod h3 > a[title]"), n)

    robots = Robots.parse("User-agent: *\n" + "".join(f"Disallow: /private{i}/*.pdf$\nAllow: /private{i}/pub/\n"
                                                      for i in range(50)) + "Disallow: /*?sort=\n")
    paths = [f"/private{i % 60}/pub/doc{i}.pdf" for i in range(1000)]
    robots_s = timeit(lambda: [robots.can_fetch("TrawlBot", p) for p in paths], 5 if quick else 20) / len(paths)

    def frontier_ops():
        f = Frontier(per_host=2)
        for i in range(10_000):
            f.push(f"http://h{i % 50}.test/p{i}", priority=i % 3)
        while True:
            e, _ = f.pop(0.0)
            if e is None:
                break
            f.done(e.host, 0.0)

    frontier_s = timeit(frontier_ops, 1 if quick else 3) / 20_000
    out = {
        "parse_product_page_ms": round(parse_s * 1000, 3), "product_page_kb": round(len(product) / 1024, 1),
        "extract_8_fields_ms": round(extract_s * 1000, 3), "extract_links_listing_ms": round(links_s * 1000, 3),
        "css_select_listing_ms": round(sel_s * 1000, 3),
        "robots_can_fetch_us": round(robots_s * 1e6, 2), "robots_rules": 101,
        "frontier_op_us": round(frontier_s * 1e6, 2),
    }
    print(f"  parse {out['parse_product_page_ms']} ms/page, extract {out['extract_8_fields_ms']} ms, "
          f"robots {out['robots_can_fetch_us']} us/check, frontier {out['frontier_op_us']} us/op")
    return out


def markdown(r: dict) -> str:
    env, cfg = r["environment"], r["config"]
    lines = [
        "# trawl benchmark",
        "",
        f"Measured {env['date']} on {env['platform']}, Python {env['python']}, trawl {env['trawl']}. "
        f"Fixture bookstore with {cfg['books']} books ({cfg['site_pages']} pages incl. listings), served over HTTP on "
        f"127.0.0.1 with {cfg['latency_ms']} ms simulated server latency per request. Rate limiting off for the "
        "throughput runs (it is a local fixture); politeness is measured separately below.",
        "",
        "Reproduce: `python scripts/benchmark.py`",
        "",
        "## Cold crawl throughput",
        "",
        "| Workers | Pages | Seconds | Pages/s | p50 ms | p95 ms | Items exact |",
        "|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    for row in r["throughput"]:
        lines.append(f"| {row['workers']} | {row['pages']} | {row['seconds']} | {row['pages_per_sec']} | "
                     f"{row['p50_ms']} | {row['p95_ms']} | {'yes' if row['items_match_ground_truth'] else 'NO'} |")
    w, c = r["recrawl"]["recrawl_unchanged"], r["recrawl"]["after_site_change"]
    cold = r["recrawl"]["cold"]
    lines += [
        "",
        f"## Re-crawl and change detection ({cfg['change_workers']} workers)",
        "",
        "| Run | Pages | Seconds | Bytes on wire | Notes |",
        "|---|---:|---:|---:|---|",
        f"| Cold | {cold['pages']} | {cold['seconds']} | {cold['bytes_on_wire']:,} | first visit |",
        f"| Re-crawl, nothing changed | {w['pages']} | {w['seconds']} | {w['bytes_on_wire']:,} | "
        f"{w['not_modified_304']}/{w['product_pages']} product pages 304, {w['fingerprint_unchanged']} listings "
        f"unchanged by fingerprint, {w['items_reported_changed']} false changes |",
        f"| Re-crawl after site change | {c['pages']} | {c['seconds']} | | {c['changed_truth']} items repriced/"
        f"restocked, {c['removed_truth']} removed |",
        "",
        f"- Bytes saved by conditional requests on an unchanged site: **{w['bytes_saved_pct']}%**",
        f"- Changed items: precision **{c['precision']}**, recall **{c['recall']}** "
        f"({c['true_positives']} of {c['changed_truth']})",
        f"- Removed items detected: {c['removed_detected']} of {c['removed_truth']} (exact set: {c['removed_exact']})",
        f"- Stored items equal the ground truth after the change: {c['items_match_ground_truth_v2']}",
    ]
    tb, cd = r["politeness"]["token_bucket"], r["politeness"]["robots_crawl_delay"]
    res = r["resilience"]
    lines += [
        "",
        "## Politeness",
        "",
        "Gaps between consecutive page requests to the one host, measured where the request leaves the crawler "
        "(client) and where the server receives it (server). The server-side minimum can dip slightly below the "
        "target because arrival time includes per-request network/thread jitter; the mean rate is the long-run figure.",
        "",
        "| Limit | Requests | Target gap | Min gap (client) | Min gap (server) | Observed rate (server) |",
        "|---|---:|---:|---:|---:|---:|",
        f"| Token bucket {tb['configured_rate']}/s, 4 workers | {tb['requests']} | {tb['expected_gap_s']} s | "
        f"{tb['client_min_gap_s']} s | {tb['server_min_gap_s']} s | {tb['observed_rate']}/s |",
        f"| robots.txt Crawl-delay {cd['crawl_delay_s']} s, 4 workers, rate off | {cd['requests']} | "
        f"{cd['crawl_delay_s']} s | {cd['client_min_gap_s']} s | {cd['server_min_gap_s']} s | {cd['observed_rate']}/s |",
        "",
        "## Resilience",
        "",
        f"{round(res['flaky_fraction'] * 100)}% of URLs answer 503 twice before succeeding: {res['injected_503s']} "
        f"injected errors, {res['retries']} retries, {res['failed_pages']} failed pages, every item extracted "
        f"correctly: {res['items_match_ground_truth']}.",
        "",
        "## Hand-written components (best of 3)",
        "",
        "| Operation | Time |",
        "|---|---:|",
    ]
    m = r["micro"]
    lines += [
        f"| Parse a product page ({m['product_page_kb']} KB) into a DOM | {m['parse_product_page_ms']} ms |",
        f"| Extract 8 fields with CSS rules | {m['extract_8_fields_ms']} ms |",
        f"| Extract + normalize all links of a listing page | {m['extract_links_listing_ms']} ms |",
        f"| CSS selector over a listing page | {m['css_select_listing_ms']} ms |",
        f"| robots.txt `can_fetch` against {m['robots_rules']} wildcard rules | {m['robots_can_fetch_us']} us |",
        f"| Frontier push or pop (10k URLs, 50 hosts) | {m['frontier_op_us']} us |",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--books", type=int, default=1000)
    ap.add_argument("--latency", type=float, default=0.02, help="simulated server latency per request (s)")
    ap.add_argument("--workers", default="1,4,8,16")
    ap.add_argument("--rate", type=float, default=5.0)
    ap.add_argument("--crawl-delay", type=float, default=0.5)
    ap.add_argument("--polite-pages", type=int, default=40)
    ap.add_argument("--quick", action="store_true", help="fewer repetitions (CI smoke run)")
    ap.add_argument("--out", default=str(ROOT / "examples"))
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    books = BookStore(n_books=args.books, seed=7)
    site_pages = None
    workers = [int(x) for x in args.workers.split(",")]
    result: dict = {
        "environment": {"date": time.strftime("%Y-%m-%d"), "python": platform.python_version(),
                        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
                        "cpu_count": __import__("os").cpu_count(), "trawl": __version__},
    }
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        print("cold crawl throughput")
        result["throughput"] = bench_throughput(books, args.latency, workers, tmp)
        site_pages = result["throughput"][0]["pages"]
        print("re-crawl and change detection")
        result["recrawl"] = bench_recrawl_and_changes(books, args.latency, max(workers), tmp)
        print("politeness")
        result["politeness"] = bench_politeness(books, args.rate, args.crawl_delay, args.polite_pages, tmp)
        print("resilience")
        result["resilience"] = bench_resilience(books, 0.05, tmp)
    print("micro-benchmarks")
    result["micro"] = bench_micro(books, args.quick)
    result["config"] = {"books": args.books, "site_pages": site_pages, "latency_ms": round(args.latency * 1000, 1),
                        "workers": workers, "change_workers": max(workers), "rate": args.rate,
                        "crawl_delay": args.crawl_delay}

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "benchmark.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (out / "benchmark.md").write_text(markdown(result), encoding="utf-8")
    print(f"wrote {out / 'benchmark.json'} and {out / 'benchmark.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
