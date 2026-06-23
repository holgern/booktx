"""Typed project status snapshot and runtime index.

This module owns the record/word/chunk/chapter aggregation that used to live
as the private ``_project_status_snapshot`` dict inside :mod:`booktx.cli`. It
returns a typed :class:`StatusSnapshot` (which serializes to the stable
``status --json`` v1 shape) plus a :class:`StatusRuntimeIndex` dataclass that
carries the lookup maps the command layer still needs for task creation and
record acceptance.

Design notes:

- The public JSON shape is preserved exactly, including the nested
  ``record_range: {start, end}`` field on each chapter. Do not flatten it to
  ``start_record_id``/``end_record_id`` without intentionally versioning to v2.
- The snapshot model has *only* public fields; no ``_private`` keys leak into
  JSON output. Runtime lookup maps live on the separate
  :class:`StatusRuntimeIndex` so they never get serialized.
- This module is intentionally free of Typer/Rich imports so it can be unit
  tested directly. CLI-specific error UX (``_die``) stays in ``cli.py``; the
  caller computes ``context_exists``/``context_ready`` and passes them in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from booktx.chapters import (
    ChapterMap,
    detect_chapters,
    load_chapter_map,
    write_chapter_map,
)
from booktx.config import (
    Project,
    current_source_sha256,
    extracted_source_sha256,
    find_source_file,
    load_manifest,
    load_translation_store,
    load_translation_version_ledger,
    project_source_sha256,
)
from booktx.models import Chunk, TranslatedRecord
from booktx.progress import SourceRecordView, load_source_chunks, load_source_records
from booktx.translation_store import active_candidate
from booktx.validate import (
    Severity,
    load_effective_translated_chunks,
)

if TYPE_CHECKING:
    from booktx.validate import Finding


__all__ = [
    "RecordRange",
    "ChapterProgress",
    "ChunkProgress",
    "SourceStatus",
    "ContextStatus",
    "StatusTotals",
    "VersionCoverage",
    "TrackCoverage",
    "StatusSnapshot",
    "StatusRuntimeIndex",
    "StatusBundle",
    "coverage_status",
    "build_status_snapshot",
    "selected_chapter",
]


def coverage_status(*, total: int, translated: int, has_error: bool) -> str:
    """Return the coverage status label for a chunk or chapter."""
    if has_error:
        return "invalid"
    if translated <= 0:
        return "pending"
    if translated >= total:
        return "complete"
    return "in_progress"


class RecordRange(BaseModel):
    """Inclusive record-id range covered by a chapter (nested in JSON v1)."""

    model_config = ConfigDict(extra="forbid")

    start: str
    end: str


class ChunkProgress(BaseModel):
    """Per-chunk translation coverage (matches the ``status --json`` v1 shape)."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    records_total: int
    records_translated: int
    records_remaining: int
    source_words_total: int
    source_words_translated: int
    source_words_remaining: int
    status: str


class ChapterProgress(BaseModel):
    """Per-chapter translation coverage (matches the ``status --json`` v1 shape)."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str
    title: str
    chunk_ids: list[str] = Field(default_factory=list)
    pending_chunk_ids: list[str] = Field(default_factory=list)
    record_range: RecordRange
    records_total: int
    records_translated: int
    records_remaining: int
    source_words_total: int
    source_words_translated: int
    source_words_remaining: int
    status: str


class SourceStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str
    format: str
    source_language: str
    target_language: str
    source_sha256: str
    source_drifted: bool = False


class ContextStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exists: bool
    ready: bool


class StatusTotals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_words: int = 0
    translated_words: int = 0
    remaining_words: int = 0
    records_total: int = 0
    records_translated: int = 0
    records_remaining: int = 0
    chunks_total: int = 0
    chunks_complete: int = 0
    chunks_partial: int = 0
    chunks_pending: int = 0
    chapters_total: int = 0
    chapters_complete: int = 0
    chapters_partial: int = 0
    chapters_pending: int = 0
    invalid_translation_files: int = 0
    stale_translation_files: int = 0


class VersionCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_ref: str
    version: int
    subversion: int
    records_with_candidate: int = 0
    active_records: int = 0


class TrackCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    label: str | None = None
    records_with_candidate: int = 0
    active_records: int = 0
    latest_subversion: int | None = None


class StatusSnapshot(BaseModel):
    """Typed project status. Serializes to the ``status --json`` v1 payload."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    project: str
    source: SourceStatus
    context: ContextStatus
    totals: StatusTotals
    next: ChapterProgress | None = None
    chapters: list[ChapterProgress] = Field(default_factory=list)
    version_coverage: list[VersionCoverage] = Field(default_factory=list)
    track_coverage: list[TrackCoverage] = Field(default_factory=list)


