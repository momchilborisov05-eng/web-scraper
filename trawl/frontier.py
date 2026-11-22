"""The URL frontier: deduplication, priorities and per-host politeness in one structure.

It is a small version of the Mercator design (Heydon and Najork, 1999): one priority queue per host ("back queues")
plus a heap of hosts keyed by the time each host may next be contacted. ``pop`` never returns a URL whose host is
still cooling down, so one slow or rate-limited host never blocks the others, and the scheduler only has to ask
"what is ready now, or how long until something is?".

- dedup: every URL is admitted once (retries go through ``requeue``, which bypasses the check)
- within a host: lower ``priority`` first, then shallower ``depth`` (breadth-first), then insertion order
- across hosts: earliest ready time first; host readiness is re-validated lazily on pop, so a host that was blocked
  after it was scheduled (429, Retry-After) is pushed back instead of being contacted early
- at most ``per_host`` requests in flight per host
"""

from __future__ import annotations

import heapq
import itertools
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from .urls import host_of


@dataclass(order=True)
class Entry:
    priority: int
    depth: int
    seq: int
    url: str = field(compare=False)
    attempt: int = field(default=1, compare=False)
    referrer: str | None = field(default=None, compare=False)

    @property
    def host(self) -> str:
        return host_of(self.url)


class Frontier:
    def __init__(self, ready_time: Callable[[str, float], float] | None = None, per_host: int = 1):
        """``ready_time(host, now)`` returns how many seconds ``host`` must still wait (0 when it may be contacted)."""
        if per_host < 1:
            raise ValueError("per_host must be >= 1")
        self._ready_time = ready_time or (lambda host, now: 0.0)
        self.per_host = per_host
        self._seq = itertools.count()
        self._queues: dict[str, list[Entry]] = defaultdict(list)
        self._hosts: list[tuple[float, int, str]] = []   # (ready_at, seq, host)
        self._scheduled: set[str] = set()
        self._active: dict[str, int] = defaultdict(int)
        self._seen: set[str] = set()
        self._pending = 0

    # --- admission -------------------------------------------------------------------------------------------
    def push(self, url: str, priority: int = 0, depth: int = 0, now: float = 0.0, referrer: str | None = None) -> bool:
        """Add a normalized URL. Returns False if it was seen before."""
        if url in self._seen:
            return False
        self._seen.add(url)
        self._enqueue(Entry(priority, depth, next(self._seq), url, 1, referrer), now)
        return True

    def requeue(self, entry: Entry, now: float = 0.0) -> None:
        """Put a URL back for another attempt (after a retryable failure). Not subject to dedup."""
        retry = Entry(entry.priority, entry.depth, next(self._seq), entry.url, entry.attempt + 1, entry.referrer)
        self._enqueue(retry, now)

    def _enqueue(self, entry: Entry, now: float) -> None:
        host = entry.host
        heapq.heappush(self._queues[host], entry)
        self._pending += 1
        if host not in self._scheduled and self._active[host] < self.per_host:
            self._schedule(host, now)

    def _schedule(self, host: str, now: float) -> None:
        at = now + max(0.0, self._ready_time(host, now))
        heapq.heappush(self._hosts, (at, next(self._seq), host))
        self._scheduled.add(host)

    # --- scheduling ------------------------------------------------------------------------------------------
    def pop(self, now: float) -> tuple[Entry | None, float | None]:
        """Return ``(entry, 0)`` if a URL may be fetched now, else ``(None, seconds_to_wait)``.

        ``(None, None)`` means nothing can become ready by waiting alone: the frontier is empty, or every host with
        pending URLs is at its in-flight limit and the caller must report completions via ``done`` first.
        """
        while self._hosts:
            at, _, host = self._hosts[0]
            if at > now:
                return None, at - now
            heapq.heappop(self._hosts)
            self._scheduled.discard(host)
            queue = self._queues.get(host)
            if not queue or self._active[host] >= self.per_host:
                continue
            wait = self._ready_time(host, now)
            if wait > 0:  # the host was slowed down after it was scheduled
                heapq.heappush(self._hosts, (now + wait, next(self._seq), host))
                self._scheduled.add(host)
                continue
            entry = heapq.heappop(queue)
            self._pending -= 1
            self._active[host] += 1
            if queue and self._active[host] < self.per_host:
                self._schedule(host, now)
            return entry, 0.0
        return None, None

    def done(self, host: str, now: float) -> None:
        """Report that a request to ``host`` finished (successfully or not)."""
        self._active[host] = max(0, self._active[host] - 1)
        if self._queues.get(host) and host not in self._scheduled:
            self._schedule(host, now)

    # --- introspection ---------------------------------------------------------------------------------------
    def __len__(self) -> int:
        return self._pending

    @property
    def in_flight(self) -> int:
        return sum(self._active.values())

    @property
    def seen_count(self) -> int:
        return len(self._seen)

    def seen(self, url: str) -> bool:
        return url in self._seen
