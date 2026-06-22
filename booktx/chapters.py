"""Chapter detection and chapter-map persistence for booktx."""

from __future__ import annotations

from dataclasses import dataclass

from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict, Field

from booktx.chunking import ProseSpan, segment_spans
from booktx.config import find_source_file, load_manifest, load_names
from booktx.context import chapter_map_path
from booktx.epub_io import extract_epub
from booktx.epub_manifest import load_epub_template_from_manifest

if False:  # pragma: no cover
    from booktx.config import Project

__all__ = [
    "Chapter",
    "ChapterMap",
    "detect_chapters",
    "load_chapter_map",
    "write_chapter_map",
]


class Chapter(BaseModel):
    """One detected chapter and the chunk range it covers."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str
    title: str = ""
    chunk_ids: list[str] = Field(default_factory=list)
    start_record_id: str = ""
    end_record_id: str = ""
    record_count: int = 0


class ChapterMap(BaseModel):
    """Detected chapters for a project."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    source_sha256: str = ""
    chapters: list[Chapter] = Field(default_factory=list)


@dataclass(slots=True)
class _Boundary:
    span_index: int
    title: str


def load_chapter_map(project: Project) -> ChapterMap | None:
    path = chapter_map_path(project)
    if not path.is_file():
        return None
    return ChapterMap.model_validate_json(path.read_text("utf-8"))


def write_chapter_map(project: Project, chapter_map: ChapterMap) -> None:
    path = chapter_map_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(chapter_map.model_dump_json(indent=2) + "\n", encoding="utf-8")


def detect_chapters(project: Project) -> ChapterMap:
    """Detect chapters and write no files."""
    source = find_source_file(project)
    names = load_names(project).protected_terms
    if project.config.format == "markdown":
        from booktx.markdown_io import extract_markdown, split_front_matter

        text = source.read_text("utf-8")
        extraction = extract_markdown(text, protected_terms=names)
        boundaries = _markdown_boundaries(split_front_matter(text)[1])
        spans = extraction.spans
    elif project.config.format == "epub":
        manifest = load_manifest(project)
        if manifest is not None:
            try:
                template = load_epub_template_from_manifest(manifest)
            except ValueError:
                template = None
            if template is not None:
                spans = [_prose_span_from_ref(span_ref) for span_ref in template.spans]
                boundaries = _epub_boundaries_from_refs(
                    template.spans, template.navigation
                )
            else:
                extraction = extract_epub(str(source), protected_terms=names)
                spans = extraction.spans
                boundaries = _epub_boundaries_from_refs(
                    extraction.span_refs, extraction.navigation
                )
        else:
            extraction = extract_epub(str(source), protected_terms=names)
            spans = extraction.spans
            boundaries = _epub_boundaries_from_refs(
                extraction.span_refs, extraction.navigation
            )
    else:  # pragma: no cover - config validation guards this
        spans = []
        boundaries = []

    record_ids = _project_record_ids(project)
    return _build_chapter_map(
        spans,
        boundaries,
        record_ids,
        language=project.config.source_language,
    )


def _project_record_ids(project: Project) -> list[str]:
    from booktx.models import Chunk

    record_ids: list[str] = []
    for chunk_path in project.chunks():
        chunk = Chunk.model_validate_json(chunk_path.read_text("utf-8"))
        record_ids.extend(record.id for record in chunk.records)
    return record_ids


def _span_record_starts(spans: list[ProseSpan], *, language: str) -> list[int]:
    starts: list[int] = []
    count = 0
    for span in spans:
        starts.append(count)
        count += len(segment_spans([span], language=language))
    return starts


