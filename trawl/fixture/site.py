"""A deterministic fake bookstore with the same URL layout and markup as books.toscrape.com.

It exists so the tests, the benchmark and the dashboard demo run offline, and so change detection can be checked
against ground truth: ``version`` 2, 3... reprice and restock a seeded subset of books and remove a few, and
``BookStore.diff`` says exactly which items changed.

What it serves, like a real site would:
- ``/robots.txt`` with a ``*`` group (``/admin/`` and ``/*?sort=`` disallowed), an optional Crawl-delay and a
  ``BadBot`` group that is disallowed everywhere
- home, ``/catalogue/page-N.html``, every category with pagination, and one page per book
- product pages send ``ETag`` and ``Last-Modified`` and answer conditional requests with 304; listing pages do not,
  so the crawler has to fall back to content fingerprints for them
- a volatile footer (render time and request counter) that changes on every response
- trap links: robots-disallowed paths, an off-site link, a ``mailto:``, a redirect, and a ``rel=nofollow`` link
- optional fault injection: a deterministic subset of paths answers 503 (with Retry-After) on the first tries,
  and a per-request latency
- gzip when the client asks for it
"""

from __future__ import annotations

import gzip
import hashlib
import html
import random
import re
import threading
import zlib
from dataclasses import dataclass, replace
from email.utils import formatdate

ADJ = ["Silent", "Hidden", "Broken", "Golden", "Last", "Wild", "Forgotten", "Crimson", "Little", "Secret", "Distant",
       "Burning", "Quiet", "Endless", "Northern", "Paper", "Glass", "Midnight", "Hollow", "Bright"]
NOUN = ["Orchard", "River", "Kingdom", "Letters", "Garden", "Machine", "Harbor", "Mountain", "Archive", "Season",
        "Lantern", "Compass", "Island", "Engine", "Library", "Promise", "Winter", "Theory", "Atlas", "Signal"]
TAIL = ["", "", "", ": A Novel", " (Book 1)", " and Other Stories", ": Notes from the Field", " (Revised Edition)"]
CATEGORIES = ["Travel", "Mystery", "Historical Fiction", "Sequential Art", "Classics", "Philosophy", "Romance",
              "Womens Fiction", "Fiction", "Childrens", "Religion", "Nonfiction", "Music", "Default", "Science Fiction",
              "Sports and Games", "Add a comment", "Fantasy", "New Adult", "Young Adult", "Science", "Poetry",
              "Paranormal", "Art", "Psychology", "Autobiography", "Parenting", "Adult Fiction", "Humor", "Horror",
              "History", "Food and Drink", "Christian Fiction", "Business", "Biography", "Thriller", "Contemporary",
              "Spirituality", "Academic", "Self Help", "Historical", "Christian", "Suspense", "Short Stories",
              "Novels", "Health", "Politics", "Cultural", "Erotica", "Crime"]
RATING_WORDS = ["One", "Two", "Three", "Four", "Five"]
PER_PAGE = 20
EPOCH = 1_793_000_000  # fixed base timestamp (Oct 2026) so Last-Modified is deterministic


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


@dataclass(frozen=True)
class Book:
    id: int
    title: str
    category: int          # index into the category list
    price_pence: int
    rating: int
    stock: int
    upc: str
    description: str
    changed_version: int = 1

    @property
    def slug(self) -> str:
        return f"{slugify(self.title)}_{self.id}"

    @property
    def price(self) -> float:
        return self.price_pence / 100

    def scraped(self, category_name: str) -> dict:
        """The record a correct scraper should extract with specs/books.yaml (ground truth for tests)."""
        return {"upc": self.upc, "title": self.title, "price": self.price, "rating": self.rating,
                "stock": self.stock, "in_stock": self.stock > 0, "category": category_name,
                "description": self.description}


