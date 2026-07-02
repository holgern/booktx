"""Domain workflow functions for source-record inspection and analysis.

Read-only workflows that load and assemble source records, chapters, and the
status snapshot, plus the source-analysis analyze/read orchestrators. The
thin Typer commands in :mod:`booktx.commands.source` delegate here. Not-found
cases raise :class:`booktx.errors.BooktxError` so the command layer can map
them to a non-zero exit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from booktx.chapters import detect_chapters, load_chapter_map
from booktx.config import (
    Project,
    list_profiles,
    load_manifest,
    load_profile_project,
    profile_source_analysis_markdown_path,
    profile_source_analysis_path,
    source_analysis_markdown_path,
    source_analysis_path,
)
from booktx.errors import BooktxError
from booktx.progress import SourceRecordView, load_source_records
from booktx.record_refs import parse_record_ref
from booktx.source_analysis import (
    SnapshotValidationError,
    SourceAnalysisReport,
    SourceAnalysisSnapshot,
    build_snapshot,
    build_source_analysis,
    read_canonical_report,
    read_snapshot,
    render_report_markdown,
)
from booktx.status import StatusBundle


@dataclass(frozen=True)
class ChapterSourceRecords:
    """One chapter's source records, ready for CLI rendering."""

    chapter_id: str
    title: str
    records: list[dict[str, str]]


def build_source_status_payload(proj: Project) -> dict[str, Any]:
    """Assemble the safe summary of extracted source state."""
    manifest = load_manifest(proj)
    source_records = load_source_records(proj)
    chapter_map = load_chapter_map(proj) or detect_chapters(proj)
    return {
        "source": "available" if proj.chunks() else "missing",
        "format": proj.config.format,
        "source_language": proj.config.source_language,
        "records": len(source_records),
        "chunks": len(proj.chunks()),
        "chapters": len(chapter_map.chapters),
        "source_sha256": manifest.source.sha256 if manifest is not None else "",
    }


def find_source_record(proj: Project, record_ref: str) -> SourceRecordView:
    """Resolve one source record by id or record ref; raise if unknown."""
    canonical_id = parse_record_ref(record_ref).canonical_id
    source_by_id = {record.record_id: record for record in load_source_records(proj)}
    record = source_by_id.get(canonical_id)
    if record is None:
        raise BooktxError(
            "unknown_source_record", f"unknown source record id: {canonical_id}"
        )
    return record


def collect_chapter_records(
    bundle: StatusBundle, chapter_id: str
) -> ChapterSourceRecords:
    """Collect all source records for one chapter from a status snapshot."""
    record_ids = bundle.index.record_ids_by_chapter.get(chapter_id)
    chapter = bundle.index.chapters_by_id.get(chapter_id)
    if not record_ids or chapter is None:
        raise BooktxError("unknown_chapter", f"unknown chapter id: {chapter_id}")
    records = [
        {"id": record_id, "source": bundle.index.source_by_id[record_id].source}
        for record_id in record_ids
    ]
    return ChapterSourceRecords(
        chapter_id=chapter.chapter_id, title=chapter.title, records=records
    )


# --- source analysis -------------------------------------------------------


@dataclass(frozen=True)
class SyncedProfile:
    """Outcome of writing one profile snapshot during --sync-profiles."""

    profile: str
    json_written: bool
    md_written: bool
    error: str | None = None


@dataclass(frozen=True)
class SourceAnalysisResult:
    """Result of a ``source analyze`` run (dry-run or written)."""

    report: SourceAnalysisReport
    canonical_json_written: bool = False
    canonical_md_written: bool = False
    canonical_md_error: str | None = None
    synced: list[SyncedProfile] = field(default_factory=list)

    @property
    def failed_syncs(self) -> list[SyncedProfile]:
        return [s for s in self.synced if not s.json_written or s.error]

    @property
    def refreshed_profiles(self) -> list[str]:
        return [s.profile for s in self.synced if s.json_written and not s.error]


@dataclass(frozen=True)
class SourceAnalysisRead:
    """A read result for ``source analysis`` (canonical or profile snapshot)."""

    kind: Literal["canonical", "snapshot"]
    report: SourceAnalysisReport | None = None
    snapshot: SourceAnalysisSnapshot | None = None
    missing: bool = False
    stale: bool = False
    hint: str = ""


def _profile_snapshot_targets(
    project: Project,
) -> list[tuple[str, Project]]:
    """Resolve every configured profile before any snapshot write starts."""
    return [
        (profile_name, load_profile_project(project.root, profile_name))
        for profile_name in list_profiles(project)
    ]


