import random
from email.utils import formatdate

import pytest

from trawl.ratelimit import HostLimiter, RetryPolicy, TokenBucket, parse_retry_after


def test_token_bucket_burst_then_steady_rate():
    b = TokenBucket(rate=2.0, burst=3, now=0.0)
    for _ in range(3):
        assert b.wait_time(0.0) == 0
        b.consume(0.0)
    assert b.wait_time(0.0) == pytest.approx(0.5)
    assert b.wait_time(0.5) == 0
    b.consume(0.5)
    assert b.wait_time(0.5) == pytest.approx(0.5)


def test_token_bucket_does_not_bank_more_than_burst():
    b = TokenBucket(rate=1.0, burst=2, now=0.0)
    b.wait_time(100.0)
    b.consume(100.0)
    b.consume(100.0)
    assert b.wait_time(100.0) == pytest.approx(1.0)


def test_unlimited_bucket_still_honours_blocks():
    b = TokenBucket(rate=None)
    assert b.wait_time(10.0) == 0
    b.block_until(15.0)
    assert b.wait_time(10.0) == pytest.approx(5.0)
    assert b.wait_time(16.0) == 0


def test_crawl_delay_can_only_slow_a_host_down():
    lim = HostLimiter(rate=4.0, burst=4)
    lim.apply_crawl_delay("a", 2.0)      # 0.5 req/s < 4 req/s: applied
    lim.apply_crawl_delay("b", 0.1)      # 10 req/s > 4 req/s: ignored
    assert lim.bucket("a").rate == pytest.approx(0.5)
    assert lim.bucket("b").rate == 4.0
    unlimited = HostLimiter(rate=None)
    unlimited.apply_crawl_delay("c", 1.0)
    assert unlimited.bucket("c").rate == 1.0


def test_retry_policy_statuses_and_attempt_limit():
    p = RetryPolicy(max_attempts=3)
    assert p.should_retry(503, 1) and p.should_retry(429, 2) and p.should_retry(0, 1)
    assert not p.should_retry(503, 3)
    assert not p.should_retry(404, 1) and not p.should_retry(200, 1)


def test_backoff_is_full_jitter_under_an_exponential_cap():
    p = RetryPolicy(base=0.5, cap=4.0, rng=random.Random(42))
    for attempt, ceiling in [(1, 0.5), (2, 1.0), (3, 2.0), (4, 4.0), (8, 4.0)]:
        samples = [p.backoff(attempt) for _ in range(300)]
        assert all(0 <= s <= ceiling for s in samples)
        assert max(samples) > ceiling * 0.8          # spread over the whole window, not a fixed delay
        assert min(samples) < ceiling * 0.2


def test_parse_retry_after_seconds_and_http_date():
    assert parse_retry_after("7", 0) == 7.0
    assert parse_retry_after(formatdate(1_000_030, usegmt=True), 1_000_000) == pytest.approx(30.0)
    assert parse_retry_after(formatdate(999_000, usegmt=True), 1_000_000) == 0.0
    assert parse_retry_after("soon", 0) is None and parse_retry_after(None, 0) is None
