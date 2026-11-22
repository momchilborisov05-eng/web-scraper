"""A forgiving HTML tree builder on top of the standard library tokenizer (``html.parser``).

``html.parser`` only emits start/end/data events; this module turns them into a tree that the CSS selector engine can
walk, and repairs the common kinds of broken markup found in the wild:

- void elements (``<br>``, ``<img>``...) never get children, with or without ``/>``
- implied end tags: a new ``<li>`` closes the open ``<li>``, ``<td>`` closes ``<td>``/``<th>``, ``<tr>`` closes
  the open row, a block element closes an open ``<p>``, and so on
- a stray end tag that matches nothing open is ignored; an end tag closes every element opened after its match
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from html.parser import HTMLParser

VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
                  "track", "wbr"})
# new tag -> (open tags it closes, tags that stop the search)
IMPLIED_END = {
    "li": ({"li"}, {"ul", "ol", "menu"}),
    "dt": ({"dt", "dd"}, {"dl"}),
    "dd": ({"dt", "dd"}, {"dl"}),
    "tr": ({"tr", "td", "th"}, {"table", "tbody", "thead", "tfoot"}),
    "td": ({"td", "th"}, {"tr", "table"}),
    "th": ({"td", "th"}, {"tr", "table"}),
    "tbody": ({"thead", "tbody", "tr", "td", "th"}, {"table"}),
    "option": ({"option"}, {"select", "datalist"}),
}
CLOSES_P = frozenset({"address", "article", "aside", "blockquote", "div", "dl", "fieldset", "footer", "form", "h1",
                      "h2", "h3", "h4", "h5", "h6", "header", "hr", "main", "nav", "ol", "p", "pre", "section",
                      "table", "ul"})
P_SCOPE = frozenset({"div", "td", "th", "li", "body", "html", "article", "section", "button", "table"})
SKIP_TEXT = frozenset({"script", "style", "template", "noscript"})
_WS = re.compile(r"\s+")


class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "elements", "pos")

    def __init__(self, tag: str, attrs: dict[str, str] | None = None, parent: Node | None = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[Node | str] = []
        self.parent = parent
        self.elements: list[Node] = []   # element children, filled by finalize()
        self.pos = 0                     # index among the parent's element children

    def __repr__(self) -> str:
        return f"<{self.tag} {self.attrs}>"

    def get(self, name: str, default: str | None = None) -> str | None:
        return self.attrs.get(name, default)

    @property
    def classes(self) -> list[str]:
        return self.attrs.get("class", "").split()

    def iter(self) -> Iterator[Node]:
        """All descendant elements in document order (not including self)."""
        stack = list(reversed(self.elements))
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.elements))

    def text(self) -> str:
        """Visible text of the subtree, whitespace collapsed."""
        parts: list[str] = []
        self._collect(parts)
        return _WS.sub(" ", "".join(parts)).strip()

    def _collect(self, parts: list[str]) -> None:
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            elif child.tag not in SKIP_TEXT:
                if child.tag in ("br", "p", "div", "li", "tr", "td", "th", "h1", "h2", "h3"):
                    parts.append(" ")
                child._collect(parts)

    def finalize(self) -> None:
        stack = [self]
        while stack:
            node = stack.pop()
            node.elements = [c for c in node.children if isinstance(c, Node)]
            for i, el in enumerate(node.elements):
                el.pos = i
            stack.extend(node.elements)


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack: list[Node] = [self.root]

    def _close_to(self, index: int) -> None:
        del self.stack[index:]

    def _imply_end(self, tag: str) -> None:
        if tag in IMPLIED_END:
            closes, boundary = IMPLIED_END[tag]
            for i in range(len(self.stack) - 1, 0, -1):
                t = self.stack[i].tag
                if t in boundary:
                    break
                if t in closes:
                    self._close_to(i)
                    break
        if tag in CLOSES_P:
            for i in range(len(self.stack) - 1, 0, -1):
                t = self.stack[i].tag
                if t == "p":
                    self._close_to(i)
                    break
                if t in P_SCOPE:
                    break

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._imply_end(tag)
        parent = self.stack[-1]
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, parent)
        parent.children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._imply_end(tag)
        parent = self.stack[-1]
        parent.children.append(Node(tag, {k: (v if v is not None else "") for k, v in attrs}, parent))

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                self._close_to(i)
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def parse_html(html: str) -> Node:
    builder = _Builder()
    builder.feed(html)
    builder.close()
    builder.root.finalize()
    return builder.root