def _build_chapter_map(
    spans: list[ProseSpan],
    boundaries: list[_Boundary],
    record_ids: list[str],
    *,
    language: str,
) -> ChapterMap:
    if not record_ids:
        return ChapterMap(chapters=[])

    starts = _span_record_starts(spans, language=language)
    candidates: list[tuple[int, str]] = []
    for boundary in boundaries:
        if boundary.span_index >= len(starts):
            continue
        record_start = starts[boundary.span_index]
        if record_start < len(record_ids):
            candidates.append((record_start, boundary.title.strip()))

    if not candidates:
        candidates = [(0, "")]
    elif candidates[0][0] != 0:
        candidates.insert(0, (0, "Front matter"))

    deduped: list[tuple[int, str]] = []
    for start, title in sorted(candidates, key=lambda item: item[0]):
        if deduped and deduped[-1][0] == start:
            if title and not deduped[-1][1]:
                deduped[-1] = (start, title)
            continue
        deduped.append((start, title))

    chapters: list[Chapter] = []
    for idx, (start, title) in enumerate(deduped, start=1):
        next_start = deduped[idx][0] if idx < len(deduped) else len(record_ids)
        end = next_start - 1
        if start > end:
            continue
        chapter_records = record_ids[start : end + 1]
        chunk_ids = _unique_chunk_ids(chapter_records)
        chapters.append(
            Chapter(
                chapter_id=f"{len(chapters) + 1:04d}",
                title=title,
                chunk_ids=chunk_ids,
                start_record_id=chapter_records[0],
                end_record_id=chapter_records[-1],
                record_count=len(chapter_records),
            )
        )
    return ChapterMap(chapters=chapters)


def _unique_chunk_ids(record_ids: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for record_id in record_ids:
        chunk_id = record_id.split("-", 1)[0]
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        out.append(chunk_id)
    return out


def _markdown_boundaries(body: str) -> list[_Boundary]:
    tokens = MarkdownIt("commonmark", {"html": True}).enable("table").parse(body)
    open_blocks: list[str] = []
    inline_index = -1
    boundaries: list[_Boundary] = []
    for token in tokens:
        ttype = token.type
        if ttype.endswith("_open"):
            open_blocks.append(ttype[: -len("_open")])
            continue
        if ttype.endswith("_close"):
            if open_blocks:
                open_blocks.pop()
            continue
        if ttype != "inline" or not token.content.strip():
            continue
        parent = open_blocks[-1] if open_blocks else ""
        if parent in _TRANSLATABLE_MARKDOWN_PARENTS:
            inline_index += 1
            if parent == "heading":
                boundaries.append(_Boundary(inline_index, token.content.strip()))
    return boundaries


_TRANSLATABLE_MARKDOWN_PARENTS = {
    "paragraph",
    "heading",
    "list_item",
    "blockquote",
    "table_cell",
    "td",
    "th",
    "strong",
    "em",
}

_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def _prose_span_from_ref(span_ref) -> ProseSpan:
    return ProseSpan(
        text=span_ref.source_text,
        placeholders=span_ref.placeholders,
        protected_terms=span_ref.protected_terms,
    )


def _epub_boundaries_from_refs(span_refs, navigation_refs) -> list[_Boundary]:
    boundaries = _navigation_boundaries(span_refs, navigation_refs)
    if boundaries:
        return boundaries

    heading_boundaries = [
        _Boundary(span_ref.span_index, span_ref.source_text)
        for span_ref in span_refs
        if span_ref.tag_name in _HEADING_TAGS and span_ref.source_text.strip()
    ]
    return heading_boundaries


def _navigation_boundaries(span_refs, navigation_refs) -> list[_Boundary]:
    boundaries: list[_Boundary] = []
    for entry in sorted(navigation_refs, key=lambda item: (item.order, item.level)):
        if not entry.title.strip():
            continue
        span_index = _navigation_span_index(entry, span_refs)
        if span_index is None:
            continue
        boundaries.append(_Boundary(span_index, entry.title))
    return boundaries


def _navigation_span_index(entry, span_refs) -> int | None:
    matches = [
        span_ref
        for span_ref in span_refs
        if entry.document_href and span_ref.document_href == entry.document_href
    ]
    if not matches and entry.spine_index is not None:
        matches = [
            span_ref
            for span_ref in span_refs
            if span_ref.spine_index == entry.spine_index
        ]
    if not matches:
        return None
    if entry.source_char_start is None:
        return matches[0].span_index
    for span_ref in matches:
        start = span_ref.source_char_start
        if start is not None and start >= entry.source_char_start:
            return span_ref.span_index
    return matches[0].span_index
