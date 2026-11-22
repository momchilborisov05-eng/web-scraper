"""The crawl engine: a single scheduler thread plus a pool of fetch workers.

The scheduler owns every piece of mutable state (frontier, rate limiters, robots cache, SQLite) and is the only
thread that writes to the database. Workers only fetch, parse and extract, which are pure functions of the
response, and hand a ``PageResult`` back. That split keeps the engine free of locks and lets the tests swap the
thread pool for a serial executor and the clock for a fake one, so a whole crawl runs deterministically in
milliseconds.

Per page:
1. the frontier yields a URL whose host may be contacted now (token bucket + per-host concurrency)
2. robots.txt for the host is fetched once (RFC 9309 status rules) and checked, and its Crawl-delay applied
3. a conditional GET is sent when validators are stored (If-None-Match / If-Modified-Since)
4. 304 or an identical content fingerprint means "unchanged": the stored links are reused and extraction skipped
5. otherwise links are extracted and filtered, and if the URL matches an item rule the item is extracted and
   diffed field by field against its previous version
6. 408/429/5xx and network errors are retried with exponential backoff and full jitter, or Retry-After when the
   server sends it; the wait blocks the whole host, which is what an overloaded server is asking for
"""

from __future__ import annotations

import concurrent.futures as cf
import logging
import random
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import urls
from .dom import parse_html
from .fetch import Fetcher, Response
from .frontier import Entry, Frontier
from .ratelimit import HostLimiter, RetryPolicy, parse_retry_after
from .robots import Robots
from .spec import Spec
from .store import PageRecord, Store

log = logging.getLogger(__name__)
REDIRECTS = (301, 302, 303, 307, 308)


class SerialExecutor(cf.Executor):
    """Runs each task inline at submit time. Used by tests for fully deterministic crawls."""

    def submit(self, fn, /, *args, **kwargs):  # type: ignore[override]
        fut: cf.Future = cf.Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - surfaced through the future like a real executor
            fut.set_exception(exc)
        return fut


@dataclass
class PageResult:
    entry: Entry
    response: Response
    outcome: str = "error"   # new / changed / unchanged / not_modified / redirect / gone / skipped / error
    links: list[str] = field(default_factory=list)
    fingerprint: str | None = None
    item: dict[str, Any] | None = None
    item_rule: str | None = None
    redirect: str | None = None
    errors: list[str] = field(default_factory=list)


@dataclass
class RunReport:
    run_id: int
    status: str = "running"
    pages: int = 0
    seconds: float = 0.0
    status_counts: Counter = field(default_factory=Counter)
    outcomes: Counter = field(default_factory=Counter)
    items: Counter = field(default_factory=Counter)
    bytes: int = 0
    retries: int = 0
    failed: int = 0
    blocked_by_robots: int = 0
    offsite_or_filtered: int = 0
    robots_fetches: int = 0
    extraction_errors: list[str] = field(default_factory=list)
    latencies: list[float] = field(default_factory=list)
    changed_keys: list[str] = field(default_factory=list)

    @property
    def pages_per_sec(self) -> float:
        return self.pages / self.seconds if self.seconds > 0 else 0.0

    def percentile(self, q: float) -> float:
        if not self.latencies:
            return 0.0
        xs = sorted(self.latencies)
        return xs[min(len(xs) - 1, round(q * (len(xs) - 1)))]

    def to_stats(self) -> dict[str, Any]:
        return {
            "pages": self.pages, "seconds": round(self.seconds, 3), "pages_per_sec": round(self.pages_per_sec, 1),
            "status_counts": {str(k): v for k, v in sorted(self.status_counts.items())},
            "outcomes": dict(self.outcomes), "items": dict(self.items), "bytes": self.bytes,
            "retries": self.retries, "failed": self.failed, "blocked_by_robots": self.blocked_by_robots,
            "offsite_or_filtered": self.offsite_or_filtered, "robots_fetches": self.robots_fetches,
            "extraction_errors": len(self.extraction_errors),
            "latency_ms": {"p50": round(self.percentile(0.5) * 1000, 2), "p95": round(self.percentile(0.95) * 1000, 2)},
        }


