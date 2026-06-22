"""EPUB extraction and rebuild via :mod:`ebooklib`.

Extraction reads an EPUB, walks the spine (XHTML reading-order documents),
and runs :mod:`booktx.html_io` on each document. The per-document templates
and a flat ordered list of prose spans are returned; span order follows spine
order so chunk ids are deterministic.

Rebuild is the inverse: it loads the same EPUB, applies per-span translated
text (already name/tag-restored by ``build.py``) into the cached templates,
and writes a **new** EPUB. Images, CSS, metadata, and spine order are preserved
because EbookLib round-trips them.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field

import ebooklib
from ebooklib import epub

from booktx.chunking import ProseSpan
from booktx.html_io import build_xhtml, extract_xhtml

__all__ = [
    "EpubExtraction",
    "EpubTemplate",
    "extract_epub",
    "build_epub",
    "read_epub",
]

# ebooklib emits a DeprecationWarning about ITEM_DOCUMENT namespace on read.
warnings.filterwarnings(
    "ignore",
    message=".*ITEM_DOCUMENT.*",
    category=DeprecationWarning,
    module="ebooklib.*",
)
warnings.filterwarnings(
    "ignore",
    message=".*In the future.*",
    category=DeprecationWarning,
    module="ebooklib.*",
)


@dataclass(slots=True)
class EpubTemplate:
    """The template + span indices for one spine document."""

    item_id: str
    file_name: str
    template: str
    #: Number of spans that came from this document (used to slice the global
    #: span list during rebuild).
    span_count: int


@dataclass(slots=True)
class EpubExtraction:
    """Result of :func:`extract_epub`."""

    templates: list[EpubTemplate] = field(default_factory=list)
    spans: list[ProseSpan] = field(default_factory=list)


def read_epub(path: str) -> epub.EpubBook:
    """Read an EPUB file into an :class:`epub.EpubBook`."""
    return epub.read_epub(str(path), options={})


def _spine_documents(book: epub.EpubBook) -> list[epub.EpubHtml]:
    """Return the XHTML spine items in reading order, deduplicated."""
    seen: set[str] = set()
    docs: list[epub.EpubHtml] = []
    for entry in book.spine:
        # Spine entries are ``(item_id, linear)`` tuples or bare ids.
        if isinstance(entry, tuple):
            item_id = entry[0]
        else:
            item_id = getattr(entry, "id", entry)
        if not isinstance(item_id, str):
            continue
        ei = book.get_item_with_id(item_id)
        if ei is None or ei.id in seen:
            continue
        seen.add(ei.id)
        # Only XHTML spine documents are translated.
        if ei.get_type() == ebooklib.ITEM_DOCUMENT and ei.file_name.endswith(
            (".xhtml", ".html", ".htm")
        ):
            docs.append(ei)
    return docs


def _item_content_str(item: epub.EpubHtml) -> str:
    """Return the XHTML content of an item as a decoded string."""
    raw = item.get_content()
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


_XML_PROLOG_RE = re.compile(r"^<\?xml[^>]*\?>\s*", re.IGNORECASE)
_DOCTYPE_RE = re.compile(r"^<!DOCTYPE[^>]*>\s*", re.IGNORECASE)


def _strip_xml_preamble(content: str) -> str:
    """Remove a leading XML prolog / DOCTYPE that ebooklib will re-add."""
    content = _XML_PROLOG_RE.sub("", content, count=1)
    content = _DOCTYPE_RE.sub("", content, count=1)
    return content


def extract_epub(
    path: str, *, protected_terms: list[str] | None = None
) -> EpubExtraction:
    """Extract translatable spans from every spine document of an EPUB."""
    protected_terms = protected_terms or []
    book = read_epub(path)
    result = EpubExtraction()
    for doc in _spine_documents(book):
        content = _item_content_str(doc)
        html = extract_xhtml(content, protected_terms=protected_terms)
        result.templates.append(
            EpubTemplate(
                item_id=doc.id,
                file_name=doc.file_name,
                template=html.template,
                span_count=len(html.spans),
            )
        )
        result.spans.extend(html.spans)
    return result


def build_epub(
    source_path: str,
    output_path: str,
    extraction: EpubExtraction,
    span_replacements: list[str],
) -> str:
    """Write a rebuilt EPUB at ``output_path`` using translated spans.

    ``span_replacements`` is the global ordered list (one entry per span in
    ``extraction.spans``); each entry must already be name/tag-restored.
    Returns the output path.
    """
    book = read_epub(source_path)
    docs = _spine_documents(book)
    docs_by_id = {d.id: d for d in docs}

    offset = 0
    for tmpl in extraction.templates:
        doc = docs_by_id.get(tmpl.item_id)
        if doc is None:
            # Spine changed between extract and build; skip safely.
            offset += tmpl.span_count
            continue
        chunk = span_replacements[offset : offset + tmpl.span_count]
        offset += tmpl.span_count
        new_content = build_xhtml(tmpl.template, chunk)
        # ebooklib adds the XML prolog and DOCTYPE on write; its HTML-based
        # body extractor chokes if they are already present, so strip them.
        doc.set_content(_strip_xml_preamble(new_content))

    # Rebuild the TOC from spine documents so round-tripped Link objects
    # (which can carry uid=None) do not break NCX/NAV generation.
    book.toc = [d for d in docs if d.file_name != "nav.xhtml"]

    epub.write_epub(str(output_path), book, {})
    return str(output_path)
    return str(output_path)
