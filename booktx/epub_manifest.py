"""Helpers for the migrated EPUB extraction manifest."""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from booktx.chunking import ProseSpan
from booktx.html_io import _translatable_blocks, parse_xhtml
from booktx.models import EpubNavigationRef, EpubSpanRef, EpubTemplateData, Manifest
from booktx.placeholders import protect_names

EPUB_TEMPLATE_PIPELINE = "epub2text+text2epub"
EPUB2TEXT_SCHEMA = "epub2text.structured.v1"
LEGACY_EPUB_MANIFEST_MESSAGE = (
    "This project uses the legacy EPUB extraction format. "
    "Re-run `booktx extract` after upgrading."
)

__all__ = [
    "EPUB2TEXT_SCHEMA",
    "EPUB_TEMPLATE_PIPELINE",
    "LEGACY_EPUB_MANIFEST_MESSAGE",
    "assert_source_sha",
    "build_raw_block_index",
    "load_epub_template_from_manifest",
    "sha256_path",
    "structured_to_navigation_refs",
    "structured_to_span_refs",
    "structured_to_text2epub_manifest",
]


@dataclass(slots=True)
class _RawBlock:
    href: str
    raw_sha256: str
    tag_name: str
    text: str
    outer_start: int
    outer_end: int
    inner_start: int
    inner_end: int
    source_fragment: str


_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([.,;:!?])")


def sha256_path(path: Path | str) -> str:
    """Return the SHA256 hex digest of ``path``."""
    return sha256(Path(path).read_bytes()).hexdigest()


def assert_source_sha(path: Path | str, expected_sha: str) -> None:
    """Raise if ``path`` does not match ``expected_sha``."""
    actual_sha = sha256_path(path)
    if expected_sha and actual_sha != expected_sha:
        raise ValueError(
            f"Source EPUB SHA256 mismatch: expected {expected_sha}, got {actual_sha}."
        )


def build_raw_block_index(structured) -> dict[str, _RawBlock]:
    """Map epub2text block ids to raw archive offsets and fragments."""
    document_blocks: dict[str, list] = {}
    for block in structured.blocks:
        document_blocks.setdefault(block.document_id, []).append(block)

    raw_index: dict[str, _RawBlock] = {}
    with zipfile.ZipFile(structured.source_path) as archive:
        archive_names = archive.namelist()
        for document in structured.documents:
            href = _resolve_archive_href(document.href, archive_names)
            raw_bytes = archive.read(href)
            raw_text = raw_bytes.decode("utf-8")
            soup = parse_xhtml(raw_text)
            candidate_blocks = _translatable_blocks(soup)

            raw_candidates: list[_RawBlock] = []
            cursor = 0
            for candidate in candidate_blocks:
                serialized = str(candidate)
                start = raw_text.find(serialized, cursor)
                if start < 0:
                    continue
                end = start + len(serialized)
                open_end = serialized.find(">") + 1
                close_start = serialized.rfind(f"</{candidate.name}>")
                inner_start = start + open_end
                inner_end = start + close_start
                raw_candidates.append(
                    _RawBlock(
                        href=href,
                        raw_sha256=sha256(raw_bytes).hexdigest(),
                        tag_name=str(candidate.name),
                        text=_normalize_block_text(
                            candidate.get_text(" ", strip=True)
                        ),
                        outer_start=start,
                        outer_end=end,
                        inner_start=inner_start,
                        inner_end=inner_end,
                        source_fragment=raw_text[inner_start:inner_end],
                    )
                )
                cursor = end

            structured_blocks = document_blocks.get(document.document_id, [])
            raw_cursor = 0
            for block in structured_blocks:
                normalized = _normalize_block_text(block.text)
                matched = None
                for idx in range(raw_cursor, len(raw_candidates)):
                    candidate = raw_candidates[idx]
                    if (
                        candidate.tag_name == block.tag_name
                        and candidate.text == normalized
                    ):
                        matched = candidate
                        raw_cursor = idx + 1
                        break
                if matched is None:
                    raise ValueError(
                        f"Could not map EPUB block {block.id} back to raw source XHTML."
                    )
                raw_index[block.id] = matched
    return raw_index


