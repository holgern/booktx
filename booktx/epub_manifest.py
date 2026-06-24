"""Helpers for the migrated EPUB extraction manifest."""

from __future__ import annotations

import re
import zipfile
from dataclasses import asdict, dataclass
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from booktx.chunking import ProseSpan
from booktx.models import EpubNavigationRef, EpubSpanRef, EpubTemplateData, Manifest

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
_DEFAULT_SKIP_TAGS = frozenset({"head", "title", "script", "style", "noscript"})
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


@dataclass(slots=True)
class _OpenRawBlock:
    tag_name: str
    path: str
    outer_start: int
    inner_start: int


@dataclass(slots=True)
class _ParsedRawBlock:
    tag_name: str
    path: str
    outer_start: int
    outer_end: int
    inner_start: int
    inner_end: int


class _RawBlockParser(HTMLParser):
    def __init__(self, raw_text: str, *, block_tags: frozenset[str]):
        super().__init__(convert_charrefs=False)
        self.raw_text = raw_text
        self.block_tags = block_tags
        self.line_starts = self._line_starts(raw_text)
        self.stack: list[tuple[str, int]] = []
        self.open_blocks: list[_OpenRawBlock] = []
        self.blocks: list[_ParsedRawBlock] = []

    @staticmethod
    def _line_starts(text: str) -> list[int]:
        starts = [0]
        for match in re.finditer("\n", text):
            starts.append(match.end())
        return starts

    def _offset(self) -> int:
        line, col = self.getpos()
        return self.line_starts[line - 1] + col

    def _skipped(self) -> bool:
        return any(tag in _DEFAULT_SKIP_TAGS for tag, _ in self.stack)

    def _path(self, tag: str) -> str:
        counts: dict[str, int] = {}
        parts = []
        for name, _ in self.stack + [(tag, 0)]:
            counts[name] = counts.get(name, 0) + 1
            parts.append(f"{name}[{counts[name]}]")
        return "/" + "/".join(parts)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        start = self._offset()
        raw = self.get_starttag_text() or self.raw_text[start:start]
        end = start + len(raw)
        if not self._skipped() and tag in self.block_tags:
            self.open_blocks.append(
                _OpenRawBlock(
                    tag_name=tag,
                    path=self._path(tag),
                    outer_start=start,
                    inner_start=end,
                )
            )
        if tag not in _VOID_TAGS:
            self.stack.append((tag, start))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._skipped() or tag not in self.block_tags:
            return
        start = self._offset()
        raw = self.get_starttag_text() or self.raw_text[start:start]
        end = start + len(raw)
        self.blocks.append(
            _ParsedRawBlock(
                tag_name=tag,
                path=self._path(tag),
                outer_start=start,
                outer_end=end,
                inner_start=end,
                inner_end=end,
            )
        )

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        end_start = self._offset()
        raw_match = re.match(r"</\s*[^>]+>", self.raw_text[end_start:])
        raw = raw_match.group(0) if raw_match else f"</{tag}>"
        end = end_start + len(raw)
        if self.open_blocks and self.open_blocks[-1].tag_name == tag:
            block = self.open_blocks.pop()
            self.blocks.append(
                _ParsedRawBlock(
                    tag_name=block.tag_name,
                    path=block.path,
                    outer_start=block.outer_start,
                    outer_end=end,
                    inner_start=block.inner_start,
                    inner_end=end_start,
                )
            )
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


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


def prose_span_from_epub_ref(span_ref: EpubSpanRef) -> ProseSpan:
    """Convert an EPUB span reference to a ProseSpan for sentence segmentation."""
    return ProseSpan(
        text=span_ref.source_text,
        placeholders=span_ref.placeholders,
        protected_terms=span_ref.protected_terms,
    )


