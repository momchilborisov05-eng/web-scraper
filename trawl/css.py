"""A CSS selector engine: a hand-written parser plus right-to-left matching over ``dom.Node`` trees.

Supported (enough for real scraping rules, see ``specs/books.yaml``):

- type ``div``, universal ``*``, ``#id``, ``.class`` (any number, combined)
- attributes ``[a]`` ``[a=v]`` ``[a~=v]`` ``[a^=v]`` ``[a$=v]`` ``[a*=v]`` ``[a|=v]``, quoted or bare values, ``i`` flag
- combinators: descendant (space), child ``>``, adjacent sibling ``+``, general sibling ``~``
- pseudo-classes: ``:first-child`` ``:last-child`` ``:only-child`` ``:nth-child(an+b|odd|even)``
  ``:nth-last-child()`` ``:first-of-type`` ``:last-of-type`` ``:nth-of-type()`` ``:not(compound)`` ``:empty``
  and the scraping extension ``:contains("text")`` (as in jQuery / Scrapy's cssselect)
- selector lists ``a, b``; results are unique and in document order

Matching is right to left, the way browsers do it: the rightmost compound filters candidates cheaply and only
those are checked against their ancestors or siblings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from .dom import Node


class SelectorError(ValueError):
    pass


_IDENT = re.compile(r"-?[_a-zA-Z -￿][_a-zA-Z0-9\- -￿]*")
_BARE_VALUE = re.compile(r"""[^\s\])"'=]+""")
_NTH = re.compile(r"^([+-]?\d*)n\s*(?:([+-])\s*(\d+))?$")


def parse_nth(expr: str) -> tuple[int, int]:
    """``an+b`` -> (a, b). Accepts ``odd``, ``even``, ``3``, ``n``, ``-n+3``, ``2n+1``, ``+5``."""
    s = expr.strip().lower().replace(" ", "")
    if s == "odd":
        return 2, 1
    if s == "even":
        return 2, 0
    if re.fullmatch(r"[+-]?\d+", s):
        return 0, int(s)
    m = _NTH.match(s)
    if not m:
        raise SelectorError(f"bad nth expression: {expr!r}")
    a_text, sign, b_text = m.groups()
    a = -1 if a_text == "-" else 1 if a_text in ("", "+") else int(a_text)
    b = int(b_text) if b_text else 0
    return a, (-b if sign == "-" else b)


def nth_matches(a: int, b: int, k: int) -> bool:
    """Is position ``k`` (1-based) equal to a*n + b for some integer n >= 0?"""
    if a == 0:
        return k == b
    n, rem = divmod(k - b, a)
    return rem == 0 and n >= 0


@dataclass
class Attr:
    name: str
    op: str | None = None
    value: str = ""
    ignore_case: bool = False

    def matches(self, node: Node) -> bool:
        actual = node.attrs.get(self.name)
        if actual is None:
            return False
        if self.op is None:
            return True
        want = self.value
        if self.ignore_case:
            actual, want = actual.lower(), want.lower()
        if self.op == "=":
            return actual == want
        if self.op == "~=":
            return want in actual.split()
        if self.op == "^=":
            return bool(want) and actual.startswith(want)
        if self.op == "$=":
            return bool(want) and actual.endswith(want)
        if self.op == "*=":
            return bool(want) and want in actual
        if self.op == "|=":
            return actual == want or actual.startswith(want + "-")
        raise SelectorError(f"unknown attribute operator {self.op}")


@dataclass
class Compound:
    tag: str | None = None
    ids: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    attrs: list[Attr] = field(default_factory=list)
    pseudos: list[tuple[str, object]] = field(default_factory=list)

    def matches(self, node: Node) -> bool:
        if self.tag is not None and node.tag != self.tag:
            return False
        if self.ids and any(node.attrs.get("id") != i for i in self.ids):
            return False
        if self.classes:
            have = node.classes
            if any(c not in have for c in self.classes):
                return False
        if any(not a.matches(node) for a in self.attrs):
            return False
        return all(_pseudo(node, name, arg) for name, arg in self.pseudos)


def _siblings(node: Node) -> list[Node]:
    return node.parent.elements if node.parent is not None else [node]


def _pseudo(node: Node, name: str, arg: object) -> bool:
    sibs = _siblings(node)
    if name == "first-child":
        return node.pos == 0
    if name == "last-child":
        return node.pos == len(sibs) - 1
    if name == "only-child":
        return len(sibs) == 1
    if name == "empty":
        return not node.elements and not any(isinstance(c, str) and c.strip() for c in node.children)
    if name in ("nth-child", "nth-last-child"):
        a, b = arg  # type: ignore[misc]
        k = node.pos + 1 if name == "nth-child" else len(sibs) - node.pos
        return nth_matches(a, b, k)
    if name in ("nth-of-type", "first-of-type", "last-of-type", "nth-last-of-type"):
        same = [s for s in sibs if s.tag == node.tag]
        idx = next(i for i, s in enumerate(same) if s is node)
        if name == "first-of-type":
            return idx == 0
        if name == "last-of-type":
            return idx == len(same) - 1
        a, b = arg  # type: ignore[misc]
        k = idx + 1 if name == "nth-of-type" else len(same) - idx
        return nth_matches(a, b, k)
    if name == "not":
        return not arg.matches(node)  # type: ignore[union-attr]
    if name == "contains":
        return str(arg) in node.text()
    raise SelectorError(f"unsupported pseudo-class :{name}")


