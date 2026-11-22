"""URL canonicalization, so the frontier sees one key per page.

Two URLs that a server will answer with the same page should normalize to the same string, otherwise the crawler
fetches the page twice and the dedup set grows without bound. The rules follow RFC 3986 section 6 (syntax-based
normalization) plus a few safe, scheme-based ones:

- lowercase scheme and host, drop the default port (80 / 443), drop userinfo and the fragment
- resolve ``.`` and ``..`` path segments, empty path becomes ``/``
- uppercase percent-escapes and decode the ones that encode unreserved characters (``%7E`` -> ``~``)
- drop tracking parameters (``utm_*``, ``gclid``, ``fbclid``...) and sort the remaining query parameters
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit, urlunsplit

DEFAULT_PORTS = {"http": 80, "https": 443}
TRACKING_PARAMS = frozenset({"gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "_ga", "yclid", "ref_src"})
SKIP_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar", ".mp4",
                   ".mp3", ".css", ".js", ".woff", ".woff2", ".exe", ".dmg")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_PCT = re.compile(r"%([0-9A-Fa-f]{2})")


def _normalize_escapes(part: str) -> str:
    def fix(m: re.Match[str]) -> str:
        ch = chr(int(m.group(1), 16))
        return ch if ch in _UNRESERVED else "%" + m.group(1).upper()

    return _PCT.sub(fix, part)


def _remove_dot_segments(path: str) -> str:
    """RFC 3986 section 5.2.4, written as a stack walk over the segments."""
    if not path:
        return "/"
    out: list[str] = []
    segments = path.split("/")
    for i, seg in enumerate(segments):
        last = i == len(segments) - 1
        if seg == ".":
            if last:
                out.append("")
        elif seg == "..":
            if len(out) > 1:
                out.pop()
            if last:
                out.append("")
        else:
            out.append(seg)
    result = "/".join(out)
    return result if result.startswith("/") else "/" + result


def _clean_query(query: str) -> str:
    if not query:
        return ""
    pairs = []
    for item in query.split("&"):
        if not item:
            continue
        key = item.split("=", 1)[0]
        if key.lower().startswith("utm_") or key.lower() in TRACKING_PARAMS:
            continue
        pairs.append(_normalize_escapes(item))
    return "&".join(sorted(pairs))


def normalize(url: str, base: str | None = None) -> str | None:
    """Return the canonical form of ``url`` (resolved against ``base``), or None if it is not crawlable http(s)."""
    url = url.strip()
    if not url or url.startswith(("javascript:", "mailto:", "tel:", "data:")):
        return None
    if base:
        url = urljoin(base, url)
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or not parts.hostname:
        return None
    host = parts.hostname.lower().rstrip(".")
    if port and port != DEFAULT_PORTS[scheme]:
        host = f"{host}:{port}"
    path = _remove_dot_segments(_normalize_escapes(parts.path))
    return urlunsplit((scheme, host, path, _clean_query(parts.query), ""))


def host_of(url: str) -> str:
    """``host[:port]`` of an already normalized URL: the unit of politeness."""
    return urlsplit(url).netloc


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def path_and_query(url: str) -> str:
    parts = urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def looks_like_asset(url: str) -> bool:
    return urlsplit(url).path.lower().endswith(SKIP_EXTENSIONS)


def in_domains(url: str, domains: set[str] | frozenset[str]) -> bool:
    """True if the URL's host is one of ``domains`` or a subdomain of one (``www.x.com`` is in ``x.com``)."""
    host = urlsplit(url).hostname or ""
    return any(host == d or host.endswith("." + d) for d in domains)
