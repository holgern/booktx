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

    structured = extract_epub_structure(
        path,
        include_raw_documents=True,
        include_offsets=True,
        include_inline_runs=True,
        include_segments=False,
        policy=ExtractionPolicy(
            normalize_whitespace=False,
            remove_duplicate_titles=False,
            include_nav_documents=False,
            strict_offsets=False,
        ),
    )
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
    """Rebuild an EPUB via text2epub using one replacement per extracted span."""
    from text2epub import (  # type: ignore[import-not-found]
        Replacement,
        ReplacementPlan,
        rebuild_epub,
    )

    if len(span_replacements) != len(extraction.span_refs):
        raise ValueError(
            "EPUB rebuild replacements do not match the extracted span count."
        )

    assert_source_sha(source_path, extraction.source_sha256)
    replacements = [
        Replacement(
            block_id=span_ref.block_id,
            text=target_text,
            expected_source=span_ref.source_text,
            allow_inline_xhtml=False,
        )
        for span_ref, target_text in zip(
            extraction.span_refs, span_replacements, strict=True
        )
    ]
    rebuild_epub(
        ReplacementPlan(
            source_epub=source_path,
            extraction_manifest=extraction.text2epub_manifest,
            replacements=replacements,
        ),
        output_path,
    )
    return str(output_path)
