"""Chapter detection and chapter-map persistence for booktx.

Chapter maps are additive metadata. They do not change chunk JSON or translated
chunk JSON. Detection is deterministic and local: markdown uses heading inline
spans, EPUB uses spine-document boundaries plus h1-h6 blocks.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict, Field

from booktx.chunking import ProseSpan, segment_spans
from booktx.config import find_source_file, load_names
from booktx.context import chapter_map_path
from booktx.epub_io import _item_content_str, _spine_documents, extract_epub, read_epub
from booktx.html_io import _translatable_blocks, parse_xhtml
from booktx.markdown_io import extract_markdown, split_front_matter

if TYPE_CHECKING:
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


# --- persistence -------------------------------------------------------------


def load_chapter_map(project: Project) -> ChapterMap | None:
    path = chapter_map_path(project)
    if not path.is_file():
        return None
    return ChapterMap.model_validate_json(path.read_text("utf-8"))


def write_chapter_map(project: Project, chapter_map: ChapterMap) -> None:
    path = chapter_map_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        chapter_map.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )


# --- detection ---------------------------------------------------------------


def detect_chapters(project: Project) -> ChapterMap:
    """Detect chapters and write no files.

    The project should already have chunks (normally after ``booktx extract``)
    so record ids can be mapped exactly to existing chunk files.
    """
    source = find_source_file(project)
    names = load_names(project).protected_terms
    if project.config.format == "markdown":
        text = source.read_text("utf-8")
        extraction = extract_markdown(text, protected_terms=names)
        boundaries = _markdown_boundaries(text)
        spans = extraction.spans
    elif project.config.format == "epub":
        extraction = extract_epub(str(source), protected_terms=names)
        spans = extraction.spans
        boundaries = _epub_boundaries(str(source))
    else:  # pragma: no cover - config validation guards this
        spans = []
        boundaries = []

    record_ids = _project_record_ids(project)
    return _build_chapter_map(spans, boundaries, record_ids)


def _project_record_ids(project: Project) -> list[str]:
    record_ids: list[str] = []
    for chunk_path in project.chunks():
        from booktx.models import Chunk

        chunk = Chunk.model_validate_json(chunk_path.read_text("utf-8"))
        record_ids.extend(record.id for record in chunk.records)
    return record_ids


def _span_record_starts(spans: list[ProseSpan], *, language: str = "en") -> list[int]:
    starts: list[int] = []
    count = 0
    for span in spans:
        starts.append(count)
        count += len(segment_spans([span], language=language))
    return starts


def _build_chapter_map(
    spans: list[ProseSpan], boundaries: list[_Boundary], record_ids: list[str]
) -> ChapterMap:
    if not record_ids:
        return ChapterMap(chapters=[])

    starts = _span_record_starts(spans)
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
    for start, title in sorted(candidates, key=lambda x: x[0]):
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


# --- markdown headings -------------------------------------------------------


def _markdown_boundaries(text: str) -> list[_Boundary]:
    _front_matter, body = split_front_matter(text)
    md = MarkdownIt("commonmark", {"html": True}).enable("table")
    tokens = md.parse(body)
    open_blocks: list[str] = []
    inline_index = -1
    boundaries: list[_Boundary] = []
    for tok in tokens:
        ttype = tok.type
        if ttype.endswith("_open"):
            open_blocks.append(ttype[: -len("_open")])
            continue
        if ttype.endswith("_close"):
            if open_blocks:
                open_blocks.pop()
            continue
        if ttype != "inline" or not tok.content.strip():
            continue
        parent = open_blocks[-1] if open_blocks else ""
        if parent in _TRANSLATABLE_MARKDOWN_PARENTS:
            inline_index += 1
            if parent == "heading":
                boundaries.append(_Boundary(inline_index, tok.content.strip()))
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


# --- EPUB headings -----------------------------------------------------------


def _epub_boundaries(path: str) -> list[_Boundary]:
    book = read_epub(path)
    boundaries: list[_Boundary] = []
    span_offset = 0
    for doc in _spine_documents(book):
        content = _item_content_str(doc)
        blocks = _epub_blocks_with_text(content)
        if not blocks:
            continue
        doc_title = _first_heading_title(blocks) or _epub_doc_title(doc)
        boundaries.append(_Boundary(span_offset, doc_title))
        for local_index, (tag_name, text) in enumerate(blocks):
            if tag_name not in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                continue
            if text == doc_title:
                continue
            boundaries.append(_Boundary(span_offset + local_index, text))
        span_offset += len(blocks)
    return boundaries


def _epub_blocks_with_text(xhtml: str) -> list[tuple[str, str]]:
    soup = parse_xhtml(xhtml)
    out: list[tuple[str, str]] = []
    for block in _translatable_blocks(soup):
        text = " ".join(block.get_text(" ", strip=True).split())
        if text:
            out.append((str(block.name), text))
    return out


def _first_heading_title(blocks: list[tuple[str, str]]) -> str:
    for tag_name, text in blocks:
        if tag_name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            return text
    return ""


def _epub_doc_title(doc) -> str:
    title = getattr(doc, "title", "") or ""
    if title.strip():
        return title.strip()
    return Path(getattr(doc, "file_name", "")).stem
