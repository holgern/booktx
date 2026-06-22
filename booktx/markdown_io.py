"""Markdown extraction and rebuild.

Extraction walks a markdown document with :mod:`markdown_it`, identifies the
translatable prose spans (the ``inline`` tokens inside paragraphs, headings,
list items, blockquotes, and table cells), hides inline code / link URLs / raw
HTML behind ``__TAG_NNN__`` tokens, hides protected names behind
``__NAME_NNN__`` tokens, and returns:

- a **template** — the original markdown with each translatable inline replaced
  by a ``__SPANTX_NNNN__`` placeholder; and
- a list of :class:`~booktx.chunking.ProseSpan` in placeholder order.

Non-translatable blocks (YAML front matter, fenced code, indented code, HTML
blocks) are left untouched in the template.

Rebuild is the inverse: substitute each ``__SPANTX_NNNN__`` with the supplied
translated span text (already name/tag-restored by ``build.py``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from markdown_it import MarkdownIt
from markdown_it.token import Token

from booktx.chunking import ProseSpan
from booktx.models import Placeholder
from booktx.placeholders import protect_names

__all__ = [
    "MarkdownExtraction",
    "FRONT_MATTER_RE",
    "extract_markdown",
    "build_markdown",
    "split_front_matter",
]

#: Translatable container types whose ``inline`` children we extract from.
_TRANSLATABLE_INLINE_PARENTS = {
    "paragraph",
    "heading",
    "list_item",
    "blockquote",  # blockquote wraps a paragraph; inline still flagged here
    "table_cell",
    "td",
    "th",
    "strong",
    "em",
}

#: Block types whose content must never be translated.
_SKIP_BLOCK_TYPES = {"fence", "code_block", "html_block"}

#: Internal span placeholder used inside the markdown template.
_SPANTX_RE = re.compile(r"__SPANTX_(\d{4})__")

#: YAML front matter at the very start of a document.
FRONT_MATTER_RE = re.compile(r"\A---\r?\n(.*?\r?\n)---\r?\n?", re.DOTALL)

# Inline non-translatable patterns, applied to each inline token's raw content.
# Order matters: code first (so URLs inside code are not double-processed), then
# link/image destinations, then raw inline HTML.
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_LINK_DEST_RE = re.compile(r"(?<=\])\(([^)\s]*)(?:\s+\"[^\"]*\")?\)")
_RAW_HTML_INLINE_RE = re.compile(r"<[^>\n]+>")


@dataclass(slots=True)
class MarkdownExtraction:
    """Result of :func:`extract_markdown`."""

    template: str
    spans: list[ProseSpan]
    front_matter: str


def split_front_matter(text: str) -> tuple[str, str]:
    """Split leading YAML front matter from the body.

    Returns ``(front_matter_with_fences, body)``. If there is no front matter,
    ``front_matter`` is empty and ``body`` is the original text.
    """
    m = FRONT_MATTER_RE.match(text)
    if not m:
        return "", text
    return m.group(0), text[m.end() :]


def _hide_inline_nontranslatables(
    raw: str, *, tag_start: int
) -> tuple[str, list[Placeholder], int]:
    """Replace inline code / link URLs / raw HTML with ``__TAG_NNN__`` tokens.

    Returns the rewritten text, the new placeholders, and the next free tag id.
    """
    placeholders: list[Placeholder] = []
    spans_to_hide: list[str] = []

    for m in _INLINE_CODE_RE.finditer(raw):
        spans_to_hide.append(m.group(0))
    for m in _LINK_DEST_RE.finditer(raw):
        # Hide just the URL portion, keep the parentheses.
        spans_to_hide.append(m.group(1))
    for m in _RAW_HTML_INLINE_RE.finditer(raw):
        spans_to_hide.append(m.group(0))

    # Deduplicate while keeping longest-first ordering for safe replacement.
    seen: set[str] = set()
    unique: list[str] = []
    for s in sorted(set(spans_to_hide), key=len, reverse=True):
        if s and s not in seen:
            seen.add(s)
            unique.append(s)

    out = raw
    idx = tag_start
    for original in unique:
        if original not in out:
            continue
        token = f"__TAG_{idx:03d}__"
        idx += 1
        out = out.replace(original, token)
        placeholders.append(Placeholder(token=token, original=original, kind="tag"))

    return out, placeholders, idx


def _collect_inline_tokens(tokens: list[Token]) -> list[Token]:
    """Return translatable ``inline`` tokens in document order.

    An inline token is translatable when its nearest enclosing block is one of
    :data:`_TRANSLATABLE_INLINE_PARENTS` and it is not inside a skipped block.
    """
    result: list[Token] = []
    # Stack of open block types so we know the parent of each inline.
    open_blocks: list[str] = []

    for tok in tokens:
        ttype = tok.type
        if ttype in _SKIP_BLOCK_TYPES:
            # Self-contained skipped block; following tokens are outside it.
            continue
        if ttype.endswith("_open"):
            open_blocks.append(ttype[: -len("_open")])
            continue
        if ttype.endswith("_close"):
            if open_blocks:
                open_blocks.pop()
            continue
        if ttype == "inline":
            parent = open_blocks[-1] if open_blocks else ""
            # table cells use td/th open in the table plugin; also accept
            # 'paragraph' inside blockquotes/list items.
            if parent in _TRANSLATABLE_INLINE_PARENTS and tok.content.strip():
                result.append(tok)
    return result


def extract_markdown(
    text: str, *, protected_terms: list[str] | None = None
) -> MarkdownExtraction:
    """Extract translatable spans from ``text``.

    Returns the rebuild template and the ordered prose spans.
    """
    protected_terms = protected_terms or []
    front_matter, body = split_front_matter(text)

    md = MarkdownIt("commonmark", {"html": True}).enable("table")
    tokens = md.parse(body)
    inline_tokens = _collect_inline_tokens(tokens)

    # Phase 1: hide inline non-translatables per-span, building tagged raw text.
    tag_index = 1
    tagged_texts: list[str] = []
    tag_placeholders_per_span: list[list[Placeholder]] = []
    for tok in inline_tokens:
        raw = tok.content
        tagged, phs, tag_index = _hide_inline_nontranslatables(raw, tag_start=tag_index)
        tagged_texts.append(tagged)
        tag_placeholders_per_span.append(phs)

    # Phase 2: assign globally-consistent NAME tokens across the whole document.
    joined_for_names = "\n".join(tagged_texts)
    name_map_result = protect_names(joined_for_names, protected_terms)
    name_token_for: dict[str, str] = {
        ph.original: ph.token for ph in name_map_result.placeholders
    }

    spans: list[ProseSpan] = []
    for tagged, tag_phs in zip(tagged_texts, tag_placeholders_per_span, strict=True):
        span_text = tagged
        name_phs: list[Placeholder] = []
        # Apply the global name map longest-first.
        for term in sorted(name_token_for, key=len, reverse=True):
            if term in span_text:
                span_text = span_text.replace(term, name_token_for[term])
                name_phs.append(
                    Placeholder(token=name_token_for[term], original=term, kind="name")
                )
        # Preserve the order a translator would see: tags + names by token id.
        placeholders = sorted(tag_phs + name_phs, key=lambda p: p.token)
        used_terms = [p.original for p in name_phs]
        spans.append(
            ProseSpan(
                text=span_text,
                placeholders=placeholders,
                protected_terms=used_terms,
            )
        )

    # Phase 3: build the template by replacing each inline's raw content with a
    # unique __SPANTX_NNNN__ placeholder, scanning the body sequentially so
    # repeated content is disambiguated by position.
    template_body = body
    rebuilt_parts: list[str] = []
    pos = 0
    for idx, tok in enumerate(inline_tokens, start=1):
        raw = tok.content
        found = template_body.find(raw, pos)
        if found < 0:
            # Could happen if content isn't a literal substring (rare). Fall
            # back to leaving the span unreplaced; rebuild will keep it.
            continue
        rebuilt_parts.append(template_body[pos:found])
        rebuilt_parts.append(f"__SPANTX_{idx:04d}__")
        pos = found + len(raw)
    rebuilt_parts.append(template_body[pos:])
    template_body = "".join(rebuilt_parts)

    template = (front_matter + template_body) if front_matter else template_body
    return MarkdownExtraction(template=template, spans=spans, front_matter=front_matter)


def build_markdown(template: str, span_replacements: list[str]) -> str:
    """Rebuild markdown by substituting each ``__SPANTX_NNNN__`` in order.

    ``span_replacements[i]`` replaces the ``(i+1)``-th span placeholder. Extra
    replacements are ignored; missing replacements leave the token in place.
    """
    out = template
    for i, replacement in enumerate(span_replacements, start=1):
        token = f"__SPANTX_{i:04d}__"
        out = out.replace(token, replacement, 1)
    return out


def span_token_ids(template: str) -> list[str]:
    """Return the ``__SPANTX_NNNN__`` tokens found in ``template``, in order."""
    return [m.group(0) for m in _SPANTX_RE.finditer(template)]
