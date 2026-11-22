"""End-to-end crawls of the fixture bookstore with a fake clock and a serial executor: exact and instant."""

import concurrent.futures as cf
from urllib.parse import urlsplit

import pytest

from trawl.crawler import Crawler
from trawl.fixture import FixtureFetcher, FixtureSite
from trawl.spec import Spec
from trawl.store import Store

from .conftest import SEED, books_spec, make_crawler


def stored_items(store: Store) -> dict[str, dict]:
    return {i["key"]: i["data"] for i in store.items("books")}


def page_requests(fetcher) -> list[tuple[float, str]]:
    return [(t, u) for t, u in fetcher.times if not u.endswith("/robots.txt")]


def permissive_spec(**overrides) -> Spec:
    spec = Spec.from_dict({"name": "open", "seeds": [SEED], "politeness": {"rate": None, "per_host_concurrency": 4},
                           "crawl": {"max_depth": 3}})
    for k, v in overrides.items():
        setattr(spec, k, v)
    return spec


def test_full_crawl_extracts_exactly_the_ground_truth(store, site, bookstore, clock):
    crawler, _ = make_crawler(books_spec(), store, site, clock)
    report = crawler.run()
    assert report.status == "completed"
    assert stored_items(store) == bookstore.ground_truth(1)
    assert report.items["new"] == len(bookstore.books(1))
    assert report.extraction_errors == [] and report.failed == 0


def test_crawl_stays_on_site_and_skips_traps(store, site, clock):
    crawler, fetcher = make_crawler(books_spec(), store, site, clock)
    report = crawler.run()
    requested = [urlsplit(u) for _, u in fetcher.times]
    assert {r.netloc for r in requested} == {"shop.test"}
    paths = [r.path for r in requested]
    assert paths.count("/robots.txt") == 1
    assert not any(p.startswith(("/admin", "/cart")) for p in paths)
    assert not any(r.query for r in requested)
    assert report.offsite_or_filtered > 0
    assert len(paths) == len(set(paths))                       # every URL fetched once
    assert report.outcomes["gone"] == 1                        # the dead link on the home page answers 404


def test_robots_rules_block_disallowed_urls(store, site, clock):
    crawler, fetcher = make_crawler(permissive_spec(), store, site, clock)
    report = crawler.run()
    fetched = [u for _, u in fetcher.times]
    assert report.blocked_by_robots >= 2                       # /admin/ and the ?sort= links
    assert not any("/admin/" in u or "sort=" in u for u in fetched)
    assert report.outcomes["redirect"] == 1                    # /old-catalogue/ -> 301
    assert f"{SEED}catalogue/page-2.html" in fetched


def test_user_agent_specific_group_disallows_everything(store, site, clock):
    crawler, fetcher = make_crawler(permissive_spec(user_agent="BadBot/2.0"), store, site, clock)
    report = crawler.run()
    assert report.pages == 0 and report.blocked_by_robots == 1
    assert [urlsplit(u).path for _, u in fetcher.times] == ["/robots.txt"]


@pytest.mark.parametrize("status, expect_pages", [(404, True), (503, False)])
def test_robots_status_codes_follow_rfc9309(store, bookstore, clock, status, expect_pages):
    site = FixtureSite(bookstore, robots_status=status)
    crawler, _ = make_crawler(books_spec(max_attempts=2, backoff_base=0.01), store, site, clock)
    report = crawler.run()
    assert (report.pages > 0) is expect_pages
    if not expect_pages:
        assert report.blocked_by_robots == 1 and report.robots_fetches == 2   # retried once, still 5xx


def test_max_pages_caps_the_run(store, site, clock):
    crawler, _ = make_crawler(books_spec(), store, site, clock)
    report = crawler.run(max_pages=25)
    assert report.status == "capped" and report.pages == 25
    assert report.items.get("removed", 0) == 0                  # an incomplete run must not mark items removed


def test_token_bucket_spaces_requests_to_one_host(store, site, clock):
    crawler, fetcher = make_crawler(books_spec(rate=2.0, burst=1, per_host=1), store, site, clock, latency=0.0)
    crawler.run(max_pages=40)
    times = [t for t, _ in fetcher.times]
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert min(gaps) >= 0.5 - 1e-9
    assert (times[-1] - times[0]) / (len(times) - 1) == pytest.approx(0.5, abs=0.01)


def test_crawl_delay_from_robots_slows_the_host(store, bookstore, clock):
    site = FixtureSite(bookstore, crawl_delay=3)
    crawler, fetcher = make_crawler(books_spec(rate=None), store, site, clock, latency=0.0)
    crawler.run(max_pages=10)
    times = [t for t, _ in page_requests(fetcher)]
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert min(gaps) >= 3 - 1e-9


def test_transient_503s_are_retried_until_success(store, bookstore, clock):
    site = FixtureSite(bookstore, flaky=0.3, flaky_failures=2, retry_after=None)
    crawler, fetcher = make_crawler(books_spec(max_attempts=4, backoff_base=0.1), store, site, clock)
    report = crawler.run()
    flaky_paths = {p for p, n in site._failures.items() if n}
    assert flaky_paths and report.retries == 2 * len(flaky_paths)
    assert report.failed == 0
    assert stored_items(store) == bookstore.ground_truth(1)
    assert max(clock.sleeps) <= 0.1 * 2 ** 3                     # full-jitter backoff stays under its ceiling


