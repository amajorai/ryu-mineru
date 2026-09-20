"""Post-processing of the markdown MinerU writes.

MinerU's native output *is* markdown — formulas already in `$…$` / `$$…$$`, tables
already as HTML blocks, reading order already reconstructed — so there is no
element-to-markdown rendering step here the way there is in the `unstructured`
backend. What is left is two small jobs: a markup-free `text` variant for callers
that want one, and a byte-budget clip that is honest about having clipped.
"""

from __future__ import annotations

import re

# Top-level, compiled once: these run over whole documents, sometimes hundreds of
# thousands of characters, and rebuilding them per call is pure waste.
_CODE_FENCE_RE = re.compile(r"^\s*```.*$", re.MULTILINE)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_BLOCKQUOTE_RE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s{0,3}[-*+]\s+", re.MULTILINE)
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_EMPHASIS_RE = re.compile(r"(\*\*|__|\*|_|`)")
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def to_plain_text(markdown: str) -> str:
    """A markup-free rendering of the same content.

    Not a markdown parser — a deliberate, small set of removals. Tables keep their
    cell text (the HTML tags go, the words stay), which is what a plain-text
    consumer wants; anything richer belongs in the `markdown` field, which is the
    primary payload and is never degraded.
    """
    text = _CODE_FENCE_RE.sub("", markdown)
    text = _IMAGE_RE.sub(r"\1", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _HEADING_RE.sub("", text)
    text = _BLOCKQUOTE_RE.sub("", text)
    text = _BULLET_RE.sub("", text)
    text = _EMPHASIS_RE.sub("", text)
    lines = [line.rstrip() for line in text.splitlines()]
    return _BLANK_RUN_RE.sub("\n\n", "\n".join(lines)).strip()


def truncate(text: str, budget: int) -> tuple[str, bool]:
    """Clip to a byte budget on a character boundary.

    Returns `(text, whole)`. A clipped document is useful and is flagged
    `truncated: true`; a dropped one is the silent-failure bug this capability
    exists to kill.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text, True
    clipped = encoded[:budget].decode("utf-8", errors="ignore")
    return clipped, False
