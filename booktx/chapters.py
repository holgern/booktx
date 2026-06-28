"""Chapter detection and chapter-map persistence for booktx."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict, Field

from booktx.chunking import ProseSpan, segment_spans
from booktx.config import (
    find_source_file,
    load_manifest,
    load_names,
    project_source_sha256,
)
from booktx.context import chapter_map_path
from booktx.epub_io import extract_epub
from booktx.epub_manifest import (
    load_epub_template_from_manifest,
    prose_span_from_epub_ref,
)
from booktx.placeholders import TRANSLATABLE_INLINE_PARENTS

if TYPE_CHECKING:
    from booktx.config import Project
    from booktx.models import EpubNavigationRef, EpubSpanRef, Record
__all__ = [
    "Chapter",
    "ChapterMap",
    "detect_chapters",
    "load_chapter_map",
    "write_chapter_map",
]

# Chapter-map algorithm/schema version. Bump when chapter detection changes
# so cached maps are regenerated even when the source SHA is unchanged. EPUB
# extract always writes the current version; ensure_chapter_map regenerates
# any cached map below this value.
CURRENT_CHAPTER_MAP_VERSION = 2


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

    version: int = CURRENT_CHAPTER_MAP_VERSION
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
    from booktx.io_utils import write_json_model_atomic

    write_json_model_atomic(chapter_map_path(project), chapter_map)


def load_chapter_map_only(project: Project) -> ChapterMap | None:
    """Return the cached chapter map without any detection or writes.

    Pure read counterpart to :func:`ensure_chapter_map`. Returns ``None`` when
    no chapter map has been cached yet.
    """
    return load_chapter_map(project)


def ensure_chapter_map(project: Project) -> ChapterMap:
    """Return a chapter map, detecting and persisting it if stale.

    Unlike :func:`load_chapter_map_only`, this may write
    ``.booktx/chapter-map.json`` when the cache is missing or records a
    different source hash. Use it in workflows that need an up-to-date map;
    use ``load_chapter_map_only`` for read-only status rendering.
    """
    source_sha256 = project_source_sha256(project)
    chapter_map = load_chapter_map(project)
    if (
        chapter_map is None
        or chapter_map.source_sha256 != source_sha256
        or chapter_map.version != CURRENT_CHAPTER_MAP_VERSION
    ):
        chapter_map = detect_chapters(project)
        write_chapter_map(project, chapter_map)
    return chapter_map


def detect_chapters(project: Project) -> ChapterMap:
    """Detect chapters and write no files."""
    source = find_source_file(project)
    source_sha256 = project_source_sha256(project)
    names = load_names(project).protected_terms
    if project.config.format == "markdown":
        from booktx.markdown_io import extract_markdown, split_front_matter

        text = source.read_text("utf-8")
        md_extraction = extract_markdown(text, protected_terms=names)
        boundaries = _markdown_boundaries(split_front_matter(text)[1])
        record_ids = _project_record_ids(project)
        return _build_chapter_map(
            md_extraction.spans,
            boundaries,
            record_ids,
            language=project.config.source_language,
            source_sha256=source_sha256,
        )
    if project.config.format == "epub":
        manifest = load_manifest(project)
        template = None
        if manifest is not None:
            try:
                template = load_epub_template_from_manifest(manifest)
            except ValueError:
                template = None
        if template is not None:
            chapter_mapping = template.chapter_mapping
            span_refs = template.spans
            navigation_refs = template.navigation
        else:
            epub_extraction = extract_epub(str(source), protected_terms=names)
            chapter_mapping = "epub2text-block-v1"
            span_refs = epub_extraction.span_refs
            navigation_refs = epub_extraction.navigation
        boundaries = _epub_boundaries_from_refs(
            span_refs, navigation_refs, chapter_mapping=chapter_mapping
        )
        records = _project_records(project)
        return _build_epub_chapter_map(boundaries, records, source_sha256=source_sha256)
    # pragma: no cover - config validation guards this branch
    return _build_chapter_map(
        [],
        [],
        [],
        language=project.config.source_language,
        source_sha256=source_sha256,
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
    source_sha256: str = "",
) -> ChapterMap:
    """Markdown chapter map: boundaries index into the prose span list."""
    if not record_ids:
        return ChapterMap(source_sha256=source_sha256, chapters=[])

    starts = _span_record_starts(spans, language=language)
    candidates: list[tuple[int, str]] = []
    for boundary in boundaries:
        if boundary.span_index >= len(starts):
            continue
        record_start = starts[boundary.span_index]
        if record_start < len(record_ids):
            candidates.append((record_start, boundary.title.strip()))
    return _assemble_chapters(candidates, record_ids, source_sha256=source_sha256)


def _assemble_chapters(
    candidates: list[tuple[int, str]],
    record_ids: list[str],
    *,
    source_sha256: str = "",
) -> ChapterMap:
    """Turn ``(record_position, title)`` candidates into contiguous chapters."""
    if not record_ids:
        return ChapterMap(source_sha256=source_sha256, chapters=[])
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
    return ChapterMap(source_sha256=source_sha256, chapters=chapters)


def _project_records(project: Project) -> list[Record]:
    """Ordered ``Record`` objects across all chunks (canonical chunk order)."""
    from booktx.models import Chunk

    records: list[Record] = []
    for chunk_path in project.chunks():
        chunk = Chunk.model_validate_json(chunk_path.read_text("utf-8"))
        records.extend(chunk.records)
    return records


def _record_positions_by_span(records: list[Record]) -> dict[int, int]:
    """Map each ``Record.span_index`` to its first position in the record array."""
    positions: dict[int, int] = {}
    for position, record in enumerate(records):
        if record.span_index is not None:
            positions.setdefault(record.span_index, position)
    return positions


def _build_epub_chapter_map(
    boundaries: list[_Boundary],
    records: list[Record],
    *,
    source_sha256: str = "",
) -> ChapterMap:
    """Build a chapter map by resolving EPUB boundaries through canonical records.

    Each EPUB boundary carries an ``EpubSpanRef.span_index`` (a prose-span
    index). It is resolved to a record-array position via ``Record.span_index``.
    A boundary whose span_index has no matching record is a manifest/extraction
    inconsistency and raises instead of silently defaulting to record zero.
    """
    record_ids = [record.id for record in records]
    if not record_ids:
        return ChapterMap(source_sha256=source_sha256, chapters=[])
    positions_by_span = _record_positions_by_span(records)
    candidates: list[tuple[int, str]] = []
    for boundary in boundaries:
        position = positions_by_span.get(boundary.span_index)
        if position is None:
            raise ValueError(
                "EPUB chapter boundary at span_index="
                f"{boundary.span_index} (title={boundary.title!r}) has no "
                "matching extracted record; the source manifest is inconsistent "
                "with the extracted chunks. Re-run `booktx extract`."
            )
        candidates.append((position, boundary.title.strip()))
    return _assemble_chapters(candidates, record_ids, source_sha256=source_sha256)


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


_TRANSLATABLE_MARKDOWN_PARENTS = TRANSLATABLE_INLINE_PARENTS

_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


_prose_span_from_ref = prose_span_from_epub_ref  # backward-compatible alias


def _heading_boundaries(span_refs: list[EpubSpanRef]) -> list[_Boundary]:
    """Heading-tag boundaries keyed by ``EpubSpanRef.span_index``."""
    return [
        _Boundary(span_ref.span_index, span_ref.source_text)
        for span_ref in span_refs
        if span_ref.tag_name in _HEADING_TAGS and span_ref.source_text.strip()
    ]


def _annotated_chapter_boundaries(span_refs: list[EpubSpanRef]) -> list[_Boundary]:
    """Authoritative boundaries from upstream ``chapter_id``/``chapter_title``.

    Emits one boundary per chapter transition, keyed by the first span_index
    carrying a new ``chapter_id``. An all-None annotation set returns ``[]``, a
    distinct authoritative "no assignment" result: callers must not fall back
    to legacy navigation when ``chapter_mapping`` is v1.
    """
    boundaries: list[_Boundary] = []
    last_chapter_id: str | None = None
    for span_ref in sorted(span_refs, key=lambda item: item.span_index):
        if not span_ref.chapter_id:
            continue
        if span_ref.chapter_id == last_chapter_id:
            continue
        boundaries.append(
            _Boundary(span_ref.span_index, (span_ref.chapter_title or "").strip())
        )
        last_chapter_id = span_ref.chapter_id
    return boundaries


def _epub_boundaries_from_refs(
    span_refs: list[EpubSpanRef],
    navigation_refs: list[EpubNavigationRef],
    *,
    chapter_mapping: str = "legacy",
) -> list[_Boundary]:
    heading_boundaries = _heading_boundaries(span_refs)
    if chapter_mapping == "epub2text-block-v1":
        # Authoritative upstream block annotations. Use them even when empty;
        # never fall back to legacy navigation for a v1 manifest.
        primary = _annotated_chapter_boundaries(span_refs)
        if primary and _looks_like_partial_numbered_chapter_sequence(
            primary, heading_boundaries
        ):
            return _merge_boundaries(primary, heading_boundaries)
        toc_extra = _toc_document_start_extras(span_refs, primary, heading_boundaries)
        if toc_extra:
            return _merge_boundaries(primary, toc_extra)
        return primary
    # Legacy manifests: conservative navigation-derived detection.
    nav_boundaries = _navigation_boundaries(span_refs, navigation_refs)
    if not nav_boundaries:
        return heading_boundaries
    if _looks_like_partial_numbered_chapter_sequence(
        nav_boundaries, heading_boundaries
    ):
        return _merge_boundaries(nav_boundaries, heading_boundaries)
    # Last resort: TOC-derived document starts for extracted documents whose
    # chapter-like titles are not yet covered by navigation or headings. Targets
    # without extracted spans are never used, so this cannot create empty
    # chapters for missing/truncated documents.
    toc_extra = _toc_document_start_extras(
        span_refs, nav_boundaries, heading_boundaries
    )
    if toc_extra:
        return _merge_boundaries(nav_boundaries, toc_extra)
    return nav_boundaries


def _boundary_ordinals(boundaries: list[_Boundary]) -> set[int]:
    from booktx.epub_toc_audit import chapter_ordinal

    return {
        ordinal
        for ordinal in (chapter_ordinal(b.title) for b in boundaries)
        if ordinal is not None
    }


def _looks_like_partial_numbered_chapter_sequence(
    nav_boundaries: list[_Boundary], heading_boundaries: list[_Boundary]
) -> bool:
    """True when navigation is a strict subset of a chapter-like heading sequence.

    Navigation must already cover at least one numbered chapter, headings must
    contain at least one numbered chapter not present in navigation, and every
    navigation ordinal must be backed by a heading. This keeps h2 section
    headings, front-matter, and non-numbered titles from triggering a merge.
    """
    nav_ordinals = _boundary_ordinals(nav_boundaries)
    heading_ordinals = _boundary_ordinals(heading_boundaries)
    if not nav_ordinals or not heading_ordinals:
        return False
    return nav_ordinals < heading_ordinals


def _merge_boundaries(*groups: list[_Boundary]) -> list[_Boundary]:
    """Combine boundary groups, sorted by span_index with deduplication.

    Two boundaries landing on the same span_index collapse into one; a titled
    boundary wins over an untitled one at the same position.
    """
    combined: list[_Boundary] = []
    for group in groups:
        combined.extend(group)
    combined.sort(key=lambda boundary: boundary.span_index)
    deduped: list[_Boundary] = []
    for boundary in combined:
        if deduped and deduped[-1].span_index == boundary.span_index:
            if boundary.title and not deduped[-1].title:
                deduped[-1] = boundary
            continue
        deduped.append(boundary)
    return deduped


def _toc_document_start_extras(
    span_refs: list[EpubSpanRef],
    nav_boundaries: list[_Boundary],
    heading_boundaries: list[_Boundary],
) -> list[_Boundary]:
    """Return TOC-derived boundaries for uncovered extracted chapter documents."""
    from booktx.epub_toc_audit import (
        chapter_ordinal,
        toc_document_start_boundaries,
    )

    covered = _boundary_ordinals(nav_boundaries) | _boundary_ordinals(
        heading_boundaries
    )
    extras: list[_Boundary] = []
    for span_index, title in toc_document_start_boundaries(span_refs):
        ordinal = chapter_ordinal(title)
        if ordinal is None or ordinal in covered:
            continue
        extras.append(_Boundary(span_index, title))
    return extras


def _navigation_boundaries(
    span_refs: list[EpubSpanRef], navigation_refs: list[EpubNavigationRef]
) -> list[_Boundary]:
    boundaries: list[_Boundary] = []
    for entry in sorted(navigation_refs, key=lambda item: (item.order, item.level)):
        if not entry.title.strip():
            continue
        span_index = _navigation_span_index(entry, span_refs)
        if span_index is None:
            continue
        boundaries.append(_Boundary(span_index, entry.title))
    return boundaries


def _navigation_span_index(
    entry: EpubNavigationRef, span_refs: list[EpubSpanRef]
) -> int | None:
    """Conservatively map a legacy navigation entry to an ``EpubSpanRef.span_index``.

    Legacy manifests predate upstream block annotations, so this re-derives the
    mapping from stored navigation metadata. It is intentionally conservative
    and returns ``None`` (no boundary) rather than guessing:

    - fallback navigation entries (``source == "fallback"``) are ignored;
    - unresolved fragments (``fragment is not None`` with no resolvable offset)
      are ignored — only a whole-document href maps to document start;
    - a whole-document href maps to document start only when its document href
      and spine index are both known (matches upstream ``_effective_start``);
    - an offset at or beyond all matching spans is ignored;
    - the returned value is the stored ``EpubSpanRef.span_index``, never a
      position inside the ``span_refs`` list.

    Legacy parity with upstream block ranges is not exact; re-extraction is the
    authoritative fix for an old project.
    """
    if entry.source == "fallback":
        return None
    # Unresolved fragment: never map (mirrors upstream _effective_start).
    if entry.fragment is not None and entry.source_char_start is None:
        return None
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
    matches.sort(key=lambda span_ref: span_ref.span_index)
    if entry.source_char_start is None:
        # Whole-document href: require a known document and spine.
        if entry.document_href is None or entry.spine_index is None:
            return None
        return matches[0].span_index
    for span_ref in matches:
        start = span_ref.source_char_start
        if start is not None and start >= entry.source_char_start:
            return span_ref.span_index
    # Offset beyond all matching spans: no boundary.
    return None
