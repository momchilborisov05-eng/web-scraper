"""Declarative crawl specs (YAML or JSON): what to crawl, how politely, which links to follow and what to extract.

A spec is validated up front and every problem is reported at once (bad regex, unknown type, invalid CSS selector,
missing key field), so a typo fails before the first request instead of after an hour of crawling.
See ``specs/books.yaml`` for a complete example.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import urls
from .css import SelectorError, compile_selector, select
from .dom import SKIP_TEXT, Node

TYPES = ("str", "int", "float", "money", "bool", "url")
_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?|-?\.\d+")


class SpecError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("invalid spec:\n  - " + "\n  - ".join(problems))
        self.problems = problems


def _to_number(raw: str) -> float | None:
    m = _NUMBER.search(raw.replace(" ", " "))
    return float(m.group(0).replace(",", "")) if m else None


@dataclass
class FieldRule:
    name: str
    css: str
    attr: str = "text"            # "text", "html-free text", or an attribute name such as "href" / "class"
    regex: re.Pattern[str] | None = None
    type: str = "str"
    map: dict[str, Any] | None = None
    many: bool = False
    default: Any = None
    required: bool = False

    def _raw(self, node: Node) -> str | None:
        return node.text() if self.attr == "text" else node.get(self.attr)

    def _convert(self, raw: str | None, base_url: str) -> Any:
        if raw is None:
            return None
        if self.regex is not None:
            m = self.regex.search(raw)
            if not m:
                return None
            raw = m.group(1) if m.groups() else m.group(0)
        raw = raw.strip()
        if self.map is not None:
            if raw not in self.map:
                return None
            value: Any = self.map[raw]
            if not isinstance(value, str):
                return value
            raw = value
        if self.type == "str":
            return raw or None
        if self.type in ("int", "float", "money"):
            num = _to_number(raw)
            if num is None:
                return None
            return int(num) if self.type == "int" else round(num, 2) if self.type == "money" else num
        if self.type == "bool":
            return raw.lower() not in ("", "0", "false", "no", "off", "out of stock")
        if self.type == "url":
            return urls.normalize(raw, base_url) or None
        raise AssertionError(self.type)

    def extract(self, root: Node, base_url: str) -> Any:
        nodes = select(root, self.css)
        if self.many:
            values = [v for v in (self._convert(self._raw(n), base_url) for n in nodes) if v is not None]
            return values if values else (self.default if self.default is not None else [])
        for node in nodes:
            value = self._convert(self._raw(node), base_url)
            if value is not None:
                return value
        return self.default


@dataclass
class ItemRule:
    name: str
    url: re.Pattern[str]
    key: str
    fields: list[FieldRule]

    def extract(self, root: Node, url: str) -> tuple[dict[str, Any] | None, list[str]]:
        data = {f.name: f.extract(root, url) for f in self.fields}
        missing = [f.name for f in self.fields if f.required and data[f.name] in (None, [], "")]
        if missing:
            return None, [f"{url}: missing required field(s) {', '.join(missing)}"]
        if data.get(self.key) in (None, ""):
            return None, [f"{url}: empty key field {self.key!r}"]
        return data, []


@dataclass
class Spec:
    name: str
    seeds: list[str]
    allowed_domains: frozenset[str]
    user_agent: str = "TrawlBot/1.0"
    rate: float | None = 1.0
    burst: float = 1.0
    per_host: int = 1
    concurrency: int = 4
    max_pages: int = 5000
    max_depth: int = 20
    timeout: float = 15.0
    max_attempts: int = 4
    backoff_base: float = 0.5
    backoff_cap: float = 30.0
    link_selector: str = "a[href]"
    follow_allow: list[re.Pattern[str]] = field(default_factory=list)
    follow_deny: list[re.Pattern[str]] = field(default_factory=list)
    priorities: list[tuple[re.Pattern[str], int]] = field(default_factory=list)
    items: list[ItemRule] = field(default_factory=list)
    hash_ignore: list[str] = field(default_factory=list)

    # --- loading ---------------------------------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> Spec:
        text = Path(path).read_text(encoding="utf-8")
        data = json.loads(text) if str(path).endswith(".json") else yaml.safe_load(text)
        return cls.from_dict(data or {})

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Spec:
        problems: list[str] = []

        def regex(pattern: Any, where: str) -> re.Pattern[str]:
            try:
                return re.compile(str(pattern))
            except re.error as e:
                problems.append(f"{where}: bad regex {pattern!r} ({e})")
                return re.compile(r"(?!)")

        def css(selector: Any, where: str) -> str:
            try:
                compile_selector(str(selector))
            except SelectorError as e:
                problems.append(f"{where}: {e}")
            return str(selector)

        name = str(d.get("name") or "").strip()
        if not name:
            problems.append("name: required")
        seeds_in = d.get("seeds") or []
        seeds = [s for s in (urls.normalize(str(x)) for x in seeds_in) if s]
        if not seeds:
            problems.append("seeds: at least one http(s) URL is required")
        domains = frozenset(str(x).lower() for x in d.get("allowed_domains") or []) or frozenset(
            urls.urlsplit(s).hostname or "" for s in seeds)

        pol = d.get("politeness") or {}
        crawl = d.get("crawl") or {}
        rate = pol.get("rate", 1.0)
        if rate is not None and float(rate) <= 0:
            problems.append("politeness.rate: must be > 0, or null for no limit")

        follow = d.get("follow") or {}
        follow_allow = [regex(p, "follow.allow") for p in follow.get("allow") or []]
        follow_deny = [regex(p, "follow.deny") for p in follow.get("deny") or []]
        priorities = [(regex(p.get("pattern", ""), "priority"), int(p.get("priority", 0)))
                      for p in d.get("priority") or []]

        items: list[ItemRule] = []
        for i, item in enumerate(d.get("items") or []):
            where = f"items[{i}]"
            fields: list[FieldRule] = []
            for fname, f in (item.get("fields") or {}).items():
                fwhere = f"{where}.fields.{fname}"
                if isinstance(f, str):
                    f = {"css": f}
                if "css" not in f:
                    problems.append(f"{fwhere}: css selector required")
                    continue
                ftype = f.get("type", "str")
                if ftype not in TYPES:
                    problems.append(f"{fwhere}: unknown type {ftype!r} (use one of {', '.join(TYPES)})")
                fields.append(FieldRule(
                    name=fname, css=css(f["css"], fwhere), attr=f.get("attr", "text"),
                    regex=regex(f["regex"], fwhere) if f.get("regex") else None, type=ftype,
                    map={str(k): v for k, v in f["map"].items()} if f.get("map") else None,
                    many=bool(f.get("many", False)), default=f.get("default"), required=bool(f.get("required", False))))
            key = str(item.get("key") or "")
            if key not in {f.name for f in fields}:
                problems.append(f"{where}.key: {key!r} must name one of the fields")
            if not item.get("url"):
                problems.append(f"{where}.url: a URL regex is required")
            items.append(ItemRule(str(item.get("name") or f"item{i}"), regex(item.get("url", "(?!)"), where), key, fields))

        change = d.get("change_detection") or {}
        hash_ignore = [css(s, "change_detection.ignore") for s in change.get("ignore") or []]
        link_selector = css(d.get("link_selector", "a[href]"), "link_selector")
        if problems:
            raise SpecError(problems)
        return cls(
            name=name, seeds=seeds, allowed_domains=domains,
            user_agent=str(d.get("user_agent") or "TrawlBot/1.0"),
            rate=None if rate is None else float(rate), burst=float(pol.get("burst", 1)),
            per_host=int(pol.get("per_host_concurrency", 1)), concurrency=int(crawl.get("concurrency", 4)),
            max_pages=int(crawl.get("max_pages", 5000)), max_depth=int(crawl.get("max_depth", 20)),
            timeout=float(crawl.get("timeout", 15)), max_attempts=int(crawl.get("max_attempts", 4)),
            backoff_base=float(crawl.get("backoff_base", 0.5)), backoff_cap=float(crawl.get("backoff_cap", 30)),
            link_selector=link_selector, follow_allow=follow_allow, follow_deny=follow_deny,
            priorities=priorities, items=items, hash_ignore=hash_ignore)

    def with_seeds(self, seeds: list[str]) -> Spec:
        """Same rules, different start URLs (e.g. the local fixture site instead of the real one)."""
        normalized = [s for s in (urls.normalize(x) for x in seeds) if s]
        if not normalized:
            raise SpecError(["seeds: at least one http(s) URL is required"])
        domains = frozenset(urls.urlsplit(s).hostname or "" for s in normalized)
        clone = Spec(**{**self.__dict__})
        clone.seeds, clone.allowed_domains = normalized, domains
        return clone

    # --- decisions -------------------------------------------------------------------------------------------
    def should_follow(self, url: str) -> bool:
        if not urls.in_domains(url, self.allowed_domains) or urls.looks_like_asset(url):
            return False
        path = urls.path_and_query(url)
        if any(p.search(path) for p in self.follow_deny):
            return False
        return not self.follow_allow or any(p.search(path) for p in self.follow_allow)

    def priority_for(self, url: str) -> int:
        path = urls.path_and_query(url)
        for pattern, prio in self.priorities:
            if pattern.search(path):
                return prio
        return 0

    def item_rule_for(self, url: str) -> ItemRule | None:
        path = urls.path_and_query(url)
        return next((r for r in self.items if r.url.search(path)), None)

    # --- page analysis ---------------------------------------------------------------------------------------
    def links(self, root: Node, page_url: str) -> list[str]:
        base = page_url
        base_tag = next((n for n in root.iter() if n.tag == "base" and n.get("href")), None)
        if base_tag is not None:
            base = urls.normalize(base_tag.get("href") or "", page_url) or page_url
        out: list[str] = []
        seen: set[str] = set()
        for node in select(root, self.link_selector):
            if "nofollow" in (node.get("rel") or "").lower().split():
                continue
            url = urls.normalize(node.get("href") or "", base)
            if url and url not in seen:
                seen.add(url)
                out.append(url)
        return out

    def fingerprint(self, root: Node) -> str:
        return fingerprint(root, self.hash_ignore)


def fingerprint(root: Node, ignore: list[str] | None = None) -> str:
    """SHA-256 of the page's meaningful content: tags, link/src/class attributes and text, minus ignored subtrees.

    Volatile parts (a "rendered at" footer, a CSRF token, a rotating ad) go in ``change_detection.ignore`` so they
    do not make every page look changed on every run.
    """
    skip = {id(n) for sel in ignore or [] for n in select(root, sel)}
    h = hashlib.sha256()
    stack: list[Node | str] = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            text = " ".join(node.split())
            if text:
                h.update(text.encode() + b"\x00")
            continue
        if id(node) in skip or node.tag in SKIP_TEXT:
            continue
        h.update(node.tag.encode() + b"\x01")
        for attr in ("href", "src", "class"):
            if attr in node.attrs:
                h.update(f"{attr}={node.attrs[attr]}".encode() + b"\x02")
        stack.extend(reversed(node.children))
    return h.hexdigest()
