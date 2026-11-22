import pytest

from trawl.web import create_app

from .conftest import books_spec, make_crawler


@pytest.fixture
def client(tmp_path, store, site, clock):
    spec = books_spec()
    make_crawler(spec, store, site, clock)[0].run()
    site.version = 2
    make_crawler(spec, store, site, clock)[0].run()
    store.commit()
    app = create_app(str(store.path))
    return app.test_client()


def test_health_and_security_headers(client):
    res = client.get("/health")
    assert res.status_code == 200 and res.json["status"] == "ok" and res.json["runs"] == 2
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in res.headers["Content-Security-Policy"]


def test_summary_and_runs(client, bookstore):
    s = client.get("/api/summary").json
    assert s["runs"] == 2 and s["items"] == len(bookstore.books(2))
    assert s["changes"] == len(bookstore.diff(1, 2)[0])
    runs = client.get("/api/runs").json["runs"]
    assert [r["id"] for r in runs] == [2, 1] and runs[0]["stats"]["status_counts"]["304"] > 0


def test_run_detail_histogram_and_throughput(client):
    d = client.get("/api/runs/1").json
    assert sum(d["latency_histogram"]["counts"]) == d["run"]["stats"]["pages"]
    assert len(d["latency_histogram"]["counts"]) == len(d["latency_histogram"]["bounds_ms"]) + 1
    assert sum(d["throughput"]) == d["run"]["stats"]["pages"]
    assert client.get("/api/runs/99").status_code == 404


def test_changes_include_price_deltas(client, bookstore):
    changes = client.get("/api/changes?run=2").json["changes"]
    changed, _ = bookstore.diff(1, 2)
    assert {c["key"] for c in changes} == changed
    priced = [c for c in changes if "price" in c["changes"]]
    assert priced
    c = priced[0]
    old, new = c["changes"]["price"]
    assert c["price_change_pct"] == round((new - old) / old * 100, 1)


def test_items_search_and_history(client, bookstore):
    changed, _ = bookstore.diff(1, 2)
    key = sorted(changed)[0]
    title = bookstore.ground_truth(2)[key]["title"]
    found = client.get("/api/items", query_string={"q": title.lower()}).json["items"]
    assert key in {i["key"] for i in found}
    hist = client.get(f"/api/items/books/{key}/history").json
    assert [v["run_id"] for v in hist["versions"]] == [1, 2]
    assert client.get("/api/items/books/nope/history").status_code == 404


@pytest.mark.parametrize("url", ["/api/runs?limit=0", "/api/runs?limit=abc", "/api/changes?run=x",
                                 "/api/items?limit=100000"])
def test_bad_parameters_are_rejected(client, url):
    res = client.get(url)
    assert res.status_code == 400 and "error" in res.json


def test_dashboard_page_and_assets_are_served(client):
    page = client.get("/")
    assert page.status_code == 200 and b"Crawl dashboard" in page.data
    for asset in ("/static/app.js", "/static/style.css", "/static/theme.js"):
        assert client.get(asset).status_code == 200
    assert client.get("/static/../app.py").status_code == 404
