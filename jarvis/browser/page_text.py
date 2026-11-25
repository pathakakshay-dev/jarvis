"""Visible-page text rules shared with the extension extractor.

This module never talks to a browser. Tests use it to verify exclusion rules.
"""

from __future__ import annotations

from html.parser import HTMLParser

_MAX_SNAPSHOT_CHARS = 12000
_SKIP_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "iframe",
        "object",
        "embed",
        "input",
        "textarea",
        "select",
        "option",
        "button",
        "form",
        "svg",
        "canvas",
        "template",
    }
)
_BLOCK_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "blockquote", "td", "th", "figcaption"})
_VOID_TAGS = frozenset({"input", "embed", "br", "hr", "img", "meta", "link"})


def extract_visible_text(html: str, *, limit: int = _MAX_SNAPSHOT_CHARS) -> str:
    """Extract a limited text snapshot from HTML using the same exclusion rules."""
    if not isinstance(html, str) or not html:
        return ""
    parser = _VisibleTextParser(limit=limit)
    parser.feed(html)
    parser.close()
    return parser.result()


class _VisibleTextParser(HTMLParser):
    def __init__(self, *, limit: int) -> None:
        super().__init__(convert_charrefs=True)
        self._limit = limit
        self._chunks: list[str] = []
        self._size = 0
        self._stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        attrs_map = {key.lower(): (value or "") for key, value in attrs}
        mode = "text"
        if name in _SKIP_TAGS or attrs_map.get("type", "").lower() == "password":
            mode = "skip"
        elif attrs_map.get("hidden") is not None or attrs_map.get("aria-hidden", "").lower() == "true":
            mode = "hidden"
        else:
            style = attrs_map.get("style", "").lower().replace(" ", "")
            if "display:none" in style or "visibility:hidden" in style:
                mode = "hidden"
            elif "contenteditable" in attrs_map and attrs_map.get("contenteditable", "true").lower() != "false":
                mode = "skip"
        self._stack.append(mode)
        if name in _VOID_TAGS:
            self._stack.pop()

    def handle_endtag(self, tag: str) -> None:
        if self._stack:
            self._stack.pop()
        if tag.lower() in _BLOCK_TAGS:
            self._push("\n")

    def handle_data(self, data: str) -> None:
        if any(mode != "text" for mode in self._stack):
            return
        text = " ".join(data.split())
        if not text:
            return
        if self._chunks and not self._chunks[-1].endswith(("\n", " ")):
            self._push(" ")
        self._push(text)

    def _push(self, text: str) -> None:
        if self._size >= self._limit:
            return
        room = self._limit - self._size
        chunk = text[:room]
        self._chunks.append(chunk)
        self._size += len(chunk)

    def result(self) -> str:
        return "".join(self._chunks).strip()