def analyze_source(
    project: Project,
    *,
    engine_requested: str = "auto",
    spacy_model: str | None = None,
    min_count: int = 2,
    ngram_max: int = 4,
    top: int = 200,
    include_common: bool = False,
    write: bool = False,
    sync_profiles: bool = False,
) -> SourceAnalysisResult:
    """Build the source-analysis report and optionally persist it.

    Without ``write`` this is a pure dry run: no canonical report, no Markdown,
    and no chapter-map writes. With ``write`` the canonical JSON is written
    atomically first, then the Markdown view; a Markdown write failure is
    reported without rolling back the valid canonical JSON.

    ``sync_profiles`` requires ``write`` and refreshes every profile snapshot
    with the same report payload and ``analysis_sha256``. Partial-write state
    is reported accurately and never claimed as full success.
    """
    from booktx.io_utils import utc_timestamp, write_json_text_atomic, write_text_atomic

    if sync_profiles and not write:
        raise BooktxError(
            "source_analysis_sync_requires_write",
            "--sync-profiles requires --write",
        )

    report = build_source_analysis(
        project,
        engine_requested=engine_requested,
        spacy_model=spacy_model,
        min_count=min_count,
        ngram_max=ngram_max,
        top=top,
        include_common=include_common,
        generated_at=utc_timestamp(),
    )
    profile_targets = _profile_snapshot_targets(project) if sync_profiles else []

    result = SourceAnalysisResult(report=report)
    if not write:
        return result

    canonical_json = source_analysis_path(project)
    canonical_md = source_analysis_markdown_path(project)
    write_json_text_atomic(canonical_json, report.model_dump_json(by_alias=True))
    result = SourceAnalysisResult(
        report=report,
        canonical_json_written=True,
    )
    try:
        write_text_atomic(canonical_md, render_report_markdown(report))
        result = SourceAnalysisResult(
            report=report,
            canonical_json_written=True,
            canonical_md_written=True,
        )
    except OSError as exc:
        # Markdown is a generated view; never roll back valid canonical JSON.
        result = SourceAnalysisResult(
            report=report,
            canonical_json_written=True,
            canonical_md_written=False,
            canonical_md_error=f"{canonical_md.name}: {exc}",
        )

    if not sync_profiles:
        return result

    synced: list[SyncedProfile] = []
    for profile_name, profile_project in profile_targets:
        snapshot = build_snapshot(
            report, profile=profile_name, generated_at=utc_timestamp()
        )
        snap_json = profile_source_analysis_path(profile_project)
        snap_md = profile_source_analysis_markdown_path(profile_project)
        json_written = False
        md_written = False
        error: str | None = None
        try:
            write_json_text_atomic(snap_json, snapshot.model_dump_json(by_alias=True))
            json_written = True
            try:
                write_text_atomic(snap_md, render_report_markdown(report))
                md_written = True
            except OSError as md_exc:
                error = f"{snap_md.name}: {md_exc}"
        except OSError as jexc:
            error = f"{snap_json.name}: {jexc}"
        synced.append(
            SyncedProfile(
                profile=profile_name,
                json_written=json_written,
                md_written=md_written,
                error=error,
            )
        )
    return SourceAnalysisResult(
        report=result.report,
        canonical_json_written=result.canonical_json_written,
        canonical_md_written=result.canonical_md_written,
        canonical_md_error=result.canonical_md_error,
        synced=synced,
    )


def read_source_analysis(project: Project, *, isolated: bool) -> SourceAnalysisRead:
    """Read canonical evidence (project root) or the current snapshot (profile).

    In profile-root isolated mode only the current profile snapshot is read,
    validated against the canonical ``analysis_sha256`` when one exists, and
    staleness is reported without exposing parent or sibling paths.
    """
    if isolated:
        if project.profile is None:
            raise BooktxError(
                "source_analysis_no_profile",
                "profile-root read requires an active profile",
            )
        snap_path = profile_source_analysis_path(project)
        canonical = read_canonical_report(project)
        try:
            # Isolated mode deliberately reads only the current profile's
            # snapshot. Runtime resolution has already validated its marker.
            read = read_snapshot(
                snap_path,
                expected_analysis_sha256=(
                    canonical.analysis_sha256 if canonical is not None else None
                ),
            )
        except SnapshotValidationError as exc:
            if exc.code == "source_analysis_snapshot_missing":
                return SourceAnalysisRead(kind="snapshot", missing=True, hint=str(exc))
            raise
        return SourceAnalysisRead(
            kind="snapshot",
            snapshot=read.snapshot,
            stale=read.stale,
            hint=read.hint,
        )

    report = read_canonical_report(project)
    if report is None:
        return SourceAnalysisRead(
            kind="canonical",
            missing=True,
            hint="no canonical source-analysis report found; "
            "run `booktx source analyze . --write` from the project root",
        )
    return SourceAnalysisRead(kind="canonical", report=report)


__all__ = [
    "ChapterSourceRecords",
    "SyncedProfile",
    "SourceAnalysisResult",
    "SourceAnalysisRead",
    "analyze_source",
    "build_source_status_payload",
    "collect_chapter_records",
    "find_source_record",
    "read_source_analysis",
]
