import pytest

from trawl import urls


@pytest.mark.parametrize("raw, expected", [
    ("HTTP://Example.COM:80/a/./b/../c?b=2&a=1#frag", "http://example.com/a/c?a=1&b=2"),
    ("https://example.com:443", "https://example.com/"),
    ("https://user:pw@example.com/x", "https://example.com/x"),
    ("https://example.com/p?utm_source=x&id=7&gclid=abc", "https://example.com/p?id=7"),
    ("https://example.com/%7euser/%41", "https://example.com/~user/A"),
])
def test_normalize_canonical_form(raw, expected):
    assert urls.normalize(raw) == expected


def test_normalize_resolves_relative_links_against_base():
    base = "http://shop.test/catalogue/category/books/travel_2/index.html"
    assert urls.normalize("../../../a-book_7/index.html", base) == "http://shop.test/catalogue/a-book_7/index.html"
    assert urls.normalize("page-2.html", base) == "http://shop.test/catalogue/category/books/travel_2/page-2.html"


@pytest.mark.parametrize("raw", ["mailto:a@b.c", "javascript:void(0)", "ftp://x.org/f", "", "#top"])
def test_normalize_rejects_non_http(raw):
    assert urls.normalize(raw) is None


def test_domains_assets_and_parts():
    assert urls.in_domains("http://shop.example.org/x", {"example.org"})
    assert not urls.in_domains("http://evil-example.org/x", {"example.org"})
    assert urls.looks_like_asset("http://x.org/media/cover.JPG")
    assert not urls.looks_like_asset("http://x.org/catalogue/index.html")
    assert urls.path_and_query("http://x.org/a/b?c=1") == "/a/b?c=1"
    assert urls.origin_of("http://x.org:8080/a") == "http://x.org:8080"
