# trawl benchmark

Measured 2026-11-20 on Windows 11 (AMD64), Python 3.12.3, trawl 2.0.0. Fixture bookstore with 1000 books (1189 pages incl. listings), served over HTTP on 127.0.0.1 with 20.0 ms simulated server latency per request. Rate limiting off for the throughput runs (it is a local fixture); politeness is measured separately below.

Reproduce: `python scripts/benchmark.py`

## Cold crawl throughput

| Workers | Pages | Seconds | Pages/s | p50 ms | p95 ms | Items exact |
|---:|---:|---:|---:|---:|---:|:---:|
| 1 | 1189 | 29.0 | 41.0 | 21.4 | 22.6 | yes |
| 4 | 1189 | 9.4 | 126.5 | 24.5 | 32.6 | yes |
| 8 | 1189 | 8.23 | 144.5 | 33.0 | 58.5 | yes |
| 16 | 1189 | 4.64 | 256.1 | 40.8 | 64.7 | yes |

## Re-crawl and change detection (16 workers)

| Run | Pages | Seconds | Bytes on wire | Notes |
|---|---:|---:|---:|---|
| Cold | 1189 | 4.56 | 1,219,599 | first visit |
| Re-crawl, nothing changed | 1189 | 2.69 | 441,392 | 1000/1000 product pages 304, 188 listings unchanged by fingerprint, 0 false changes |
| Re-crawl after site change | 1185 | 4.07 | | 46 items repriced/restocked, 4 removed |

- Bytes saved by conditional requests on an unchanged site: **63.8%**
- Changed items: precision **1.0**, recall **1.0** (46 of 46)
- Removed items detected: 4 of 4 (exact set: True)
- Stored items equal the ground truth after the change: True

## Politeness

Gaps between consecutive page requests to the one host, measured where the request leaves the crawler (client) and where the server receives it (server). The server-side minimum can dip slightly below the target because arrival time includes per-request network/thread jitter; the mean rate is the long-run figure.

| Limit | Requests | Target gap | Min gap (client) | Min gap (server) | Observed rate (server) |
|---|---:|---:|---:|---:|---:|
| Token bucket 5.0/s, 4 workers | 40 | 0.2 s | 0.1999 s | 0.1784 s | 5.0/s |
| robots.txt Crawl-delay 0.5 s, 4 workers, rate off | 10 | 0.5 s | 0.5001 s | 0.4993 s | 1.999/s |

## Resilience

5% of URLs answer 503 twice before succeeding: 134 injected errors, 134 retries, 0 failed pages, every item extracted correctly: True.

## Hand-written components (best of 3)

| Operation | Time |
|---|---:|
| Parse a product page (1.7 KB) into a DOM | 1.845 ms |
| Extract 8 fields with CSS rules | 0.895 ms |
| Extract + normalize all links of a listing page | 2.822 ms |
| CSS selector over a listing page | 0.628 ms |
| robots.txt `can_fetch` against 101 wildcard rules | 341.95 us |
| Frontier push or pop (10k URLs, 50 hosts) | 17.55 us |
