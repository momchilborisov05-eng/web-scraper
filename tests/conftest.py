"""Shared fixtures: a fake clock, a small fixture bookstore and a crawler wired for deterministic runs."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from trawl.crawler import Crawler, SerialExecutor
from trawl.fixture import BookStore, FixtureFetcher, FixtureSite
from trawl.spec import Spec
from trawl.store import Store

ROOT = Path(__file__).resolve().parent.parent
BOOKS_SPEC = ROOT / "specs" / "books.yaml"
SEED = "http://shop.test/"


class FakeClock:
    """Monotonic time that only moves when someone sleeps. Makes rate limiting and backoff exact and instant."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += max(0.0, seconds)


class TimedFetcher(FixtureFetcher):
    """Records the fake-clock time of every request so tests can check politeness gaps."""

    def __init__(self, site: FixtureSite, clock: FakeClock, latency: float = 0.0):
        super().__init__(site, latency=latency, sleep=clock.sleep)
        self.clock = clock
        self.times: list[tuple[float, str]] = []

    def fetch(self, url, headers=None, timeout=15.0):
        self.times.append((self.clock(), url))
        return super().fetch(url, headers, timeout)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def bookstore() -> BookStore:
    return BookStore(n_books=60, n_categories=5, seed=3, change_rate=0.2, remove_rate=0.05)


@pytest.fixture
def site(bookstore: BookStore) -> FixtureSite:
    return FixtureSite(bookstore)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "trawl.db")
    yield s
    s.close()


def books_spec(**overrides) -> Spec:
    spec = Spec.load(BOOKS_SPEC).with_seeds([SEED])
    spec.rate, spec.per_host, spec.concurrency = None, 4, 4
    for k, v in overrides.items():
        setattr(spec, k, v)
    return spec


def make_crawler(spec: Spec, store: Store, site: FixtureSite, clock: FakeClock, latency: float = 0.01):
    fetcher = TimedFetcher(site, clock, latency)
    crawler = Crawler(spec, store, fetcher, clock=clock, sleep=clock.sleep, executor=SerialExecutor(),
                      rng=random.Random(1))
    return crawler, fetcher
