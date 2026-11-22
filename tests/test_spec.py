import pytest

from trawl.dom import parse_html
from trawl.fixture import BookStore, FixtureSite
from trawl.spec import Spec, SpecError, fingerprint

from .conftest import SEED, books_spec


def minimal(**extra):
    d = {"name": "t", "seeds": ["https://example.com/"]}
    d.update(extra)
    return d


def test_books_spec_extracts_exact_ground_truth_from_a_product_page():
    store = BookStore(n_books=30, n_categories=4, seed=11)
    site = FixtureSite(store)
    spec = books_spec()
    for book in list(store.books(1).values())[:10]:
        url = f"{SEED}catalogue/{book.slug}/index.html"
        res = site.handle(f"/catalogue/{book.slug}/index.html")
        data, errors = spec.item_rule_for(url).extract(parse_html(res.body.decode()), url)
        assert errors == []
        assert data == book.scraped(store.categories[book.category])


def test_item_rule_only_matches_product_urls():
    spec = books_spec()
    assert spec.item_rule_for(f"{SEED}catalogue/some-book_12/index.html").name == "book"
    assert spec.item_rule_for(f"{SEED}catalogue/category/books/travel_2/index.html") is None
    assert spec.item_rule_for(f"{SEED}catalogue/page-2.html") is None


def test_field_types_regex_map_default_and_many():
    spec = Spec.from_dict(minimal(items=[{
        "name": "p", "url": ".", "key": "sku",
        "fields": {
            "sku": {"css": ".sku", "required": True},
            "price": {"css": ".price", "type": "money"},
            "qty": {"css": ".qty", "regex": r"(\d+) left", "type": "int"},
            "big": {"css": ".big", "type": "float"},
            "ok": {"css": ".ok", "type": "bool"},
            "size": {"css": ".size", "map": {"S": "small", "L": "large"}},
            "missing": {"css": ".nope", "default": "n/a"},
            "tags": {"css": ".tag", "many": True},
            "img": {"css": "img", "attr": "src", "type": "url"},
        }}]))
    html = ('<div class="sku"> A-1 </div><div class="price">£1,299.50</div><div class="qty">Only 7 left</div>'
            '<div class="big">approx. 12.5 kg</div><div class="ok">yes</div><div class="size">L</div>'
            '<i class="tag">x</i><i class="tag">y</i><img src="../img/p.png">')
    data, errors = spec.items[0].extract(parse_html(html), "https://example.com/shop/item/1")
    assert errors == []
    assert data == {"sku": "A-1", "price": 1299.5, "qty": 7, "big": 12.5, "ok": True, "size": "large",
                    "missing": "n/a", "tags": ["x", "y"], "img": "https://example.com/shop/img/p.png"}


def test_missing_required_field_drops_the_item_with_an_error():
    spec = Spec.from_dict(minimal(items=[{"name": "p", "url": ".", "key": "sku",
                                          "fields": {"sku": {"css": ".sku", "required": True}}}]))
    data, errors = spec.items[0].extract(parse_html("<p>nothing</p>"), "https://example.com/x")
    assert data is None and errors and "sku" in errors[0]


def test_spec_validation_collects_every_problem():
    with pytest.raises(SpecError) as exc:
        Spec.from_dict({"seeds": ["notaurl"], "politeness": {"rate": 0},
                        "follow": {"allow": ["("]},
                        "items": [{"name": "x", "key": "nope", "fields": {"a": {"css": "a[", "type": "date"}}}]})
    text = str(exc.value)
    for fragment in ("name", "seeds", "rate", "follow.allow", "items[0].fields.a", "unknown type", "key", "url"):
        assert fragment in text


def test_follow_rules_domains_and_priorities():
    spec = books_spec()
    assert spec.should_follow(f"{SEED}catalogue/page-3.html")
    assert not spec.should_follow(f"{SEED}catalogue/page-3.html?sort=price")
    assert not spec.should_follow(f"{SEED}admin/")
    assert not spec.should_follow("https://external.example.org/")
    assert not spec.should_follow(f"{SEED}media/cache/1.jpg")
    assert spec.priority_for(f"{SEED}catalogue/page-3.html") < spec.priority_for(f"{SEED}catalogue/x_1/index.html")


def test_links_respect_base_tag_and_nofollow():
    spec = Spec.from_dict(minimal())
    root = parse_html('<base href="https://cdn.example.com/d/"><a href="a.html">a</a><a href="/b" rel="nofollow">b</a>'
                      '<a href="a.html#x">dup</a><a href="mailto:x@y">m</a>')
    assert spec.links(root, "https://example.com/page") == ["https://cdn.example.com/d/a.html"]


def test_fingerprint_ignores_configured_volatile_regions():
    a = parse_html("<main><p>Same</p></main><footer>rendered 10:00</footer>")
    b = parse_html("<main><p>Same</p></main><footer>rendered 10:01</footer>")
    c = parse_html("<main><p>Different</p></main><footer>rendered 10:00</footer>")
    assert fingerprint(a) != fingerprint(b)
    assert fingerprint(a, ["footer"]) == fingerprint(b, ["footer"])
    assert fingerprint(a, ["footer"]) != fingerprint(c, ["footer"])
    assert fingerprint(parse_html("<p>a   b</p>")) == fingerprint(parse_html("<p>a b</p>"))