def structured_to_text2epub_manifest(
    structured, *, raw_block_index: dict[str, _RawBlock] | None = None
) -> dict[str, object]:
    """Convert an epub2text structured extraction to a text2epub manifest."""
    raw_block_index = raw_block_index or build_raw_block_index(structured)

    blocks_by_document_id: dict[str, list[dict[str, object]]] = {
        document.document_id: [] for document in structured.documents
    }
    document_hrefs: dict[str, str] = {}
    document_raw_sha256: dict[str, str] = {}

    for block in structured.blocks:
        raw_block = raw_block_index[block.id]
        document_hrefs[block.document_id] = raw_block.href
        document_raw_sha256[block.document_id] = raw_block.raw_sha256
        blocks_by_document_id.setdefault(block.document_id, []).append(
            {
                "block_id": block.id,
                "text": block.text,
                "source_start": raw_block.outer_start,
                "source_end": raw_block.outer_end,
                "body_source_start": raw_block.inner_start,
                "body_source_end": raw_block.inner_end,
                "source_fragment": raw_block.source_fragment,
                "replacement_mode": "whole_block_body",
            }
        )

    entries: list[dict[str, object]] = []
    for document in structured.documents:
        entries.append(
            {
                "href": document_hrefs.get(document.document_id, document.href),
                "media_type": document.media_type,
                "spine_index": document.spine_index,
                "raw_sha256": document_raw_sha256.get(
                    document.document_id, document.raw_bytes_sha256
                ),
                "blocks": blocks_by_document_id.get(document.document_id, []),
            }
        )

    return {
        "schema_version": 1,
        "source_sha256": structured.source_sha256,
        "entries": entries,
    }


def structured_to_span_refs(
    structured,
    *,
    protected_terms: list[str],
    raw_block_index: dict[str, _RawBlock] | None = None,
) -> tuple[list[ProseSpan], list[EpubSpanRef]]:
    """Build booktx spans plus ordered span refs from epub2text blocks."""
    raw_block_index = raw_block_index or build_raw_block_index(structured)
    spans: list[ProseSpan] = []
    span_refs: list[EpubSpanRef] = []

    for block in structured.blocks:
        if not block.text or not block.text.strip():
            continue

        protected = protect_names(block.text, protected_terms)
        names = [placeholder.original for placeholder in protected.placeholders]
        span_index = len(span_refs)
        raw_block = raw_block_index[block.id]

        spans.append(
            ProseSpan(
                text=protected.text,
                placeholders=protected.placeholders,
                protected_terms=names,
            )
        )
        span_refs.append(
            EpubSpanRef(
                span_index=span_index,
                block_id=block.id,
                document_href=block.document_href,
                spine_index=block.spine_index,
                tag_name=block.tag_name,
                source_text=block.text,
                source_text_sha256=sha256(block.text.encode("utf-8")).hexdigest(),
                source_char_start=raw_block.inner_start,
                source_char_end=raw_block.inner_end,
                placeholders=protected.placeholders,
                protected_terms=names,
            )
        )

    return spans, span_refs


def structured_to_navigation_refs(structured) -> list[EpubNavigationRef]:
    """Convert epub2text navigation entries into stored manifest refs."""
    refs: list[EpubNavigationRef] = []
    for entry in structured.navigation:
        refs.append(
            EpubNavigationRef(
                id=entry.id,
                title=entry.title,
                href=entry.href,
                document_href=entry.document_href,
                fragment=entry.fragment,
                spine_index=entry.spine_index,
                source_char_start=entry.source_char_start,
                source_byte_start=entry.source_byte_start,
                level=entry.level,
                parent_id=entry.parent_id,
                order=entry.order,
                children=list(entry.children),
                source=entry.source,
            )
        )
    return refs


def load_epub_template_from_manifest(manifest: Manifest) -> EpubTemplateData:
    """Validate and return the EPUB v2 template stored in ``manifest``."""
    if manifest.source.format != "epub":
        raise ValueError("Manifest source format is not EPUB.")
    if manifest.version < 2:
        raise ValueError(LEGACY_EPUB_MANIFEST_MESSAGE)

    try:
        template = EpubTemplateData.model_validate(manifest.template)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"EPUB manifest template is invalid: {exc}") from exc

    if template.pipeline != EPUB_TEMPLATE_PIPELINE:
        raise ValueError(LEGACY_EPUB_MANIFEST_MESSAGE)
    return template


def _resolve_archive_href(href: str, archive_names: list[str]) -> str:
    if href in archive_names:
        return href
    suffix_matches = [name for name in archive_names if name.endswith(f"/{href}")]
    if len(suffix_matches) == 1:
        return suffix_matches[0]
    return href


def _normalize_block_text(text: str) -> str:
    collapsed = " ".join(text.split())
    return _SPACE_BEFORE_PUNCT_RE.sub(r"\1", collapsed)
