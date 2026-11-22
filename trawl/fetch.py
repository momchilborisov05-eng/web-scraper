"""HTTP fetching: a small keep-alive client on ``http.client`` and the ``Fetcher`` protocol the crawler depends on.

The crawler only knows ``Fetcher.fetch(url, headers, timeout) -> Response``. Production uses ``HttpFetcher``; tests
and the benchmark plug in the in-process fixture site (``trawl.fixture.FixtureFetcher``) or a scripted fake, which
is what keeps the test suite offline and deterministic.

``HttpFetcher`` does not follow redirects: the crawler sees every 3xx and decides (same domain? robots allowed?
seen before?) like for any other link.
"""

from __future__ import annotations

import gzip
import http.client
import re
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

USER_AGENT = "TrawlBot/1.0 (+https://github.com/momchilborisov05-eng/web-scraper; polite crawler)"
_META_CHARSET = re.compile(rb"<meta[^>]+charset=[\"']?([A-Za-z0-9_\-]+)", re.I)


@dataclass
class Response:
    url: str
    status: int                       # 0 = network error or timeout
    headers: dict[str, str] = field(default_factory=dict)   # lower-case names
    body: bytes = b""
    elapsed: float = 0.0
    error: str | None = None
    wire_bytes: int = 0               # bytes on the wire (compressed size when gzip was used)

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").lower()

    @property
    def is_html(self) -> bool:
        ct = self.content_type
        return "html" in ct or (not ct and self.body.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")))

    def text(self) -> str:
        charset = None
        m = re.search(r"charset=([\w\-]+)", self.content_type)
        if m:
            charset = m.group(1)
        else:
            mm = _META_CHARSET.search(self.body[:2048])
            if mm:
                charset = mm.group(1).decode("ascii", "ignore")
        try:
            return self.body.decode(charset or "utf-8", errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


class Fetcher(Protocol):
    def fetch(self, url: str, headers: dict[str, str] | None = None, timeout: float = 15.0) -> Response: ...


def decode_body(raw: bytes, encoding: str) -> bytes:
    encoding = encoding.lower().strip()
    if encoding in ("gzip", "x-gzip"):
        return gzip.decompress(raw)
    if encoding == "deflate":
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw


class HttpFetcher:
    """HTTP/1.1 client with one persistent connection per (thread, host), gzip and a response size cap."""

    def __init__(self, user_agent: str = USER_AGENT, max_bytes: int = 5_000_000, clock=time.perf_counter):
        self.user_agent = user_agent
        self.max_bytes = max_bytes
        self.clock = clock
        self._local = threading.local()

    def _conn(self, scheme: str, netloc: str, timeout: float, fresh: bool = False) -> http.client.HTTPConnection:
        pool: dict[tuple[str, str], http.client.HTTPConnection] = getattr(self._local, "pool", None) or {}
        self._local.pool = pool
        key = (scheme, netloc)
        if fresh and key in pool:
            pool.pop(key).close()
        if key not in pool:
            cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
            pool[key] = cls(netloc, timeout=timeout)
        return pool[key]

    def close(self) -> None:
        for conn in (getattr(self._local, "pool", None) or {}).values():
            conn.close()
        self._local.pool = {}

    def fetch(self, url: str, headers: dict[str, str] | None = None, timeout: float = 15.0) -> Response:
        parts = urlsplit(url)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        req_headers = {"User-Agent": self.user_agent, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                       "Accept-Encoding": "gzip, deflate", "Connection": "keep-alive", **(headers or {})}
        start = self.clock()
        for attempt in (1, 2):   # a reused keep-alive socket may have been closed by the server: retry once fresh
            conn = self._conn(parts.scheme, parts.netloc, timeout, fresh=attempt == 2)
            try:
                conn.request("GET", target, headers=req_headers)
                resp = conn.getresponse()
                raw = resp.read(self.max_bytes + 1)
                hdrs = {k.lower(): v for k, v in resp.getheaders()}
                if resp.will_close:
                    self._conn(parts.scheme, parts.netloc, timeout, fresh=True)
                body = decode_body(raw[: self.max_bytes], hdrs.get("content-encoding", ""))
                return Response(url, resp.status, hdrs, body, self.clock() - start, wire_bytes=len(raw))
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError) as exc:
                if attempt == 2:
                    return Response(url, 0, elapsed=self.clock() - start, error=f"{type(exc).__name__}: {exc}")
            except (OSError, http.client.HTTPException, zlib.error) as exc:   # TimeoutError is an OSError
                self._conn(parts.scheme, parts.netloc, timeout, fresh=True)
                return Response(url, 0, elapsed=self.clock() - start, error=f"{type(exc).__name__}: {exc}")
        raise AssertionError("unreachable")