def test_permanent_failures_give_up_after_max_attempts(store, bookstore, clock):
    site = FixtureSite(bookstore, flaky=0.2, flaky_failures=99, retry_after=None)
    crawler, fetcher = make_crawler(books_spec(max_attempts=3, backoff_base=0.01), store, site, clock)
    report = crawler.run()
    assert report.failed > 0 and report.status_counts[503] == report.failed
    attempts = {}
    for _, u in fetcher.times:
        attempts[u] = attempts.get(u, 0) + 1
    failing = [u for u in attempts if site._failures.get(urlsplit(u).path)]
    assert failing and all(attempts[u] == 3 for u in failing)


def test_retry_after_blocks_the_whole_host(store, bookstore, clock):
    site = FixtureSite(bookstore, flaky=0.1, flaky_failures=1, retry_after=7)
    crawler, fetcher = make_crawler(books_spec(per_host=1, concurrency=1), store, site, clock, latency=0.0)
    crawler.run(max_pages=80)
    first = next(i for i, (_, u) in enumerate(fetcher.times) if site._failures.get(urlsplit(u).path))
    t_fail, t_next = fetcher.times[first][0], fetcher.times[first + 1][0]
    assert t_next - t_fail >= 7 - 1e-9                          # nothing at all is sent to the host for 7 s


def test_recrawl_uses_conditional_requests_and_reports_no_changes(store, site, bookstore, clock):
    spec = books_spec()
    make_crawler(spec, store, site, clock)[0].run()
    crawler, fetcher = make_crawler(spec, store, site, clock)
    report = crawler.run()
    products = [h for u, h in fetcher.calls if spec.item_rule_for(u) and "missing-book" not in u]
    assert products and all("If-None-Match" in h for h in products)
    assert report.status_counts[304] == len(bookstore.books(1))
    assert report.outcomes["changed"] == 0 and report.outcomes["new"] == 0
    assert report.items["unchanged"] == len(bookstore.books(1)) and not report.changed_keys


def test_footer_noise_is_ignored_only_when_configured(store, site, clock):
    make_crawler(books_spec(), store, site, clock)[0].run()
    quiet = make_crawler(books_spec(), store, site, clock)[0].run()
    noisy = make_crawler(books_spec(hash_ignore=[]), store, site, clock)[0].run()
    assert quiet.outcomes["unchanged"] > 0 and quiet.outcomes["changed"] == 0
    assert noisy.outcomes["changed"] == quiet.outcomes["unchanged"]


def test_change_detection_matches_ground_truth_across_versions(store, site, bookstore, clock):
    spec = books_spec()
    make_crawler(spec, store, site, clock)[0].run()
    site.version = 2
    report = make_crawler(spec, store, site, clock)[0].run()
    changed, removed = bookstore.diff(1, 2)
    assert changed and removed
    assert set(report.changed_keys) == changed
    assert report.items["removed"] == len(removed)
    assert stored_items(store) == bookstore.ground_truth(2)
    removed_rows = {i["key"] for i in store.items("books", include_removed=True) if i["removed_run"]}
    assert removed_rows == removed
    key = next(iter(changed))
    history = store.history("books", key)
    assert len(history) == 2 and history[1]["changes"]
    for field_name, (old, new) in history[1]["changes"].items():
        assert old == bookstore.ground_truth(1)[key][field_name]
        assert new == bookstore.ground_truth(2)[key][field_name]


def test_network_errors_are_retried_then_recorded_as_failures(store, site, clock):
    spec = books_spec(seeds=[SEED, "http://down.test/"], allowed_domains=frozenset({"shop.test", "down.test"}),
                      max_attempts=2, backoff_base=0.01)
    crawler, _ = make_crawler(spec, store, site, clock)
    crawler.fetcher.hosts = {"shop.test"}
    report = crawler.run()
    assert report.blocked_by_robots == 1          # robots.txt unreachable (network error) -> disallow all (RFC 9309)
    assert report.failed == 0 and report.items["new"] == len(site.store.books(1))


def test_threaded_run_matches_serial_ground_truth(tmp_path, bookstore):
    site = FixtureSite(bookstore)
    spec = books_spec(concurrency=8, per_host=8)
    store = Store(tmp_path / "threads.db")
    report = Crawler(spec, store, FixtureFetcher(site)).run()
    assert report.status == "completed" and report.failed == 0
    assert stored_items(store) == bookstore.ground_truth(1)
    store.close()


def test_worker_exceptions_do_not_kill_the_crawl(store, site, clock):
    crawler, fetcher = make_crawler(books_spec(max_attempts=1), store, site, clock)
    original = fetcher.fetch

    def exploding(url, headers=None, timeout=15.0):
        if url == f"{SEED}catalogue/page-2.html":
            raise RuntimeError("boom")
        return original(url, headers, timeout)

    crawler.fetcher.fetch = exploding
    report = crawler.run()
    assert report.status == "completed" and report.failed == 1


def test_serial_executor_propagates_results():
    from trawl.crawler import SerialExecutor

    ex = SerialExecutor()
    assert ex.submit(lambda x: x * 2, 21).result() == 42
    fut = ex.submit(lambda: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        fut.result()
    assert isinstance(fut, cf.Future)
