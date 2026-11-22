"""Per-host token buckets, retry backoff and Retry-After parsing.

All time comes from the caller (``now`` arguments), so the buckets are pure and the tests drive them with a fake
clock instead of sleeping.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime


class TokenBucket:
    """Classic token bucket: ``rate`` tokens per second, at most ``burst`` stored.

    ``rate=None`` means unlimited. A bucket can also be blocked until a point in time (``block_until``), which is how
    a 429/503 with Retry-After or a retry backoff pauses a whole host.
    """

    def __init__(self, rate: float | None, burst: float = 1.0, now: float = 0.0):
        if rate is not None and rate <= 0:
            raise ValueError("rate must be > 0 or None")
        if burst < 1:
            raise ValueError("burst must be >= 1")
        self.rate = rate
        self.burst = float(burst)
        self.tokens = float(burst)
        self.updated = now
        self.blocked_until = -math.inf

    def _refill(self, now: float) -> None:
        if self.rate is not None and now > self.updated:
            self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
        self.updated = max(self.updated, now)

    def wait_time(self, now: float) -> float:
        """Seconds until one token is available (0 if one is available now)."""
        self._refill(now)
        block = max(0.0, self.blocked_until - now)
        if self.rate is None or self.tokens >= 1.0:
            return block
        return max(block, (1.0 - self.tokens) / self.rate)

    def consume(self, now: float) -> None:
        self._refill(now)
        if self.rate is not None:
            self.tokens -= 1.0

    def block_until(self, until: float) -> None:
        self.blocked_until = max(self.blocked_until, until)


class HostLimiter:
    """One token bucket per host. ``Crawl-delay`` from robots.txt can only make a host slower, never faster."""

    def __init__(self, rate: float | None = 1.0, burst: float = 1.0):
        self.rate = rate
        self.burst = burst
        self._buckets: dict[str, TokenBucket] = {}

    def bucket(self, host: str, now: float = 0.0) -> TokenBucket:
        if host not in self._buckets:
            self._buckets[host] = TokenBucket(self.rate, self.burst, now)
        return self._buckets[host]

    def apply_crawl_delay(self, host: str, delay: float | None, now: float = 0.0) -> None:
        if not delay or delay <= 0:
            return
        bucket = self.bucket(host, now)
        robots_rate = 1.0 / delay
        if bucket.rate is None or robots_rate < bucket.rate:
            bucket.rate = robots_rate
            bucket.burst = 1.0
            bucket.tokens = min(bucket.tokens, 1.0)

    def wait_time(self, host: str, now: float) -> float:
        return self.bucket(host, now).wait_time(now)

    def consume(self, host: str, now: float) -> None:
        self.bucket(host, now).consume(now)

    def block(self, host: str, until: float) -> None:
        self.bucket(host).block_until(until)


@dataclass
class RetryPolicy:
    """Exponential backoff with full jitter (delay = uniform(0, min(cap, base * 2**attempt)))."""

    max_attempts: int = 4
    base: float = 0.5
    cap: float = 30.0
    retry_statuses: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})
    rng: random.Random = field(default_factory=random.Random)

    def should_retry(self, status: int, attempt: int) -> bool:
        """``attempt`` is 1 for the first try. Status 0 means a network error or timeout."""
        return attempt < self.max_attempts and (status == 0 or status in self.retry_statuses)

    def backoff(self, attempt: int) -> float:
        ceiling = min(self.cap, self.base * (2 ** (attempt - 1)))
        return self.rng.uniform(0, ceiling)


def parse_retry_after(value: str | None, now_epoch: float) -> float | None:
    """Seconds to wait from a Retry-After header (delta-seconds or an HTTP-date), or None if absent/invalid."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - now_epoch)
