import pytest

from trawl.robots import Robots, wildcard_match

ROBOTS = """
# comment line
User-agent: *
Disallow: /private/
Allow: /private/public/
Disallow: /*.pdf$
Disallow: /search?q=
Crawl-delay: 2

User-agent: TrawlBot
User-agent: OtherBot
Disallow: /no-trawl/
Allow: /no-trawl/ok.html

User-agent: trawlbot
Crawl-delay: 5

Sitemap: https://example.com/sitemap.xml
"""


@pytest.mark.parametrize("pattern, path, ok", [
    ("/a", "/abc", True), ("/a$", "/a", True), ("/a$", "/ab", False), ("/*.pdf$", "/x/y.pdf", True),
    ("/*.pdf$", "/x/y.pdf?z", False), ("/a*b*c", "/a--b--c-tail", True), ("/a*b*c", "/a--c--b", False),
    ("*", "/anything", True),
])
def test_wildcard_match(pattern, path, ok):
    assert wildcard_match(pattern, path) is ok


def test_star_group_longest_match_and_allow_wins_ties():
    r = Robots.parse(ROBOTS)
    assert not r.can_fetch("SomeBot/2.0", "/private/x")
    assert r.can_fetch("SomeBot/2.0", "/private/public/x")
    assert not r.can_fetch("SomeBot", "/files/report.pdf")
    assert r.can_fetch("SomeBot", "/files/report.pdf?download=1")
    assert not r.can_fetch("SomeBot", "/search?q=books")
    tie = Robots.parse("User-agent: *\nDisallow: /page\nAllow: /page\n")
    assert tie.can_fetch("X", "/page")


def test_specific_groups_are_merged_and_replace_star_group():
    r = Robots.parse(ROBOTS)
    # TrawlBot matches its own (merged) groups only: /private/ is not disallowed for it
    assert r.can_fetch("TrawlBot/1.0 (+https://x)", "/private/x")
    assert not r.can_fetch("TrawlBot/1.0", "/no-trawl/x")
    assert r.can_fetch("TrawlBot/1.0", "/no-trawl/ok.html")
    assert r.crawl_delay("TrawlBot/1.0") == 5
    assert r.crawl_delay("Anyone") == 2
    assert r.sitemaps == ["https://example.com/sitemap.xml"]


def test_robots_txt_itself_is_always_allowed_and_percent_encoding_is_normalized():
    r = Robots.parse("User-agent: *\nDisallow: /\n")
    assert r.can_fetch("X", "/robots.txt")
    assert not r.can_fetch("X", "/")
    enc = Robots.parse("User-agent: *\nDisallow: /caf%C3%A9/\n")
    assert not enc.can_fetch("X", "/café/menu")


@pytest.mark.parametrize("status, allowed", [(404, True), (403, True), (410, True), (500, False), (503, False),
                                             (0, False)])
def test_rfc9309_status_rules(status, allowed):
    assert Robots.from_status(status, "").can_fetch("X", "/anything") is allowed


def test_empty_disallow_and_rules_outside_groups_are_ignored():
    r = Robots.parse("Disallow: /orphan/\nUser-agent: *\nDisallow:\n")
    assert r.can_fetch("X", "/orphan/page")
    assert r.can_fetch("X", "/anything")
