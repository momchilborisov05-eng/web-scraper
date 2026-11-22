"""Offline fixture: a fake books.toscrape.com, reachable in-process (tests) or over real HTTP (benchmark, demo)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from ..fetch import Response, decode_body
from .site import BookStore, FixtureSite

__all__ = ["BookStore", "FixtureSite", "FixtureFetcher", "FixtureServer"]


class FixtureFetcher:
    """A ``Fetcher`` that calls the site directly: no sockets, no threads, optional simulated latency."""

    def __init__(self, site: FixtureSite, latency: float = 0.0, sleep: Callable[[float], None] = time.sleep,
                 hosts: set[str] | None = None):
        self.site = site
        self.latency = latency
        self.sleep = sleep
        self.hosts = hosts   # None: answer for any host
        self.calls: list[tuple[str, dict[str, str]]] = []

    def fetch(self, url: str, headers: dict[str, str] | None = None, timeout: float = 15.0) -> Response:
        parts = urlsplit(url)
        self.calls.append((url, dict(headers or {})))
        if self.hosts is not None and parts.netloc not in self.hosts:
            return Response(url, 0, error="connection refused")
        if self.latency:
            self.sleep(self.latency)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        res = self.site.handle(target, headers)
        hdrs = {k.lower(): v for k, v in res.headers.items()}
        body = decode_body(res.body, hdrs.get("content-encoding", ""))
        return Response(url, res.status, hdrs, body, self.latency, wire_bytes=len(res.body))


class FixtureServer:
    """Serves a ``FixtureSite`` over HTTP/1.1 (keep-alive) on 127.0.0.1 in a background thread."""

    def __init__(self, site: FixtureSite, port: int = 0, latency: float = 0.0, host: str = "127.0.0.1"):
        site_ref, latency_ref = site, latency

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802 - http.server naming
                if latency_ref:
                    time.sleep(latency_ref)
                res = site_ref.handle(self.path, dict(self.headers.items()))
                self.send_response(res.status)
                for k, v in res.headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(res.body)))
                self.end_headers()
                self.wfile.write(res.body)

            def log_message(self, *args) -> None:
                pass

        self.site = site
        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="fixture-http", daemon=True)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/"

    def __enter__(self) -> FixtureServer:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
