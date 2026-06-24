"""EPUB extraction and rebuild adapters over epub2text and text2epub."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field

from booktx.chunking import ProseSpan
from booktx.epub_manifest import (
    assert_source_sha,
    build_raw_block_index,
    structured_to_navigation_refs,
    structured_to_span_refs,
    structured_to_text2epub_manifest,
)
from booktx.models import EpubNavigationRef, EpubSpanRef

__all__ = [
    "EpubExtraction",
    "build_epub",
    "extract_epub",
    "read_epub",
]


@dataclass(slots=True)
class EpubExtraction:
    """Structured EPUB extraction data used by booktx."""

    spans: list[ProseSpan] = field(default_factory=list)
    span_refs: list[EpubSpanRef] = field(default_factory=list)
    text2epub_manifest: dict[str, object] = field(default_factory=dict)
    source_sha256: str = ""
    navigation: list[EpubNavigationRef] = field(default_factory=list)


def read_epub(path: str) -> zipfile.ZipFile:
    """Open an EPUB archive for direct ZIP-level inspection."""
    return zipfile.ZipFile(str(path))


def extract_epub(
    path: str, *, protected_terms: list[str] | None = None
) -> EpubExtraction:
    """Extract translatable EPUB spans through epub2text structured blocks."""
    from epub2text import extract_epub_structure  # type: ignore[import-not-found]
    from epub2text.structured import ExtractionPolicy  # type: ignore[import-not-found]

    try:
        structured = extract_epub_structure(
            path,
            include_raw_documents=True,
            include_offsets=True,
            include_inline_runs=True,
            include_segments=True,
            include_xhtml_fragments=True,
            policy=ExtractionPolicy(
                normalize_whitespace=False,
                remove_duplicate_titles=False,
                include_nav_documents=False,
                strict_offsets=False,
            ),
        )
    except TypeError as exc:
        raise RuntimeError(
            "Installed epub2text does not support include_xhtml_fragments; "
            "upgrade epub2text before extracting EPUB sources."
        ) from exc
    raw_block_index = build_raw_block_index(structured)
    spans, span_refs = structured_to_span_refs(
        structured,
        protected_terms=protected_terms or [],
        raw_block_index=raw_block_index,
    )
    return EpubExtraction(
        spans=spans,
        span_refs=span_refs,
        text2epub_manifest=structured_to_text2epub_manifest(
            structured, raw_block_index=raw_block_index
        ),
        source_sha256=structured.source_sha256,
        navigation=structured_to_navigation_refs(structured),
    )


def build_epub(
    source_path: str,
    output_path: str,
    extraction: EpubExtraction,
    span_replacements: list[str],
) -> str:
    """Rebuild an EPUB via text2epub using one replacement per extracted span.

    ``span_replacements`` provides one target string per entry in
    ``extraction.spans`` (sentence-level). Spans are grouped back into one
    :class:`text2epub.Replacement` per block-level ``span_ref``: for each
    ``span_ref`` the replacements whose span index falls in
    ``[span_ref.span_index, next_span_index)`` are joined with a space. When
    every span of a block is unchanged, the original raw block is reused so
    pass-through builds stay byte-identical even when the joined sentence XHTML
    differs from the stored block fragment.
    """
    from text2epub import (  # type: ignore[import-not-found]
        Replacement,
        ReplacementPlan,
        rebuild_epub,
    )

    if len(span_replacements) != len(extraction.spans):
        raise ValueError(
            "EPUB rebuild replacements must provide one entry per extracted span."
        )

    assert_source_sha(source_path, extraction.source_sha256)
    from booktx.epub_inline_xhtml import sanitize_target_fragment
    from booktx.placeholders import restore

    source_fragments = [
        restore(span.text, span.placeholders) for span in extraction.spans
    ]
    replacements = []
    for idx, span_ref in enumerate(extraction.span_refs):
        next_span_index = (
            extraction.span_refs[idx + 1].span_index
            if idx + 1 < len(extraction.span_refs)
            else None
        )
        start = span_ref.span_index
        end = next_span_index if next_span_index is not None else len(extraction.spans)
        targets = span_replacements[start:end]
        sources = source_fragments[start:end]
        joined_target = " ".join(t.strip() for t in targets if t and t.strip())
        joined_source = " ".join(s.strip() for s in sources if s and s.strip())
        source_view = span_ref.source_view_text or span_ref.source_text
        replacement_text = joined_target
        allow_inline_xhtml = False
        records_unchanged = all(
            target == source for target, source in zip(targets, sources, strict=True)
        )
        if records_unchanged:
            replacement_text = span_ref.source_text
        elif joined_target == source_view:
            replacement_text = span_ref.source_text
        elif span_ref.source_markup == "epub-inline-xhtml:v1":
            sanitized = sanitize_target_fragment(joined_target, joined_source)
            errors = [issue for issue in sanitized.issues if issue.severity == "error"]
            if errors:
                raise ValueError(errors[0].message)
            replacement_text = sanitized.xhtml
            allow_inline_xhtml = True
        replacements.append(
            Replacement(
                block_id=span_ref.block_id,
                text=replacement_text,
                expected_source=span_ref.source_text,
                allow_inline_xhtml=allow_inline_xhtml,
            )
        )
    rebuild_epub(
        ReplacementPlan(
            source_epub=source_path,
            extraction_manifest=extraction.text2epub_manifest,
            replacements=replacements,
        ),
        output_path,
    )
    return str(output_path)
