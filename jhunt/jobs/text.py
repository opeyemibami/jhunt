"""Plain text from a job page's HTML, with the stdlib parser (ADR 0007).

Good enough to read and search a snapshot; the HTML record keeps everything.
A better extractor can come later as its own dependency decision.
"""

import re
from html.parser import HTMLParser

# Content inside these is never text a reader sees.
_SKIP = {"script", "style", "noscript", "template", "svg"}
# These start a new line.
_BLOCKS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "td", "th", "title", "tr", "ul",
}  # fmt: skip


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)  # &amp; becomes &, and so on
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in _SKIP:
            self._skipping += 1
        if tag in _BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP and self._skipping:
            self._skipping -= 1
        if tag in _BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            # Any run of whitespace in the source, newlines included, is one
            # space in HTML; only block tags start a new line. Keep it at the
            # edges too: "We <em>operate</em>" must not become "Weoperate".
            # Where the source has no whitespace (We<span>operate</span>), none
            # is added, as a browser wouldn't either.
            self.parts.append(re.sub(r"\s+", " ", data))


def html_to_text(html: bytes) -> str:
    """One line per block, whitespace collapsed, blank lines dropped.

    Bytes are read as UTF-8, with anything else replaced, so the same bytes
    always give the same text (the stored record has no Content-Type charset).
    """
    collector = _Collector()
    collector.feed(html.decode("utf-8", errors="replace"))
    collector.close()
    lines = (" ".join(line.split()) for line in "".join(collector.parts).split("\n"))
    return "\n".join(line for line in lines if line)
