"""XHTML text extraction and rebuild, shared by :mod:`booktx.epub_io`.

Design (mirrors :mod:`booktx.markdown_io` for HTML):

- *Translatable blocks* are leaf prose containers (``p``, ``h1``..``h6``,
  ``li``, ``td``, ``th``, ``caption``, ``dt``, ``dd``, ``figcaption``).
- For each translatable block, its direct children are walked in order:
  - A text node becomes translatable prose.
  - An inline element (``a``, ``strong``, ``em``, ``code``, ``span``, …) is
    serialized to XHTML and hidden behind a ``__TAG_NNN__`` token, so the
    translator never sees the markup and rebuild restores it verbatim. This
    naturally skips inline code from translation.
- Protected names get globally-consistent ``__NAME_NNN__`` tokens.
- The extracted template is the XHTML with each translatable block's children
  replaced by a single ``__SPANTX_NNNN__`` text node; rebuild is the inverse.
- ``<pre>`` blocks, ``<script>``, ``<style>``, and the ``<head>`` are never
  touched. Image ``alt`` text and other attributes are not translated in v1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, NavigableString, Tag  # type: ignore[attr-defined]
from bs4.formatter import XMLFormatter

from booktx.chunking import ProseSpan
from booktx.models import Placeholder
from booktx.placeholders import SPANTX_RE, protect_names

__all__ = [
    "HtmlExtraction",
    "TRANSLATABLE_BLOCK_TAGS",
    "SKIP_BLOCK_TAGS",
    "parse_xhtml",
    "extract_xhtml",
    "build_xhtml",
    "restore_block_content",
]

#: Leaf prose containers that each produce one span.
TRANSLATABLE_BLOCK_TAGS = {
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "td",
    "th",
    "caption",
    "dt",
    "dd",
    "figcaption",
}

#: Tags that are never translated and never traversed for text.
SKIP_BLOCK_TAGS = {"pre", "script", "style", "head", "title", "noscript"}

#: Inline-level tags that become a single ``__TAG_NNN__`` placeholder. Any tag
#: that is not a known block tag and not skipped is treated as inline.
_BLOCK_TAG_UNIVERSE = TRANSLATABLE_BLOCK_TAGS | {
    "div",
    "section",
    "article",
    "main",
    "header",
    "footer",
    "nav",
    "aside",
    "blockquote",
    "ul",
    "ol",
    "table",
    "thead",
    "tbody",
    "tfoot",
    "tr",
    "dl",
    "figure",
    "body",
    "html",
}


def _is_inline(tag: Tag) -> bool:
    return tag.name not in _BLOCK_TAG_UNIVERSE and tag.name not in SKIP_BLOCK_TAGS


_SPANTX_RE = SPANTX_RE  # backward-compatible alias


class _RawTextFormatter(XMLFormatter):
    """Don't HTML-escape placeholder underscores or quotes we just wrote."""

    def __init__(self) -> None:
        super().__init__(entity_substitution=None)


@dataclass(slots=True)
class HtmlExtraction:
    """Result of :func:`extract_xhtml`."""

    template: str
    spans: list[ProseSpan]


def parse_xhtml(xhtml: str | bytes) -> BeautifulSoup:
    """Parse an XHTML document with a forgiving XML parser."""
    if isinstance(xhtml, bytes):
        return BeautifulSoup(xhtml, "lxml-xml")
    return BeautifulSoup(xhtml, "lxml-xml")


_OPEN_TAG_RE = re.compile(r"^<[^>]+>")


def _open_tag_str(tag: Tag) -> str:
    """Return just the opening tag of ``tag`` with attributes.

    e.g. ``<a href=\"u\">``.
    """
    m = _OPEN_TAG_RE.match(str(tag))
    return m.group(0) if m else f"<{tag.name}>"


# Self-closing / void inline elements get a single TAG token (no close pair).
_VOID_INLINE_TAGS = {
    "br",
    "img",
    "wbr",
    "hr",
    "input",
    "area",
    "col",
    "embed",
    "source",
    "track",
}

# Opaque inline elements: their inner text is preserved verbatim (not
# translated). Inline code and friends belong here.
_OPAQUE_INLINE_TAGS = {"code", "kbd", "samp", "var", "tt"}


def _extract_block_span(
    block: Tag, *, tag_start: int
) -> tuple[str, list[Placeholder], int]:
    """Build the protected prose for one block, recursing into inline tags.

    Inline tags become paired ``__TAG_NNN__`` open/close tokens (or a single
    token for void elements), so their inner text stays translatable while
    attributes and nesting survive rebuild verbatim.
    """
    parts: list[str] = []
    placeholders: list[Placeholder] = []
    idx_local = tag_start

    def walk(node: Tag) -> int:
        nonlocal idx_local
        for child in node.children:
            if isinstance(child, NavigableString):
                if getattr(child, "is_comment", False):
                    continue
                parts.append(str(child))
            elif isinstance(child, Tag):
                if child.name in SKIP_BLOCK_TAGS:
                    continue
                if not _is_inline(child):
                    # Block-level child of a leaf block: skip (handled elsewhere).
                    continue
                if child.name in _VOID_INLINE_TAGS or child.name in _OPAQUE_INLINE_TAGS:
                    token = f"__TAG_{idx_local:03d}__"
                    idx_local += 1
                    parts.append(token)
                    placeholders.append(
                        Placeholder(token=token, original=str(child), kind="tag")
                    )
                    continue
                open_token = f"__TAG_{idx_local:03d}__"
                idx_local += 1
                parts.append(open_token)
                placeholders.append(
                    Placeholder(
                        token=open_token, original=_open_tag_str(child), kind="tag"
                    )
                )
                walk(child)
                close_token = f"__TAG_{idx_local:03d}__"
                idx_local += 1
                parts.append(close_token)
                placeholders.append(
                    Placeholder(
                        token=close_token, original=f"</{child.name}>", kind="tag"
                    )
                )
        return idx_local

    walk(block)
    return "".join(parts), placeholders, idx_local


