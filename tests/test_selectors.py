import pytest

from trawl.css import SelectorError, nth_matches, parse_nth, select, select_one
from trawl.dom import parse_html

HTML = """<!doctype html><html><body>
<div id="main" class="wrap page">
  <h1 class="title">Books</h1>
  <ul class="list">
    <li class="item first"><a href="/a" data-k="x">A</a></li>
    <li class="item"><a href="/b" rel="nofollow">B</a></li>
    <li class="item special"><a href="https://ext.org/c">C</a></li>
    <li class="item"><span>no link</span></li>
  </ul>
  <p>intro<p>second paragraph
  <table><tr><th>UPC</th><td>abc123</td></tr><tr><th>Tax</th><td>0</td></tr></table>
  <img src="/x.png"><br>
  <div class="empty"></div>
  <script>var hidden = "<p>not a tag</p>";</script>
</div></body></html>"""


@pytest.fixture(scope="module")
def doc():
    return parse_html(HTML)


def texts(nodes):
    return [n.text() for n in nodes]


def test_tree_builder_handles_implied_end_tags_and_void_elements(doc):
    ps = select(doc, "p")
    assert texts(ps) == ["intro", "second paragraph"]          # <p> implicitly closed by the next <p>
    img = select_one(doc, "img")
    assert img.children == [] and img.parent.get("id") == "main"
    assert "hidden" not in select_one(doc, "#main").text()      # script content is not page text
    assert select(doc, "script p") == []                        # and "<p>" inside a script is not a tag
    assert len(select(doc, "li")) == 4


@pytest.mark.parametrize("selector, expected", [
    ("#main > h1.title", ["Books"]),
    ("ul.list li.item a", ["A", "B", "C"]),
    ("li.first + li a", ["B"]),
    ("li.first ~ li.special a", ["C"]),
    ("a[href^='https']", ["C"]),
    ("a[href$=b]", ["B"]),
    ("a[href*=xt]", ["C"]),
    ("a[rel~=nofollow]", ["B"]),
    ("a[data-k='X' i]", ["A"]),
    ("li:nth-child(2n+1) > *", ["A", "C"]),
    ("li:nth-child(even) > *", ["B", "no link"]),
    ("li:last-child span", ["no link"]),
    ("li:not(.item.special) > a", ["A", "B"]),
    ("th:contains('UPC') + td", ["abc123"]),
    ("h1, li.special a", ["Books", "C"]),
])
def test_selector_matches(doc, selector, expected):
    assert texts(select(doc, selector)) == expected


def test_results_are_in_document_order_without_duplicates(doc):
    nodes = select(doc, "div *, li")
    assert len(nodes) == len({id(n) for n in nodes})
    order = {id(n): i for i, n in enumerate(doc.iter())}
    positions = [order[id(n)] for n in nodes]
    assert positions == sorted(positions)


def test_empty_and_only_child(doc):
    assert [n.get("class") for n in select(doc, "div:empty")] == ["empty"]
    assert texts(select(doc, "li > a:only-child")) == ["A", "B", "C"]


@pytest.mark.parametrize("expr, a, b", [("odd", 2, 1), ("even", 2, 0), ("3", 0, 3), ("-n+3", -1, 3), ("2n", 2, 0),
                                        ("n", 1, 0)])
def test_parse_nth(expr, a, b):
    assert parse_nth(expr) == (a, b)


def test_nth_matches_first_three():
    a, b = parse_nth("-n+3")
    assert [k for k in range(1, 7) if nth_matches(a, b, k)] == [1, 2, 3]


@pytest.mark.parametrize("bad", ["", "div >", "a[href", "p:hover", "::before", "a[href=='x']", "li:nth-child(x)"])
def test_invalid_selectors_raise(bad):
    with pytest.raises(SelectorError):
        select(parse_html("<p>x</p>"), bad)