class BookStore:
    def __init__(self, n_books: int = 1000, n_categories: int = 50, seed: int = 7, change_rate: float = 0.05,
                 remove_rate: float = 0.005):
        if not 1 <= n_categories <= len(CATEGORIES):
            raise ValueError(f"n_categories must be 1..{len(CATEGORIES)}")
        self.seed = seed
        self.change_rate = change_rate
        self.remove_rate = remove_rate
        self.categories = CATEGORIES[:n_categories]
        rng = random.Random(seed)
        weights = [1 / (i + 1) ** 0.6 for i in range(n_categories)]   # a few big categories, a long tail
        books = []
        for i in range(1, n_books + 1):
            title = f"The {rng.choice(ADJ)} {rng.choice(NOUN)}{rng.choice(TAIL)}"
            if rng.random() < 0.3:
                title = f"{rng.choice(ADJ)} {rng.choice(NOUN)} #{rng.randint(1, 9)}"
            books.append(Book(
                id=i, title=title, category=rng.choices(range(n_categories), weights)[0],
                price_pence=rng.randint(1000, 5999), rating=rng.randint(1, 5), stock=rng.randint(1, 22),
                upc=hashlib.sha1(f"{seed}-{i}".encode()).hexdigest()[:16],
                description=f"A {rng.choice(ADJ).lower()} story about a {rng.choice(NOUN).lower()} "
                            f"and the people who {rng.choice(['keep', 'lose', 'find', 'guard'])} it."))
        self._versions: dict[int, dict[int, Book]] = {1: {b.id: b for b in books}}

    def books(self, version: int = 1) -> dict[int, Book]:
        """Catalogue state at ``version``. Each version applies seeded price/stock changes and removals."""
        if version < 1:
            raise ValueError("version starts at 1")
        if version not in self._versions:
            prev = self.books(version - 1)
            rng = random.Random(self.seed * 1000 + version)
            nxt: dict[int, Book] = {}
            for bid, book in prev.items():
                roll = rng.random()
                if roll < self.remove_rate:
                    continue
                if roll < self.remove_rate + self.change_rate:
                    factor = rng.choice([0.8, 0.85, 0.9, 1.1, 1.15, 1.25])
                    stock = book.stock if rng.random() < 0.6 else max(0, book.stock - rng.randint(1, 8))
                    book = replace(book, price_pence=max(500, int(book.price_pence * factor)), stock=stock,
                                   changed_version=version)
                nxt[bid] = book
            self._versions[version] = nxt
        return self._versions[version]

    def ground_truth(self, version: int = 1) -> dict[str, dict]:
        return {b.upc: b.scraped(self.categories[b.category]) for b in self.books(version).values()}

    def diff(self, old: int, new: int) -> tuple[set[str], set[str]]:
        """``(changed_upcs, removed_upcs)`` between two versions."""
        a, b = self.ground_truth(old), self.ground_truth(new)
        changed = {k for k in a.keys() & b.keys() if a[k] != b[k]}
        return changed, set(a) - set(b)