def _translatable_blocks(soup: BeautifulSoup) -> list[Tag]:
    blocks: list[Tag] = []
    for tag in soup.find_all(True):
        if tag.name not in TRANSLATABLE_BLOCK_TAGS:
            continue
        # Skip blocks that live inside a skipped ancestor.
        if any(p.name in SKIP_BLOCK_TAGS for p in tag.parents):
            continue
        # Skip blocks that contain a block-level child (they are not leaf
        # prose containers); their inner blocks are extracted separately.
        has_block_child = any(
            isinstance(c, Tag)
            and c.name in _BLOCK_TAG_UNIVERSE
            and c.name not in TRANSLATABLE_BLOCK_TAGS
            for c in tag.children
        )
        if has_block_child:
            continue
        blocks.append(tag)
    return blocks


def extract_xhtml(
    xhtml: str | bytes, *, protected_terms: list[str] | None = None
) -> HtmlExtraction:
    """Extract translatable spans from an XHTML document."""
    protected_terms = protected_terms or []
    soup = parse_xhtml(xhtml)
    blocks = _translatable_blocks(soup)

    tag_index = 1
    tagged_texts: list[str] = []
    tag_phs_per_block: list[list[Placeholder]] = []
    keep_blocks: list[Tag] = []

    for block in blocks:
        raw, phs, tag_index = _extract_block_span(block, tag_start=tag_index)
        if not raw.strip():
            continue
        tagged_texts.append(raw)
        tag_phs_per_block.append(phs)
        keep_blocks.append(block)

    # Globally-consistent name tokens.
    joined = "\n".join(tagged_texts)
    name_res = protect_names(joined, protected_terms)
    name_token_for = {ph.original: ph.token for ph in name_res.placeholders}

    spans: list[ProseSpan] = []
    for raw, tag_phs in zip(tagged_texts, tag_phs_per_block, strict=True):
        span_text = raw
        name_phs: list[Placeholder] = []
        for term in sorted(name_token_for, key=len, reverse=True):
            if term in span_text:
                span_text = span_text.replace(term, name_token_for[term])
                name_phs.append(
                    Placeholder(token=name_token_for[term], original=term, kind="name")
                )
        placeholders = sorted(tag_phs + name_phs, key=lambda p: p.token)
        spans.append(
            ProseSpan(
                text=span_text,
                placeholders=placeholders,
                protected_terms=[p.original for p in name_phs],
            )
        )

    # Replace each kept block's children with a single SPANTX text node.
    for idx, block in enumerate(keep_blocks, start=1):
        block.clear()
        block.append(NavigableString(f"__SPANTX_{idx:04d}__"))

    template = str(soup)
    return HtmlExtraction(template=template, spans=spans)


def restore_block_content(block: Tag, translated_text: str) -> None:
    """Replace ``block``'s children with translated text + restored inline tags.

    ``translated_text`` must already have NAME/TAG tokens restored (use
    :func:`booktx.placeholders.restore`). Inline-tag fragments are re-parsed
    and re-attached, preserving nested structure and attributes.
    """
    block.clear()
    # Split keeping the TAG tokens. Tokens look like __TAG_NNN__; but after
    # restore() they are already gone. So this function expects the caller to
    # pass text that may still contain literal inline-XHTML fragments only if
    # restore kept them. We re-parse any "<...>" fragments the caller embedded.
    _append_mixed(block, translated_text)


def _append_mixed(parent: Tag, text: str) -> None:
    """Append ``text`` to ``parent``, re-parsing inline XHTML fragments.

    The translator's output is plain text plus inline-XHTML fragments restored
    from TAG placeholders. We split on top-level ``<tag ...>`` openings and
    re-parse each fragment so attributes/nesting survive.
    """
    if not text:
        return
    # Strategy: parse the whole string as XML fragments. To keep text-only
    # leading/trailing text, wrap in a temporary root.
    wrapped = f"<root>{text}</root>"
    try:
        parsed = BeautifulSoup(wrapped, "lxml-xml")
    except Exception:  # noqa: BLE001 - fall back to plain text
        parent.append(NavigableString(text))
        return
    root = parsed.find("root")
    if root is None:
        parent.append(NavigableString(text))
        return
    # Re-parent children into parent, preserving order and node types.
    for child in list(root.contents):
        parent.append(child.extract() if isinstance(child, Tag) else child)


def build_xhtml(template: str, span_replacements: list[str]) -> str:
    """Rebuild XHTML by substituting each ``__SPANTX_NNNN__`` block in order.

    ``span_replacements[i]`` must already have name/tag placeholders restored
    (so it is a mix of text and inline-XHTML fragments). The corresponding
    block element is cleared and refilled via :func:`restore_block_content`.
    """
    soup = parse_xhtml(template)
    # SPANTX markers live as text nodes inside their blocks. Find them in
    # document order.
    marker_nodes = []
    for node in soup.find_all(string=lambda s: bool(s and _SPANTX_RE.search(s))):
        marker_nodes.append(node)

    for i, replacement in enumerate(span_replacements, start=1):
        token = f"__SPANTX_{i:04d}__"
        # Find the marker text node for this index.
        target = None
        for node in marker_nodes:
            if token in str(node):
                target = node
                break
        if target is None:
            continue
        parent = target.parent
        if parent is None:
            continue
        restore_block_content(parent, replacement)
        # Remove the consumed marker from the search list.
        if target in marker_nodes:
            marker_nodes.remove(target)

    return str(soup)


# span_token_ids is imported from booktx.placeholders above