@dataclass
class Selector:
    compounds: list[Compound]
    combinators: list[str]   # combinators[i] joins compounds[i] and compounds[i + 1]

    def matches(self, node: Node) -> bool:
        return self._match(node, len(self.compounds) - 1)

    def _match(self, node: Node, i: int) -> bool:
        if not self.compounds[i].matches(node):
            return False
        if i == 0:
            return True
        comb = self.combinators[i - 1]
        if comb == ">":
            parent = node.parent
            return parent is not None and parent.tag != "#document" and self._match(parent, i - 1)
        if comb == " ":
            anc = node.parent
            while anc is not None and anc.tag != "#document":
                if self._match(anc, i - 1):
                    return True
                anc = anc.parent
            return False
        sibs = _siblings(node)
        if comb == "+":
            return node.pos > 0 and self._match(sibs[node.pos - 1], i - 1)
        if comb == "~":
            return any(self._match(s, i - 1) for s in sibs[:node.pos])
        raise SelectorError(f"unknown combinator {comb!r}")


class _Parser:
    def __init__(self, text: str):
        self.s = text
        self.i = 0

    def error(self, msg: str) -> SelectorError:
        return SelectorError(f"{msg} at position {self.i} in {self.s!r}")

    def peek(self) -> str:
        return self.s[self.i] if self.i < len(self.s) else ""

    def skip_ws(self) -> bool:
        start = self.i
        while self.peek().isspace():
            self.i += 1
        return self.i > start

    def ident(self) -> str:
        m = _IDENT.match(self.s, self.i)
        if not m:
            raise self.error("expected a name")
        self.i = m.end()
        return m.group(0)

    def string_or_ident(self) -> str:
        q = self.peek()
        if q in "\"'":
            end = self.s.find(q, self.i + 1)
            if end == -1:
                raise self.error("unterminated string")
            value = self.s[self.i + 1:end]
            self.i = end + 1
            return value
        m = _BARE_VALUE.match(self.s, self.i)   # unquoted: lenient (allows "a[href$=.pdf]") but no quotes or "="
        if not m:
            raise self.error("expected a value")
        self.i = m.end()
        return m.group(0)

    def group(self) -> list[Selector]:
        selectors = [self.selector()]
        while self.peek() == ",":
            self.i += 1
            selectors.append(self.selector())
        if self.i != len(self.s):
            raise self.error("unexpected character")
        return selectors

    def selector(self) -> Selector:
        self.skip_ws()
        compounds = [self.compound()]
        combinators: list[str] = []
        while True:
            had_ws = self.skip_ws()
            ch = self.peek()
            if ch in (">", "+", "~"):
                self.i += 1
                self.skip_ws()
                combinators.append(ch)
            elif ch in ("", ",", ")"):
                break
            elif had_ws:
                combinators.append(" ")
            else:
                raise self.error("unexpected character")
            compounds.append(self.compound())
        return Selector(compounds, combinators)

    def compound(self) -> Compound:
        c = Compound()
        start = self.i
        if self.peek() == "*":
            self.i += 1
        elif _IDENT.match(self.s, self.i):
            c.tag = self.ident().lower()
        while True:
            ch = self.peek()
            if ch == "#":
                self.i += 1
                c.ids.append(self.ident())
            elif ch == ".":
                self.i += 1
                c.classes.append(self.ident())
            elif ch == "[":
                c.attrs.append(self.attribute())
            elif ch == ":":
                c.pseudos.append(self.pseudo())
            else:
                break
        if self.i == start:
            raise self.error("expected a selector")
        return c

    def attribute(self) -> Attr:
        self.i += 1
        self.skip_ws()
        name = self.ident().lower()
        self.skip_ws()
        attr = Attr(name)
        for op in ("~=", "^=", "$=", "*=", "|=", "="):
            if self.s.startswith(op, self.i):
                self.i += len(op)
                self.skip_ws()
                attr.op = op
                attr.value = self.string_or_ident()
                self.skip_ws()
                if self.peek() in ("i", "I"):
                    attr.ignore_case = True
                    self.i += 1
                    self.skip_ws()
                break
        if self.peek() != "]":
            raise self.error("expected ']'")
        self.i += 1
        return attr

    def pseudo(self) -> tuple[str, object]:
        self.i += 1
        name = self.ident().lower()
        needs_arg = name in ("nth-child", "nth-last-child", "nth-of-type", "nth-last-of-type", "not", "contains")
        if not needs_arg:
            if name not in ("first-child", "last-child", "only-child", "first-of-type", "last-of-type", "empty"):
                raise self.error(f"unsupported pseudo-class :{name}")
            return name, None
        if self.peek() != "(":
            raise self.error(f":{name} needs an argument")
        self.i += 1
        self.skip_ws()
        arg: object
        if name == "not":
            arg = self.compound()
        elif name == "contains":
            arg = self.string_or_ident()
        else:
            end = self.s.find(")", self.i)
            if end == -1:
                raise self.error("expected ')'")
            arg = parse_nth(self.s[self.i:end])
            self.i = end
        self.skip_ws()
        if self.peek() != ")":
            raise self.error("expected ')'")
        self.i += 1
        return name, arg


@lru_cache(maxsize=512)
def compile_selector(text: str) -> tuple[Selector, ...]:
    if not text or not text.strip():
        raise SelectorError("empty selector")
    return tuple(_Parser(text.strip()).group())


def select(root: Node, selector: str) -> list[Node]:
    """All elements under ``root`` that match ``selector``, in document order."""
    group = compile_selector(selector)
    return [node for node in root.iter() if any(sel.matches(node) for sel in group)]


def select_one(root: Node, selector: str) -> Node | None:
    group = compile_selector(selector)
    for node in root.iter():
        if any(sel.matches(node) for sel in group):
            return node
    return None