@dataclass
class SiteResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class FixtureSite:
    """Renders ``BookStore`` as HTML pages. ``handle()`` is transport-free; ``server.py`` puts it behind HTTP."""

    def __init__(self, store: BookStore | None = None, version: int = 1, crawl_delay: float | None = None,
                 flaky: float = 0.0, flaky_failures: int = 1, retry_after: int | None = 1, robots_status: int = 200):
        self.store = store or BookStore()
        self.version = version
        self.crawl_delay = crawl_delay
        self.flaky = flaky
        self.flaky_failures = flaky_failures
        self.retry_after = retry_after
        self.robots_status = robots_status
        self._lock = threading.Lock()
        self._counter = 0
        self._failures: dict[str, int] = {}
        self.requests: list[str] = []

    # --- helpers ---------------------------------------------------------------------------------------------
    def _books(self) -> list[Book]:
        return sorted(self.store.books(self.version).values(), key=lambda b: b.id)

    def _cat_slug(self, idx: int) -> str:
        return f"{slugify(self.store.categories[idx])}_{idx + 2}"

    def _footer(self) -> str:
        with self._lock:
            self._counter += 1
            n = self._counter
        return (f'<footer><p class="generated">Rendered {formatdate(EPOCH + n, usegmt=True)} '
                f"&middot; request {n}</p></footer>")

    def _page(self, title: str, body: str, sidebar: bool = True) -> str:
        side = ""
        if sidebar:
            links = "".join(f'<li><a href="/catalogue/category/books/{self._cat_slug(i)}/index.html">{html.escape(c)}'
                            f"</a></li>" for i, c in enumerate(self.store.categories))
            side = (f'<aside class="sidebar col-sm-4"><div class="side_categories"><ul class="nav nav-list"><li>'
                    f'<a href="/catalogue/category/books_1/index.html">Books</a><ul>{links}</ul></li></ul></div>'
                    f"</aside>")
        return (f"<!DOCTYPE html><html lang=\"en-us\"><head><meta charset=\"utf-8\"><title>{html.escape(title)} | "
                f"Books to Scrape - Sandbox</title><link rel=\"stylesheet\" href=\"/static/style.css\"></head><body>"
                f'<header class="header"><a href="/index.html">Books to Scrape</a> We love being scraped!</header>'
                f'<div class="page_inner">{side}<div class="col-sm-8">{body}</div></div>'
                f"{{FOOTER}}</body></html>")

    def _pod(self, book: Book, href: str) -> str:
        stock = '<i class="icon-ok"></i> In stock' if book.stock else '<i class="icon-remove"></i> Out of stock'
        return (f'<li class="col-xs-6"><article class="product_pod"><div class="image_container"><a href="{href}">'
                f'<img src="/media/cache/{book.id}.jpg" alt="{html.escape(book.title)}" class="thumbnail"></a></div>'
                f'<p class="star-rating {RATING_WORDS[book.rating - 1]}"></p>'
                f'<h3><a href="{href}" title="{html.escape(book.title)}">{html.escape(book.title[:40])}</a></h3>'
                f'<div class="product_price"><p class="price_color">£{book.price:.2f}</p>'
                f'<p class="instock availability">{stock}</p></div></article></li>')

    def _listing(self, title: str, books: list[Book], page: int, href_prefix: str, next_fmt: str,
                 prev_fmt: str) -> str | None:
        pages = max(1, -(-len(books) // PER_PAGE))
        if page < 1 or page > pages:
            return None
        chunk = books[(page - 1) * PER_PAGE: page * PER_PAGE]
        pods = "".join(self._pod(b, f"{href_prefix}{b.slug}/index.html") for b in chunk)
        pager = '<ul class="pager">'
        if page > 1:
            pager += f'<li class="previous"><a href="{prev_fmt.format(page - 1)}">previous</a></li>'
        pager += f'<li class="current">Page {page} of {pages}</li>'
        if page < pages:
            pager += f'<li class="next"><a href="{next_fmt.format(page + 1)}">next</a></li>'
        pager += "</ul>"
        body = (f'<div class="page-header"><h1>{html.escape(title)}</h1></div><form class="form-horizontal">'
                f"<strong>{len(books)}</strong> results.</form><ol class=\"row\">{pods}</ol>{pager}"
                f'<p class="sort"><a href="?sort=price">Sort by price</a></p>')
        return self._page(title, body)

    def _product(self, book: Book) -> str:
        cat = self.store.categories[book.category]
        stock = f"In stock ({book.stock} available)" if book.stock else "Out of stock"
        rows = [("UPC", book.upc), ("Product Type", "Books"), ("Price (excl. tax)", f"£{book.price:.2f}"),
                ("Price (incl. tax)", f"£{book.price:.2f}"), ("Tax", "£0.00"), ("Availability", stock),
                ("Number of reviews", "0")]
        table = "".join(f"<tr><th>{k}</th><td>{html.escape(v)}</td></tr>" for k, v in rows)
        body = (f'<ul class="breadcrumb"><li><a href="../../index.html">Home</a></li>'
                f'<li><a href="../category/books_1/index.html">Books</a></li>'
                f'<li><a href="../category/books/{self._cat_slug(book.category)}/index.html">{html.escape(cat)}</a></li>'
                f'<li class="active">{html.escape(book.title)}</li></ul>'
                f'<article class="product_page"><div class="row"><div class="col-sm-6"><div id="product_gallery">'
                f'<img src="../../media/cache/{book.id}.jpg" alt="{html.escape(book.title)}"></div></div>'
                f'<div class="col-sm-6 product_main"><h1>{html.escape(book.title)}</h1>'
                f'<p class="price_color">£{book.price:.2f}</p>'
                f'<p class="instock availability"><i class="icon-ok"></i> {stock}</p>'
                f'<p class="star-rating {RATING_WORDS[book.rating - 1]}"><i class="icon-star"></i></p></div></div>'
                f'<div id="product_description" class="sub-header"><h2>Product Description</h2></div>'
                f"<p>{html.escape(book.description)}</p>"
                f'<div class="sub-header"><h2>Product Information</h2></div>'
                f'<table class="table table-striped">{table}</table></article>')
        return self._page(book.title, body, sidebar=False)

    def _home_extras(self) -> str:
        return ('<div class="extras"><a href="/admin/">Admin</a> <a href="https://external.example.org/">Partner</a> '
                '<a href="mailto:shop@example.org">Mail</a> <a href="/old-catalogue/">Old catalogue</a> '
                '<a href="/cart/" rel="nofollow">Cart</a> <a href="/catalogue/missing-book_99999/index.html">'
                "Gone</a></div>")

    def robots_txt(self) -> str:
        lines = ["User-agent: *", "Disallow: /admin/", "Disallow: /*?sort="]
        if self.crawl_delay:
            lines.append(f"Crawl-delay: {self.crawl_delay:g}")
        lines += ["", "User-agent: BadBot", "Disallow: /", "", "Sitemap: /sitemap.xml"]
        return "\n".join(lines) + "\n"

    # --- routing ---------------------------------------------------------------------------------------------
    def render(self, path: str) -> tuple[int, str, str, dict[str, str]]:
        """``(status, content_type, body, extra_headers)`` for a path, before conditional/gzip handling."""
        path, _, _query = path.partition("?")
        books = self._books()
        if path == "/robots.txt":
            if self.robots_status != 200:
                return self.robots_status, "text/plain", "", {}
            return 200, "text/plain; charset=utf-8", self.robots_txt(), {}
        if path in ("/", "/index.html"):
            listing = self._listing("All products", books, 1, "catalogue/", "catalogue/page-{}.html", "")
            return 200, "text/html; charset=utf-8", (listing or "").replace("{FOOTER}", self._home_extras() + "{FOOTER}"), {}
        if path == "/old-catalogue/":
            return 301, "text/html", "", {"Location": "/catalogue/page-2.html"}
        m = re.fullmatch(r"/catalogue/page-(\d+)\.html", path)
        if m:
            listing = self._listing("All products", books, int(m.group(1)), "", "page-{}.html", "page-{}.html")
            return (200, "text/html; charset=utf-8", listing, {}) if listing else (404, "text/html", "Not found", {})
        m = re.fullmatch(r"/catalogue/category/books_1/(index|page-(\d+))\.html", path)
        if m:
            listing = self._listing("Books", books, int(m.group(2) or 1), "../../", "page-{}.html", "page-{}.html")
            return (200, "text/html; charset=utf-8", listing, {}) if listing else (404, "text/html", "Not found", {})
        m = re.fullmatch(r"/catalogue/category/books/([a-z0-9\-]+)_(\d+)/(index|page-(\d+))\.html", path)
        if m:
            idx = int(m.group(2)) - 2
            if 0 <= idx < len(self.store.categories) and self._cat_slug(idx) == f"{m.group(1)}_{m.group(2)}":
                in_cat = [b for b in books if b.category == idx]
                listing = self._listing(self.store.categories[idx], in_cat, int(m.group(4) or 1), "../../../",
                                        "page-{}.html", "page-{}.html")
                if listing:
                    return 200, "text/html; charset=utf-8", listing, {}
            return 404, "text/html", "Not found", {}
        m = re.fullmatch(r"/catalogue/([a-z0-9\-]+)_(\d+)/index\.html", path)
        if m:
            book = self.store.books(self.version).get(int(m.group(2)))
            if book is None or book.slug != f"{m.group(1)}_{m.group(2)}":
                return 404, "text/html", "<h1>Not found</h1>", {}
            return 200, "text/html; charset=utf-8", self._product(book), {
                "Last-Modified": formatdate(EPOCH + book.changed_version * 86400, usegmt=True)}
        return 404, "text/html", "<h1>Not found</h1>", {}

    def handle(self, path: str, headers: dict[str, str] | None = None) -> SiteResponse:
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        with self._lock:
            self.requests.append(path)
            if self.flaky and path != "/robots.txt" and (zlib.crc32(path.encode()) % 1000) < self.flaky * 1000:
                seen = self._failures.get(path, 0)
                if seen < self.flaky_failures:
                    self._failures[path] = seen + 1
                    extra = {"Retry-After": str(self.retry_after)} if self.retry_after is not None else {}
                    return SiteResponse(503, {"Content-Type": "text/plain", **extra}, b"Service Unavailable")
        status, ctype, body, extra = self.render(path)
        out_headers = {"Content-Type": ctype, **extra}
        if "Last-Modified" in extra:   # product pages: strong validator over the stable part of the page
            etag = '"' + hashlib.sha1(body.encode()).hexdigest()[:20] + '"'
            out_headers["ETag"] = etag
            inm = headers.get("if-none-match")
            ims = headers.get("if-modified-since")
            if (inm and etag in [t.strip() for t in inm.split(",")]) or (not inm and ims == extra["Last-Modified"]):
                return SiteResponse(304, {"ETag": etag, "Last-Modified": extra["Last-Modified"]}, b"")
        raw = body.replace("{FOOTER}", self._footer()).encode("utf-8")
        if "gzip" in headers.get("accept-encoding", "") and len(raw) > 1024:
            raw = gzip.compress(raw, compresslevel=5)
            out_headers["Content-Encoding"] = "gzip"
        return SiteResponse(status, out_headers, raw)
