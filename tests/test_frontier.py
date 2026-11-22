import pytest

from trawl.frontier import Frontier


def drain(f: Frontier, now: float = 0.0):
    out = []
    while True:
        entry, _ = f.pop(now)
        if entry is None:
            return out
        out.append(entry)
        f.done(entry.host, now)


def test_dedup_and_priority_order_within_a_host():
    f = Frontier()
    assert f.push("http://a/3", priority=2)
    assert f.push("http://a/1", priority=0)
    assert f.push("http://a/2", priority=1)
    assert not f.push("http://a/1", priority=0)
    assert f.seen_count == 3 and len(f) == 3
    assert [e.url for e in drain(f)] == ["http://a/1", "http://a/2", "http://a/3"]


def test_fifo_among_equal_priorities():
    f = Frontier()
    for i in range(5):
        f.push(f"http://a/{i}")
    assert [e.url for e in drain(f)] == [f"http://a/{i}" for i in range(5)]


def test_per_host_concurrency_limit():
    f = Frontier(per_host=2)
    for i in range(4):
        f.push(f"http://a/{i}")
    f.push("http://b/0")
    got = [f.pop(0.0)[0] for _ in range(3)]
    assert sorted(e.host for e in got) == ["a", "a", "b"]
    assert f.pop(0.0) == (None, None)        # host a is at its limit: only a completion can unblock it
    f.done("a", 0.0)
    entry, _ = f.pop(0.0)
    assert entry is not None and entry.host == "a"


def test_hosts_wait_for_their_ready_time():
    ready = {"slow": 5.0}

    def ready_time(host, now):
        return max(0.0, ready.get(host, 0.0) - now)

    f = Frontier(ready_time)
    f.push("http://slow/1", now=0.0)
    entry, wait = f.pop(0.0)
    assert entry is None and wait == pytest.approx(5.0)
    entry, wait = f.pop(5.0)
    assert entry.url == "http://slow/1"


def test_lazy_revalidation_when_a_host_is_slowed_after_scheduling():
    blocked = {"a": 0.0}
    f = Frontier(lambda h, now: max(0.0, blocked[h] - now))
    f.push("http://a/1", now=0.0)
    blocked["a"] = 3.0                        # e.g. a 429 with Retry-After arrived meanwhile
    entry, wait = f.pop(0.0)
    assert entry is None
    entry, wait = f.pop(0.0)
    assert entry is None and wait == pytest.approx(3.0)
    assert f.pop(3.0)[0].url == "http://a/1"


def test_requeue_bypasses_dedup_and_counts_attempts():
    f = Frontier()
    f.push("http://a/x")
    e, _ = f.pop(0.0)
    f.done("a", 0.0)
    f.requeue(e, 0.0)
    again, _ = f.pop(0.0)
    assert again.url == "http://a/x" and again.attempt == 2 and len(f) == 0
