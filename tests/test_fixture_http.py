"""The real network path: HttpFetcher against the fixture site served over HTTP/1.1 on localhost."""

import pytest

from trawl.crawler import Crawler
from trawl.fetch import HttpFetcher
from trawl.fixture import BookStore, FixtureServer, FixtureSite
from trawl.spec import Spec
from trawl.store import Store

from .conftest import BOOKS_SPEC


@pytest.fixture(scope="module")
def server():
    site = FixtureSite(BookStore(n_books=40, n_categories=4, seed=5))
    with FixtureServer(site) as srv:
        yield srv


def test_fetch_gzip_and_conditional_get(server):
    f = HttpFetcher()
    book = next(iter(server.site.store.books(1).values()))
    url = f"{server.url}catalogue/{book.slug}/index.html"
    first = f.fetch(url)
    assert first.status == 200 and first.is_html and book.title in first.text()
    assert first.headers.get("content-encoding") == "gzip" and first.wire_bytes < len(first.body)
    again = f.fetch(url, {"If-None-Match": first.headers["etag"]})
    assert again.status == 304 and again.body == b""
    f.close()


def test_redirects_are_returned_not_followed(server):
    res = HttpFetcher().fetch(server.url + "old-catalogue/")
    assert res.status == 301 and res.headers["location"] == "/catalogue/page-2.html"


def test_connection_refused_is_status_zero():
    res = HttpFetcher().fetch("http://127.0.0.1:9/")
    assert res.status == 0 and res.error


def test_end_to_end_crawl_over_http(server, tmp_path):
    spec = Spec.load(BOOKS_SPEC).with_seeds([server.url])
    spec.rate, spec.per_host, spec.concurrency = None, 4, 4
    store = Store(tmp_path / "http.db")
    report = Crawler(spec, store, HttpFetcher(user_agent=spec.user_agent)).run()
    assert report.status == "completed" and report.failed == 0
    assert {i["key"]: i["data"] for i in store.items("books")} == server.site.store.ground_truth(1)
    store.close()