@dataclass(slots=True)
class StatusRuntimeIndex:
    """Lookup maps derived while building a :class:`StatusSnapshot`.

    These are intentionally not serialized. They carry the live objects the
    command layer needs for task creation, record acceptance, and selection.
    """

    source_chunks: dict[str, Chunk]
    source_by_id: dict[str, SourceRecordView]
    translated_by_id: dict[str, TranslatedRecord]
    record_ids_by_chapter: dict[str, list[str]]
    record_to_chapter: dict[str, str]
    chapters_by_id: dict[str, ChapterProgress]
    chunk_summaries: list[ChunkProgress]
    record_error_by_id: dict[str, Finding]


@dataclass(slots=True)
class StatusBundle:
    """A status snapshot paired with its runtime index."""

    snapshot: StatusSnapshot
    index: StatusRuntimeIndex


def _chapter_map_for_workflow(proj: Project) -> ChapterMap:
    source_sha256 = project_source_sha256(proj)
    chapter_map = load_chapter_map(proj)
    if chapter_map is None or chapter_map.source_sha256 != source_sha256:
        chapter_map = detect_chapters(proj)
        write_chapter_map(proj, chapter_map)
    return chapter_map


def build_status_snapshot(
    proj: Project,
    *,
    context_exists: bool,
    context_ready: bool,
) -> StatusBundle:
    """Build a typed status snapshot and runtime index for ``proj``.

    ``context_exists``/``context_ready`` are computed by the caller (the CLI
    owns the invalid-context error UX). Everything else — source/chunk
    loading, effective-translation merge, chapter mapping, coverage math — is
    owned here.
    """
    source_path = find_source_file(proj)
    manifest = load_manifest(proj)
    source_chunks = {chunk.chunk_id: chunk for chunk in load_source_chunks(proj)}
    source_records = load_source_records(proj)
    chapter_map = _chapter_map_for_workflow(proj)
    effective = load_effective_translated_chunks(proj, source_chunks=source_chunks)

    source_by_id = {record.record_id: record for record in source_records}
    translated_by_id: dict[str, TranslatedRecord] = {
        record.id: record
        for chunk in effective.chunks.values()
        for record in chunk.records
    }
    findings = effective.findings
    record_error_by_id: dict[str, Finding] = {
        finding.record_id: finding
        for finding in findings
        if finding.severity == Severity.ERROR and finding.record_id
    }
    chunk_has_error = {
        finding.chunk_id
        for finding in findings
        if finding.severity == Severity.ERROR
        and finding.chunk_id not in {"context", "store"}
    }

    ordered_record_ids = [record.record_id for record in source_records]
    record_index_by_id = {
        record_id: idx for idx, record_id in enumerate(ordered_record_ids)
    }
    record_ids_by_chapter: dict[str, list[str]] = {}
    record_to_chapter: dict[str, str] = {}

    for chapter in chapter_map.chapters:
        start = record_index_by_id.get(chapter.start_record_id)
        end = record_index_by_id.get(chapter.end_record_id)
        if start is None or end is None or end < start:
            ids: list[str] = []
        else:
            ids = ordered_record_ids[start : end + 1]
        record_ids_by_chapter[chapter.chapter_id] = ids
        for record_id in ids:
            record_to_chapter[record_id] = chapter.chapter_id

    chunk_summaries: list[ChunkProgress] = []
    for chunk in source_chunks.values():
        chunk_record_ids = [record.id for record in chunk.records]
        translated = [
            record_id for record_id in chunk_record_ids if record_id in translated_by_id
        ]
        source_words_total = sum(
            source_by_id[record_id].source_words for record_id in chunk_record_ids
        )
        source_words_translated = sum(
            source_by_id[record_id].source_words for record_id in translated
        )
        chunk_summaries.append(
            ChunkProgress(
                chunk_id=chunk.chunk_id,
                records_total=len(chunk_record_ids),
                records_translated=len(translated),
                records_remaining=len(chunk_record_ids) - len(translated),
                source_words_total=source_words_total,
                source_words_translated=source_words_translated,
                source_words_remaining=source_words_total - source_words_translated,
                status=coverage_status(
                    total=len(chunk_record_ids),
                    translated=len(translated),
                    has_error=chunk.chunk_id in chunk_has_error,
                ),
            )
        )

    chapter_summaries: list[ChapterProgress] = []
    for chapter in chapter_map.chapters:
        chapter_record_ids = record_ids_by_chapter.get(chapter.chapter_id, [])
        translated = [
            record_id
            for record_id in chapter_record_ids
            if record_id in translated_by_id
        ]
        pending = [
            record_id
            for record_id in chapter_record_ids
            if record_id not in translated_by_id
        ]
        pending_chunk_ids: list[str] = []
        seen_pending_chunks: set[str] = set()
        for record_id in pending:
            chunk_id = source_by_id[record_id].chunk_id
            if chunk_id in seen_pending_chunks:
                continue
            seen_pending_chunks.add(chunk_id)
            pending_chunk_ids.append(chunk_id)
        source_words_total = sum(
            source_by_id[record_id].source_words for record_id in chapter_record_ids
        )
        source_words_translated = sum(
            source_by_id[record_id].source_words for record_id in translated
        )
        chapter_summaries.append(
            ChapterProgress(
                chapter_id=chapter.chapter_id,
                title=chapter.title,
                chunk_ids=list(chapter.chunk_ids),
                pending_chunk_ids=pending_chunk_ids,
                record_range=RecordRange(
                    start=chapter.start_record_id, end=chapter.end_record_id
                ),
                records_total=len(chapter_record_ids),
                records_translated=len(translated),
                records_remaining=len(chapter_record_ids) - len(translated),
                source_words_total=source_words_total,
                source_words_translated=source_words_translated,
                source_words_remaining=source_words_total - source_words_translated,
                status=coverage_status(
                    total=len(chapter_record_ids),
                    translated=len(translated),
                    has_error=any(
                        chunk_id in chunk_has_error for chunk_id in chapter.chunk_ids
                    ),
                ),
            )
        )

    chapters_by_id = {chapter.chapter_id: chapter for chapter in chapter_summaries}
    next_chapter = next(
        (chapter for chapter in chapter_summaries if chapter.records_remaining > 0),
        None,
    )

    total_source_words = sum(record.source_words for record in source_records)
    translated_source_words = sum(
        source_by_id[record_id].source_words for record_id in translated_by_id
    )
    chunks_complete = sum(
        1
        for chunk in chunk_summaries
        if chunk.records_translated == chunk.records_total
    )
    chunks_partial = sum(
        1
        for chunk in chunk_summaries
        if 0 < chunk.records_translated < chunk.records_total
    )
    chunks_pending = len(chunk_summaries) - chunks_complete - chunks_partial
    chapters_complete = sum(
        1
        for chapter in chapter_summaries
        if chapter.records_translated == chapter.records_total
    )
    chapters_partial = sum(
        1
        for chapter in chapter_summaries
        if 0 < chapter.records_translated < chapter.records_total
    )
    chapters_pending = len(chapter_summaries) - chapters_complete - chapters_partial

    source_sha256 = (
        manifest.source.sha256
        if manifest is not None and manifest.source.sha256
        else project_source_sha256(proj)
    )
    extracted_sha = extracted_source_sha256(proj)
    source_drifted = (
        bool(extracted_sha) and extracted_sha != current_source_sha256(proj)
    )

    snapshot = StatusSnapshot(
        version=1,
        project=str(proj.root),
        source=SourceStatus(
            filename=source_path.name,
            format=proj.config.format,
            source_language=proj.config.source_language,
            target_language=proj.config.target_language,
            source_sha256=source_sha256,
            source_drifted=source_drifted,
        ),
        context=ContextStatus(exists=context_exists, ready=context_ready),
        totals=StatusTotals(
            source_words=total_source_words,
            translated_words=translated_source_words,
            remaining_words=total_source_words - translated_source_words,
            records_total=len(source_records),
            records_translated=len(translated_by_id),
            records_remaining=len(source_records) - len(translated_by_id),
            chunks_total=len(chunk_summaries),
            chunks_complete=chunks_complete,
            chunks_partial=chunks_partial,
            chunks_pending=chunks_pending,
            chapters_total=len(chapter_summaries),
            chapters_complete=chapters_complete,
            chapters_partial=chapters_partial,
            chapters_pending=chapters_pending,
            invalid_translation_files=len(chunk_has_error),
            stale_translation_files=len(
                {
                    finding.chunk_id
                    for finding in findings
                    if finding.rule == "stale_translation"
                }
            ),
        ),
        next=next_chapter,
        chapters=[],
    )

    try:
        store = load_translation_store(proj)
        ledger = load_translation_version_ledger(proj)
    except Exception:
        store = None
        ledger = None

    if store is not None:
        version_counts: dict[str, VersionCoverage] = {}
        track_counts: dict[int, TrackCoverage] = {}
        for stored in store.records.values():
            seen_track_versions: set[int] = set()
            for candidate in stored.versions:
                coverage = version_counts.setdefault(
                    candidate.version_ref,
                    VersionCoverage(
                        version_ref=candidate.version_ref,
                        version=candidate.version,
                        subversion=candidate.subversion,
                    ),
                )
                coverage.records_with_candidate += 1
                if stored.active_version == candidate.version_ref:
                    coverage.active_records += 1

                track = track_counts.setdefault(
                    candidate.version,
                    TrackCoverage(version=candidate.version),
                )
                track.latest_subversion = max(
                    track.latest_subversion or 0, candidate.subversion
                )
                if candidate.version not in seen_track_versions:
                    track.records_with_candidate += 1
                    seen_track_versions.add(candidate.version)
                if stored.active_version == candidate.version_ref:
                    track.active_records += 1

        if ledger is not None:
            for track_id, track in ledger.tracks.items():
                coverage = track_counts.setdefault(
                    int(track_id),
                    TrackCoverage(version=track.version),
                )
                coverage.label = track.label
                if track.subversions:
                    coverage.latest_subversion = max(
                        subversion.subversion for subversion in track.subversions.values()
                    )
        snapshot.version_coverage = [
            version_counts[key] for key in sorted(version_counts, key=lambda item: tuple(int(part) for part in item.split(".")))
        ]
        snapshot.track_coverage = [
            track_counts[key] for key in sorted(track_counts)
        ]

    index = StatusRuntimeIndex(
        source_chunks=source_chunks,
        source_by_id=source_by_id,
        translated_by_id=translated_by_id,
        record_ids_by_chapter=record_ids_by_chapter,
        record_to_chapter=record_to_chapter,
        chapters_by_id=chapters_by_id,
        chunk_summaries=chunk_summaries,
        record_error_by_id=record_error_by_id,
    )

    return StatusBundle(snapshot=snapshot, index=index)


def selected_chapter(
    bundle: StatusBundle, chapter_id: str | None
) -> ChapterProgress | None:
    """Return the focused chapter for the ``status``/``next`` commands.

    ``chapter_id=None`` selects the first chapter with remaining records
    (``snapshot.next``), which is ``None`` when everything is translated.
    A specific ``chapter_id`` returns that chapter whether or not it still has
    remaining records; the caller decides how to react to a complete chapter.
    Returns ``None`` only when the id is unknown.
    """
    if chapter_id is None:
        return bundle.snapshot.next
    return bundle.index.chapters_by_id.get(chapter_id)
