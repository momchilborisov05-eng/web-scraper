"""robots.txt parsing and matching, written from the RFC (RFC 9309, Robots Exclusion Protocol).

Not ``urllib.robotparser``: the standard library parser uses first-match instead of longest-match, has no ``*`` or
``$`` wildcards, and does not merge groups that name the same user agent. This module implements:

- groups of one or more ``user-agent`` lines followed by rules; a ``user-agent`` line after a rule starts a new group
- user-agent matching on the product token, case-insensitive; every group that matches is merged; ``*`` is the
  fallback only when no specific group matches
- longest match wins (pattern length in octets); on a tie between allow and disallow, allow wins
- ``*`` matches any sequence of characters, ``$`` anchors the end; percent-escapes are normalized on both sides
- ``/robots.txt`` itself is always allowed; an empty ``disallow:`` allows everything
- status rules: 2xx parse, 4xx "unavailable" means allow everything, 5xx or a network error "unreachable" means
  disallow everything; files are parsed up to 500 KiB
- the non-standard ``crawl-delay`` (per group) and ``sitemap`` (global) lines
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote

from .urls import _normalize_escapes

MAX_BYTES = 500 * 1024


def _encode(pattern: str) -> str:
    # Non-ASCII characters are compared as their UTF-8 percent-encoding (RFC 9309 section 2.2.2).
    return _normalize_escapes(quote(pattern, safe="/?=&*$%:;@,+!~'()[]#-._"))


def wildcard_match(pattern: str, path: str) -> bool:
    """Match a robots.txt path pattern against a path. Prefix match unless the pattern ends with ``$``.

    Two-pointer wildcard matching with single-star backtracking: linear for patterns with at most one ``*`` and
    O(len(pattern) * len(path)) in the worst case, with no regex compilation.
    """
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    else:
        pattern += "*"
    p = s = 0
    star_p = -1
    star_s = 0
    while s < len(path):
        if p < len(pattern) and pattern[p] == "*":
            star_p, star_s = p, s
            p += 1
        elif p < len(pattern) and pattern[p] == path[s]:
            p += 1
            s += 1
        elif star_p != -1:
            p = star_p + 1
            star_s += 1
            s = star_s
        else:
            return False
    while p < len(pattern) and pattern[p] == "*":
        p += 1
    return p == len(pattern)


@dataclass
class Rule:
    allow: bool
    pattern: str

    @property
    def specificity(self) -> int:
        return len(self.pattern.encode())


@dataclass
class Group:
    agents: list[str] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    crawl_delay: float | None = None


@dataclass
class Robots:
    groups: list[Group] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)
    allow_all: bool = False
    disallow_all: bool = False

    # --- construction ----------------------------------------------------------------------------------------
    @classmethod
    def parse(cls, text: str) -> Robots:
        robots = cls()
        current: Group | None = None
        last_was_agent = False
        text = text[:MAX_BYTES].lstrip("﻿")
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower().replace("_", "-")
            if key in ("user-agent", "useragent"):
                if current is None or not last_was_agent:
                    current = Group()
                    robots.groups.append(current)
                current.agents.append(value.lower())
                last_was_agent = True
                continue
            if key == "sitemap":
                if value:
                    robots.sitemaps.append(value)
                continue
            last_was_agent = False
            if current is None:
                continue  # rules before any user-agent line belong to no group
            if key in ("allow", "disallow"):
                if value:  # "Disallow:" with no path allows everything, so it adds no rule
                    current.rules.append(Rule(key == "allow", _encode(value)))
            elif key == "crawl-delay":
                try:
                    delay = float(value)
                except ValueError:
                    continue
                if delay >= 0:
                    current.crawl_delay = delay
        return robots

    @classmethod
    def from_status(cls, status: int, text: str = "") -> Robots:
        """Build the policy for a robots.txt response, applying the RFC 9309 status-code rules."""
        if 200 <= status < 300:
            return cls.parse(text)
        if 400 <= status < 500:
            return cls(allow_all=True)
        return cls(disallow_all=True)  # 5xx, 3xx loops and network errors (status 0): unreachable

    # --- queries ---------------------------------------------------------------------------------------------
    def _groups_for(self, user_agent: str) -> list[Group]:
        token = user_agent.split("/", 1)[0].strip().lower()
        specific = [g for g in self.groups if any(a != "*" and a == token for a in g.agents)]
        if specific:
            return specific
        return [g for g in self.groups if "*" in g.agents]

    def can_fetch(self, user_agent: str, path: str) -> bool:
        """``path`` is the path plus query of the URL, e.g. ``/catalogue/page-2.html?sort=price``."""
        if path.split("?", 1)[0] == "/robots.txt" or self.allow_all:
            return True
        if self.disallow_all:
            return False
        path = _encode(path or "/")
        best: Rule | None = None
        for group in self._groups_for(user_agent):
            for rule in group.rules:
                if not wildcard_match(rule.pattern, path):
                    continue
                if (best is None or rule.specificity > best.specificity
                        or (rule.specificity == best.specificity and rule.allow and not best.allow)):
                    best = rule
        return best is None or best.allow

    def crawl_delay(self, user_agent: str) -> float | None:
        delays = [g.crawl_delay for g in self._groups_for(user_agent) if g.crawl_delay is not None]
        return max(delays) if delays else None