def build_raw_block_index(structured: Any) -> dict[str, _RawBlock]:
    """Map epub2text block ids to raw archive offsets and fragments."""
    blocks_by_document_id: dict[str, list[Any]] = {}
    for block in structured.blocks:
        blocks_by_document_id.setdefault(block.document_id, []).append(block)

    raw_index: dict[str, _RawBlock] = {}
    with zipfile.ZipFile(structured.source_path) as archive:
        archive_names = archive.namelist()
        for document in structured.documents:
            structured_blocks = blocks_by_document_id.get(document.document_id, [])
            if not structured_blocks:
                continue

            href = _resolve_archive_href(document.href, archive_names)
            raw_bytes = archive.read(href)
            encoding = document.encoding or "utf-8"
            raw_text = raw_bytes.decode(encoding)
            block_tags = frozenset(block.tag_name for block in structured_blocks)
            raw_parser = _RawBlockParser(raw_text, block_tags=block_tags)
            raw_parser.feed(raw_text)
            raw_parser.close()

            raw_blocks = raw_parser.blocks
            if len(raw_blocks) != len(structured_blocks):
                raise ValueError(
                    f"Could not map EPUB document {href!r}: parsed {len(raw_blocks)} "
                    f"raw text blocks but epub2text reported {len(structured_blocks)}."
                )

            raw_digest = sha256(raw_bytes).hexdigest()
            for block, raw_block in zip(structured_blocks, raw_blocks, strict=True):
                if (
                    raw_block.tag_name != block.tag_name
                    or raw_block.path != block.element_path
                ):
                    raise ValueError(
                        f"Could not map EPUB block {block.id} back to raw source XHTML."
                    )

                raw_index[block.id] = _RawBlock(
                    href=href,
                    raw_sha256=raw_digest,
                    tag_name=block.tag_name,
                    text=_normalize_block_text(block.text),
                    outer_start=raw_block.outer_start,
                    outer_end=raw_block.outer_end,
                    inner_start=raw_block.inner_start,
                    inner_end=raw_block.inner_end,
                    source_fragment=raw_text[
                        raw_block.inner_start : raw_block.inner_end
                    ],
                )
    return raw_index


def structured_to_text2epub_manifest(
    structured: Any, *, raw_block_index: dict[str, _RawBlock] | None = None
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
    structured: Any,
    *,
    protected_terms: list[str],
    raw_block_index: dict[str, _RawBlock] | None = None,
) -> tuple[list[ProseSpan], list[EpubSpanRef]]:
    """Build booktx spans plus ordered span refs from epub2text blocks."""
    raw_block_index = raw_block_index or build_raw_block_index(structured)
    from booktx.epub_inline_xhtml import (
        INLINE_XHTML_CODEC,
        inline_skeleton,
        protect_names_in_xhtml_text_nodes,
    )

    # epub2text stores sentence-level segments globally on the structured
    # extraction, keyed by block id. TextBlock has no ``segments`` attribute, so
    # looking it up on the block always missed and booktx fell back to the full
    # block XHTML fragment. Consume the upstream segments here so sentence
    # detection already happened on visible block text.
    segments_by_block_id: dict[str, list[Any]] = {}
    for segment in getattr(structured, "segments", []) or []:
        segments_by_block_id.setdefault(segment.block_id, []).append(segment)
    for items in segments_by_block_id.values():
        items.sort(
            key=lambda segment: (segment.block_text_start, segment.block_text_end)
        )

    spans: list[ProseSpan] = []
    span_refs: list[EpubSpanRef] = []

    for block in structured.blocks:
        if not block.text or not block.text.strip():
            continue

        block_fragment = (
            getattr(getattr(block, "xhtml_fragment", None), "xhtml", "") or block.text
        )
        source_view_sha256 = sha256(block_fragment.encode("utf-8")).hexdigest()
        segment_items = segments_by_block_id.get(block.id) or [block]
        block_has_upstream_segments = block.id in segments_by_block_id
        first_span_index = len(spans)
        raw_block = raw_block_index[block.id]
        for segment in segment_items:
            segment_text = getattr(segment, "text", block.text)
            segment_fragment = (
                getattr(getattr(segment, "xhtml_fragment", None), "xhtml", "")
                or segment_text
            )
            if not segment_text or not str(segment_text).strip():
                continue
            protected_text, record_placeholders = protect_names_in_xhtml_text_nodes(
                str(segment_fragment), protected_terms
            )
            names = [placeholder.original for placeholder in record_placeholders]
            spans.append(
                ProseSpan(
                    text=protected_text,
                    placeholders=record_placeholders,
                    protected_terms=names,
                    presegmented=block_has_upstream_segments,
                )
            )
        span_ref = EpubSpanRef(
            span_index=first_span_index,
            block_id=block.id,
            document_href=block.document_href,
            spine_index=block.spine_index,
            tag_name=block.tag_name,
            source_text=block.text,
            source_text_sha256=sha256(block.text.encode("utf-8")).hexdigest(),
            source_char_start=raw_block.inner_start,
            source_char_end=raw_block.inner_end,
            placeholders=[],
            protected_terms=[],
            source_view_text=str(block_fragment),
            source_view_sha256=source_view_sha256,
            source_markup=INLINE_XHTML_CODEC,
            inline_skeleton=[
                asdict(token) for token in inline_skeleton(str(block_fragment))
            ],
        )
        span_refs.append(span_ref)

    return spans, span_refs


def structured_to_navigation_refs(structured: Any) -> list[EpubNavigationRef]:
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