class Crawler:
    def __init__(self, spec: Spec, store: Store, fetcher: Fetcher, *,
                 clock: Callable[[], float] = time.perf_counter, sleep: Callable[[float], None] = time.sleep,
                 executor: cf.Executor | None = None, rng: random.Random | None = None):
        self.spec = spec
        self.store = store
        self.fetcher = fetcher
        self.clock = clock
        self.sleep = sleep
        self._executor = executor
        self.retry = RetryPolicy(spec.max_attempts, spec.backoff_base, spec.backoff_cap, rng=rng or random.Random())
        self.limiter = HostLimiter(spec.rate, spec.burst)
        self.robots: dict[str, Robots] = {}

    # --- robots ----------------------------------------------------------------------------------------------
    def _robots_for(self, url: str, report: RunReport) -> Robots:
        host = urls.host_of(url)
        if host in self.robots:
            return self.robots[host]
        target = urls.origin_of(url) + "/robots.txt"
        robots = Robots(disallow_all=True)
        for attempt in range(1, self.retry.max_attempts + 1):
            wait = self.limiter.wait_time(host, self.clock())
            if wait > 0:
                self.sleep(wait)
            self.limiter.consume(host, self.clock())
            resp = self._follow_redirects(target)
            report.robots_fetches += 1
            if self.retry.should_retry(resp.status, attempt):
                self.sleep(self.retry.backoff(attempt))
                continue
            robots = Robots.from_status(resp.status, resp.text() if 200 <= resp.status < 300 else "")
            break
        self.limiter.apply_crawl_delay(host, robots.crawl_delay(self.spec.user_agent), self.clock())
        self.robots[host] = robots
        return robots

    def _follow_redirects(self, url: str, hops: int = 5) -> Response:
        resp = self.fetcher.fetch(url, {"User-Agent": self.spec.user_agent}, self.spec.timeout)
        while resp.status in REDIRECTS and hops > 0 and resp.headers.get("location"):
            nxt = urls.normalize(resp.headers["location"], url)
            if not nxt:
                break
            url, hops = nxt, hops - 1
            resp = self.fetcher.fetch(url, {"User-Agent": self.spec.user_agent}, self.spec.timeout)
        if resp.status in REDIRECTS:
            return Response(url, 404)   # too many redirects: treat robots.txt as unavailable (RFC 9309 2.3.1.2)
        return resp

    # --- worker side (pure: no shared state) -----------------------------------------------------------------
    def _work(self, entry: Entry, prev: PageRecord | None) -> PageResult:
        headers = {"User-Agent": self.spec.user_agent}
        if prev is not None and prev.status in (200, 304):
            if prev.etag:
                headers["If-None-Match"] = prev.etag
            if prev.last_modified:
                headers["If-Modified-Since"] = prev.last_modified
        try:
            resp = self.fetcher.fetch(entry.url, headers, self.spec.timeout)
        except Exception as exc:  # noqa: BLE001 - a fetcher bug must not kill the crawl
            resp = Response(entry.url, 0, error=f"{type(exc).__name__}: {exc}")
        result = PageResult(entry, resp)
        status = resp.status
        if status in REDIRECTS:
            result.outcome = "redirect"
            result.redirect = urls.normalize(resp.headers.get("location", ""), entry.url)
            return result
        if status == 304 and prev is not None:
            result.outcome = "not_modified"
            result.links = prev.links
            result.fingerprint = prev.fingerprint
            return result
        if status in (404, 410):
            result.outcome = "gone"
            return result
        if status != 200:
            return result
        if not resp.is_html:
            result.outcome = "skipped"
            return result
        root = parse_html(resp.text())
        result.fingerprint = self.spec.fingerprint(root)
        if prev is not None and prev.fingerprint == result.fingerprint:
            result.outcome = "unchanged"
            result.links = prev.links
            return result
        result.outcome = "changed" if prev is not None and prev.fingerprint else "new"
        result.links = self.spec.links(root, entry.url)
        rule = self.spec.item_rule_for(entry.url)
        if rule is not None:
            result.item_rule = rule.name
            result.item, result.errors = rule.extract(root, entry.url)
        return result

    # --- scheduler side --------------------------------------------------------------------------------------
    def _admit(self, frontier: Frontier, url: str, depth: int, report: RunReport, referrer: str | None) -> None:
        if depth > self.spec.max_depth or frontier.seen(url):
            return
        if not self.spec.should_follow(url):
            report.offsite_or_filtered += 1
            return
        frontier.push(url, self.spec.priority_for(url), depth, self.clock(), referrer)

    def _handle(self, run_id: int, frontier: Frontier, result: PageResult, report: RunReport) -> None:
        entry, resp = result.entry, result.response
        host, now = entry.host, self.clock()
        frontier.done(host, now)
        if self.retry.should_retry(resp.status, entry.attempt):
            delay = parse_retry_after(resp.headers.get("retry-after"), time.time())
            delay = min(delay, self.spec.backoff_cap) if delay is not None else self.retry.backoff(entry.attempt)
            self.limiter.block(host, now + delay)
            frontier.requeue(entry, now)
            report.retries += 1
            self.store.log_fetch(run_id, entry.url, resp.status, "retry", resp.elapsed, len(resp.body), entry.attempt, now)
            return
        report.pages += 1
        report.status_counts[resp.status] += 1
        report.outcomes[result.outcome] += 1
        report.bytes += resp.wire_bytes or len(resp.body)
        report.latencies.append(resp.elapsed)
        self.store.log_fetch(run_id, entry.url, resp.status, result.outcome, resp.elapsed,
                             resp.wire_bytes or len(resp.body), entry.attempt, now)
        if result.outcome == "error":
            report.failed += 1
            return
        if result.redirect:
            self._admit(frontier, result.redirect, entry.depth, report, entry.url)
            return
        if result.outcome in ("skipped", "gone"):
            return
        prev_key = None
        if result.outcome in ("not_modified", "unchanged"):
            prev = self.store.page(entry.url)
            prev_key = prev.item_key if prev else None
            if prev_key:
                self.store.touch_item(run_id, self.spec.name, prev_key)
                report.items["unchanged"] += 1
        item_key = prev_key
        if result.item is not None:
            rule = next(r for r in self.spec.items if r.name == result.item_rule)
            item_key = str(result.item[rule.key])
            outcome, changes = self.store.upsert_item(run_id, self.spec.name, item_key, entry.url, result.item)
            report.items[outcome] += 1
            if changes:
                report.changed_keys.append(item_key)
        report.extraction_errors.extend(result.errors)
        self.store.save_page(run_id, entry.url, resp.status, etag=resp.headers.get("etag"),
                             last_modified=resp.headers.get("last-modified"), fingerprint=result.fingerprint,
                             links=result.links, item_key=item_key, changed=result.outcome in ("new", "changed"))
        for link in result.links:
            self._admit(frontier, link, entry.depth + 1, report, entry.url)

    def run(self, max_pages: int | None = None, progress: Callable[[RunReport], None] | None = None) -> RunReport:
        spec = self.spec
        limit = max_pages or spec.max_pages
        run_id = self.store.start_run(spec.name, spec.seeds)
        report = RunReport(run_id)
        frontier = Frontier(lambda host, now: self.limiter.wait_time(host, now), per_host=spec.per_host)
        for seed in spec.seeds:
            frontier.push(seed, spec.priority_for(seed), 0, self.clock())
        executor = self._executor or cf.ThreadPoolExecutor(max_workers=spec.concurrency,
                                                           thread_name_prefix="trawl-fetch")
        inflight: dict[cf.Future, Entry] = {}
        dispatched = last_progress = 0
        started = self.clock()
        try:
            while True:
                for fut in [f for f in inflight if f.done()]:
                    del inflight[fut]
                    self._handle(run_id, frontier, fut.result(), report)
                    if progress and report.pages - last_progress >= 100:
                        last_progress = report.pages
                        progress(report)
                capped = dispatched >= limit
                if not inflight and (capped or len(frontier) == 0):
                    break
                wait: float | None = None
                if not capped and len(inflight) < spec.concurrency:
                    now = self.clock()
                    entry, wait = frontier.pop(now)
                    if entry is not None:
                        robots = self._robots_for(entry.url, report)
                        if not robots.can_fetch(spec.user_agent, urls.path_and_query(entry.url)):
                            report.blocked_by_robots += 1
                            self.store.log_fetch(run_id, entry.url, 0, "blocked", 0.0, 0, entry.attempt, now)
                            frontier.done(entry.host, now)
                            continue
                        if self.limiter.wait_time(entry.host, self.clock()) > 0:   # Crawl-delay just applied
                            frontier.done(entry.host, self.clock())
                            frontier.requeue(Entry(entry.priority, entry.depth, entry.seq, entry.url,
                                                   entry.attempt - 1, entry.referrer), self.clock())
                            continue
                        self.limiter.consume(entry.host, self.clock())
                        if entry.attempt == 1:
                            dispatched += 1
                        inflight[executor.submit(self._work, entry, self.store.page(entry.url))] = entry
                        continue
                if inflight:
                    cf.wait(list(inflight), timeout=wait, return_when=cf.FIRST_COMPLETED)
                elif wait is not None:
                    self.sleep(wait)
                else:
                    break
            complete = len(frontier) == 0
            if complete:
                report.items["removed"] = self.store.mark_removed(run_id, spec.name)
            report.status = "completed" if complete else "capped"
        except BaseException:
            report.status = "failed"
            raise
        finally:
            if self._executor is None:
                executor.shutdown(wait=True, cancel_futures=True)
            report.seconds = self.clock() - started
            self.store.commit()
            self.store.finish_run(run_id, report.status, report.to_stats())
        return report
