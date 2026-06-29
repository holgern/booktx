"""Profile-local editor QA indexes for translation review.

This module builds three generated artifacts under ``translations/<profile>/``:

* ``source-index.json``      - source text only, every source record in order;
* ``target-index.json``      - effective target text only (no source prose);
* ``source-target-index.json`` - slim keyed source/target side-by-side view.

All three are rebuildable derived artifacts. They must never become canonical
state and must never be used as build input; the canonical state remains
``translation-store.json``.

The target text in the target-based indexes is taken from the same
build-trusted loader (``load_effective_translated_chunks``) that ``booktx
build`` trusts, so the exported targets match build output exactly. Store
version/review metadata is attached only when the store selection agrees with
that build output; otherwise the target-based indexes are not rewritten and a
clear error is reported.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from booktx.chapters import ChapterMap, ensure_chapter_map
from booktx.config import (
    Project,
    load_translation_store,
    project_source_sha256,
    translation_source_index_path,
    translation_source_target_index_path,
    translation_target_index_path,
)
from booktx.io_utils import write_text_atomic
from booktx.models import Chunk
from booktx.progress import load_source_chunks, source_record_sha256
from booktx.record_refs import parse_record_ref
from booktx.translation_store import (
    EffectiveCandidateError,
    EffectiveCandidateSelection,
    effective_candidate_selection,
    sha256_text,
)
from booktx.validate import Finding, Severity, load_effective_translated_chunks

__all__ = [
    "EditorIndexError",
    "EditorIndexesResult",
    "SourceIndex",
    "SourceIndexRecord",
    "SourceTargetIndex",
    "SourceTargetIndexRecord",
    "TargetIndex",
    "TargetIndexRecord",
    "build_chapter_record_map",
    "build_editor_indexes",
    "build_source_index",
    "export_editor_indexes",
    "write_compact_records_json",
]

_VALID_KINDS = ("source", "target", "source-target")
_ALL_KINDS = ("source", "target", "source-target")
# Provenance fields are omitted from the compact payload when empty/null so
# record lines stay compact; core fields are always present (nullable core
# fields such as ``chapter_id`` are still written as null).
_PROVENANCE_KEYS = (
    "target_sha256",
    "updated_at",
    "review_chain",
    "context_view_sha256",
)


# --- Pydantic models ---------------------------------------------------------


class _BaseRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceIndexRecord(_BaseRecord):
    chunk_id: str
    part_id: str
    chapter_id: str | None = None
    chapter_title: str | None = None
    source_sha256: str
    source: str


class TargetIndexRecord(_BaseRecord):
    chunk_id: str
    part_id: str
    chapter_id: str | None = None
    chapter_title: str | None = None
    version: str | None = None
    review: str | None = None
    selected_kind: Literal["translation", "review"]
    selected_ref: str
    target: str
    target_sha256: str | None = None
    updated_at: str | None = None
    review_chain: list[str] = Field(default_factory=list)
    context_view_sha256: str | None = None


class SourceTargetIndexRecord(_BaseRecord):
    chunk_id: str
    part_id: str
    chapter_id: str | None = None
    chapter_title: str | None = None
    source_sha256: str
    source: str
    active_version: str | None = None
    active_review: str | None = None
    version: str | None = None
    review: str | None = None
    selected_kind: Literal["translation", "review"] | None = None
    selected_ref: str | None = None
    target: str | None = None
    target_sha256: str | None = None
    updated_at: str | None = None
    review_chain: list[str] = Field(default_factory=list)
    context_view_sha256: str | None = None


class _BaseIndex(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SourceIndex(_BaseIndex):
    schema_name: Literal["booktx.source-index.v1"] = Field(
        default="booktx.source-index.v1", alias="schema"
    )
    profile: str
    source_sha256: str
    generated_at: str
    record_count: int
    records: dict[str, SourceIndexRecord]


class TargetIndex(_BaseIndex):
    schema_name: Literal["booktx.target-index.v1"] = Field(
        default="booktx.target-index.v1", alias="schema"
    )
    profile: str
    source_sha256: str
    generated_at: str
    record_count: int
    missing_count: int
    records: dict[str, TargetIndexRecord]


class SourceTargetIndex(_BaseIndex):
    schema_name: Literal["booktx.source-target-index.v1"] = Field(
        default="booktx.source-target-index.v1",
        alias="schema",
    )
    profile: str
    source_sha256: str
    generated_at: str
    record_count: int
    translated_count: int
    missing_count: int
    records: dict[str, SourceTargetIndexRecord]


class EditorIndexesResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_path: str | None = None
    target_path: str | None = None
    source_target_path: str | None = None
    source_record_count: int = 0
    target_record_count: int = 0
    source_target_record_count: int = 0
    translated_count: int = 0
    missing_count: int = 0
    warning_count: int = 0
    error_count: int = 0
    written: list[Literal["source", "target", "source-target"]] = Field(
        default_factory=list
    )


class EditorIndexError(Exception):
    """Raised when blocking findings prevent writing target-based indexes."""

    def __init__(self, findings: list[Finding], result: EditorIndexesResult) -> None:
        self.findings = findings
        self.result = result
        first = findings[0]
        location = f" [{first.record_id}]" if first.record_id else ""
        super().__init__(
            f"target indexes not written because translation data is invalid: "
            f"{first.chunk_id}{location} {first.rule}: {first.message}"
        )


# --- helpers -----------------------------------------------------------------


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _slim_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Drop empty/null provenance fields, keep every core field."""
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key in _PROVENANCE_KEYS:
            if value is None:
                continue
            if key == "review_chain" and not value:
                continue
        out[key] = value
    return out


def write_compact_records_json(
    path: Path,
    header: Mapping[str, Any],
    records: Mapping[str, Mapping[str, Any]],
) -> None:
    """Write a valid JSON file with one compact record payload per line.

    Top-level header keys are written one per line; each record is serialized
    on its own line inside the ``records`` object so ``rg``/``nvim`` hits show
    the record id and relevant text together. ``ensure_ascii=False`` keeps
    German and source punctuation searchable as typed.
    """
    lines: list[str] = ["{\n"]
    for key, value in header.items():
        lines.append(f"  {json.dumps(key)}: {json.dumps(value, ensure_ascii=False)},\n")
    lines.append('  "records": {\n')
    record_items = list(records.items())
    for index, (record_id, payload) in enumerate(record_items):
        suffix = "," if index + 1 < len(record_items) else ""
        lines.append(
            "    "
            + json.dumps(record_id, ensure_ascii=False)
            + ": "
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            + suffix
            + "\n"
        )
    lines.append("  }\n")
    lines.append("}\n")
    write_text_atomic(path, "".join(lines))


def build_chapter_record_map(
    source_chunks: list[Chunk], chapter_map: ChapterMap
) -> dict[str, dict[str, str | None]]:
    """Map each record id to its chapter id/title using chapter record ranges.

    Membership is resolved by record-id range (``start_record_id``..
    ``end_record_id``), never by chunk id alone, so chunks that span a chapter
    boundary stay correct.
    """
    ordered_ids = [record.id for chunk in source_chunks for record in chunk.records]
    index_by_id = {record_id: idx for idx, record_id in enumerate(ordered_ids)}
    chapter_by_record: dict[str, dict[str, str | None]] = {}
    for chapter in chapter_map.chapters:
        start = index_by_id.get(chapter.start_record_id)
        end = index_by_id.get(chapter.end_record_id)
        if start is None or end is None or start > end:
            continue
        meta = {
            "chapter_id": chapter.chapter_id,
            "chapter_title": chapter.title or None,
        }
        for record_id in ordered_ids[start : end + 1]:
            chapter_by_record[record_id] = meta
    return chapter_by_record


def _record_ids(source: Mapping[str, Any], stored: Any, field_name: str) -> Any:
    return getattr(stored, field_name, None) if stored is not None else None


def _source_payload(
    chunk_id: str,
    part_id: str,
    chapter: Mapping[str, Any],
    source_sha: str,
    source_text: str,
) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "part_id": part_id,
        "chapter_id": chapter.get("chapter_id"),
        "chapter_title": chapter.get("chapter_title"),
        "source_sha256": source_sha,
        "source": source_text,
    }


def _build_source_record_payloads(
    source_chunks: list[Chunk],
    chapter_by_record: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    payloads: dict[str, dict[str, Any]] = {}
    for chunk in source_chunks:
        for record in chunk.records:
            ref = parse_record_ref(record.id)
            chapter = chapter_by_record.get(record.id, {})
            payloads[record.id] = _source_payload(
                f"{ref.chunk_id:04d}",
                f"{ref.part_id:06d}",
                chapter,
                source_record_sha256(record.source),
                record.source,
            )
    return payloads


def _divergence_finding(
    record_id: str, chunk_id: str, rule: str, message: str
) -> Finding:
    return Finding(
        chunk_id=chunk_id,
        severity=Severity.ERROR,
        rule=rule,
        message=message,
        record_id=record_id,
    )


# --- index builders ----------------------------------------------------------


def build_source_index(project: Project) -> SourceIndex:
    """Build the source-only index from current source chunks and chapter map."""
    source_chunks = load_source_chunks(project)
    chapter_map = ensure_chapter_map(project)
    chapter_by_record = build_chapter_record_map(source_chunks, chapter_map)
    records = _build_source_record_payloads(source_chunks, chapter_by_record)
    slim = {rid: _slim_payload(payload) for rid, payload in records.items()}
    return SourceIndex(
        schema="booktx.source-index.v1",
        profile=project.profile or "",
        source_sha256=project_source_sha256(project),
        generated_at=_now_utc(),
        record_count=len(records),
        records=slim,  # type: ignore[arg-type]
    )


def build_editor_indexes(
    project: Project,
) -> tuple[SourceIndex, TargetIndex, SourceTargetIndex, list[Finding]]:
    """Build all three editor indexes plus the blocking/non-blocking findings.

    Target text is resolved through the build-trusted effective loader so the
    exported targets match ``booktx build`` exactly. Store metadata is attached
    only when an accepted store selection agrees with the build target;
    otherwise a blocking finding is recorded and the caller must not write the
    target-based indexes.
    """
    source_chunks = load_source_chunks(project)
    chapter_map = ensure_chapter_map(project)
    chapter_by_record = build_chapter_record_map(source_chunks, chapter_map)
    source_sha = project_source_sha256(project)
    profile = project.profile or ""
    generated_at = _now_utc()

    source_payloads = _build_source_record_payloads(source_chunks, chapter_by_record)

    source_chunk_map = {chunk.chunk_id: chunk for chunk in source_chunks}
    effective = load_effective_translated_chunks(
        project, source_chunks=source_chunk_map
    )
    build_targets: dict[str, str] = {}
    for translated_chunk in effective.chunks.values():
        for tr in translated_chunk.records:
            build_targets[tr.id] = tr.target

    store = load_translation_store(project)

    findings: list[Finding] = list(effective.findings)
    target_payloads: dict[str, dict[str, Any]] = {}
    source_target_payloads: dict[str, dict[str, Any]] = {}
    translated_count = 0

    for chunk in source_chunks:
        for record in chunk.records:
            record_id = record.id
            ref = parse_record_ref(record_id)
            chunk_id = f"{ref.chunk_id:04d}"
            part_id = f"{ref.part_id:06d}"
            chapter = chapter_by_record.get(record_id, {})
            source_sha_rec = source_record_sha256(record.source)
            source_base = _source_payload(
                chunk_id, part_id, chapter, source_sha_rec, record.source
            )
            stored = store.records.get(record_id)
            active_version = _record_ids(source_base, stored, "active_version")
            active_review = _record_ids(source_base, stored, "active_review")

            build_target = build_targets.get(record_id)
            if build_target is None:
                # Untranslated: no effective target. Keep every source field
                # and null selection fields for the side-by-side view.
                source_target_payloads[record_id] = {
                    **source_base,
                    "active_version": active_version,
                    "active_review": active_review,
                    "version": None,
                    "review": None,
                    "selected_kind": None,
                    "selected_ref": None,
                    "target": None,
                }
                continue

            selection: EffectiveCandidateSelection | EffectiveCandidateError | None
            selection = (
                effective_candidate_selection(stored, strict_active_review=True)
                if stored is not None
                else None
            )
            if isinstance(selection, EffectiveCandidateError):
                findings.append(
                    _divergence_finding(
                        record_id, chunk_id, selection.rule, selection.message
                    )
                )
                continue
            if selection is None:
                findings.append(
                    _divergence_finding(
                        record_id,
                        chunk_id,
                        "editor_index_legacy_contribution",
                        "build output has a target with no accepted store "
                        "selection; migrate or remove legacy translated chunks "
                        "before exporting editor indexes",
                    )
                )
                continue
            if selection.candidate.target != build_target:
                findings.append(
                    _divergence_finding(
                        record_id,
                        chunk_id,
                        "editor_index_target_divergence",
                        "store selection target does not match build output",
                    )
                )
                continue

            translated_count += 1
            target = selection.candidate.target
            updated_at = getattr(selection.candidate, "updated_at", None) or None
            context_view_sha = getattr(selection.candidate, "context_view_sha256", None)
            common = {
                "chunk_id": chunk_id,
                "part_id": part_id,
                "chapter_id": chapter.get("chapter_id"),
                "chapter_title": chapter.get("chapter_title"),
                "version": selection.version_ref,
                "review": selection.review_ref,
                "selected_kind": selection.selected_kind,
                "selected_ref": selection.selected_ref,
                "target": target,
                "target_sha256": sha256_text(target),
                "updated_at": updated_at,
                "review_chain": list(selection.review_chain),
                "context_view_sha256": context_view_sha,
            }
            target_payloads[record_id] = dict(common)
            source_target_payloads[record_id] = {
                **source_base,
                "active_version": active_version,
                "active_review": active_review,
                "version": common["version"],
                "review": common["review"],
                "selected_kind": common["selected_kind"],
                "selected_ref": common["selected_ref"],
                "target": common["target"],
                "target_sha256": common["target_sha256"],
                "updated_at": updated_at,
                "review_chain": list(selection.review_chain),
                "context_view_sha256": context_view_sha,
            }

    total = len(source_payloads)
    missing_count = total - translated_count

    source_slim = {
        rid: _slim_payload(payload) for rid, payload in source_payloads.items()
    }
    target_slim = {
        rid: _slim_payload(payload) for rid, payload in target_payloads.items()
    }
    source_target_slim = {
        rid: _slim_payload(payload) for rid, payload in source_target_payloads.items()
    }

    source_index = SourceIndex(
        schema="booktx.source-index.v1",
        profile=profile,
        source_sha256=source_sha,
        generated_at=generated_at,
        record_count=total,
        records=source_slim,  # type: ignore[arg-type]
    )
    target_index = TargetIndex(
        schema="booktx.target-index.v1",
        profile=profile,
        source_sha256=source_sha,
        generated_at=generated_at,
        record_count=len(target_slim),
        missing_count=missing_count,
        records=target_slim,  # type: ignore[arg-type]
    )
    source_target_index = SourceTargetIndex(
        schema="booktx.source-target-index.v1",
        profile=profile,
        source_sha256=source_sha,
        generated_at=generated_at,
        record_count=total,
        translated_count=translated_count,
        missing_count=missing_count,
        records=source_target_slim,  # type: ignore[arg-type]
    )
    return source_index, target_index, source_target_index, findings


# --- export orchestration ----------------------------------------------------


def _resolve_kinds(
    kinds: set[str] | None,
) -> set[Literal["source", "target", "source-target"]]:
    if not kinds:
        return {"source", "target", "source-target"}
    invalid = sorted(k for k in kinds if k not in _VALID_KINDS)
    if invalid:
        raise ValueError(
            f"invalid --kind value(s) {invalid}; expected one of {list(_VALID_KINDS)}"
        )
    return set(kinds)  # type: ignore[arg-type]  # validated against _VALID_KINDS


def _write_index(path: Path, index: BaseModel) -> None:
    """Write one validated index as compact one-record-per-line JSON.

    The index model was already validated at construction. ``model_dump``
    re-expands optional provenance fields, so records are re-slimmed to keep
    lines compact while the structure stays schema-valid.
    """
    dump = index.model_dump(by_alias=True)
    header = {key: value for key, value in dump.items() if key != "records"}
    records = {
        record_id: _slim_payload(payload)
        for record_id, payload in dump["records"].items()
    }
    write_compact_records_json(path, header, records)


def export_editor_indexes(
    project: Project,
    *,
    kinds: set[Literal["source", "target", "source-target"]] | None = None,
    fail_on_warn: bool = False,
    write_jsonl: bool = False,
) -> EditorIndexesResult:
    """Build and write the requested editor indexes.

    ``source-index.json`` is always safe to write. ``target-index.json`` and
    ``source-target-index.json`` are written only when there are no blocking
    target findings (errors, or warnings when ``fail_on_warn`` is set). When
    blocking findings prevent writing target-based indexes, an
    :class:`EditorIndexError` is raised carrying the partial result.

    When ``write_jsonl`` is True, JSONL versions are written alongside the
    existing JSON files: ``current-source.jsonl``, ``current-target.jsonl``,
    ``current-source-target.jsonl``.
    """
    requested = _resolve_kinds(kinds)  # type: ignore[arg-type]
    source_index, target_index, source_target_index, findings = build_editor_indexes(
        project
    )

    error_findings = [f for f in findings if f.severity == Severity.ERROR]
    warn_findings = [f for f in findings if f.severity == Severity.WARN]
    blocking: list[Finding] = list(error_findings)
    if fail_on_warn:
        blocking.extend(warn_findings)
    has_blocking = bool(blocking)
    need_target_based = bool(requested & {"target", "source-target"})

    result = EditorIndexesResult(
        source_record_count=source_index.record_count,
        target_record_count=target_index.record_count,
        source_target_record_count=source_target_index.record_count,
        translated_count=source_target_index.translated_count,
        missing_count=source_target_index.missing_count,
        warning_count=len(warn_findings),
        error_count=len(error_findings),
    )

    # source-index never depends on translation state.
    if "source" in requested:
        source_path = translation_source_index_path(project)
        _write_index(source_path, source_index)
        result.source_path = str(source_path)
        result.written.append("source")
        if write_jsonl:
            jsonl_path = source_path.with_suffix(".jsonl")
            _write_jsonl_index(jsonl_path, source_index.records)

    if not has_blocking:
        if "target" in requested:
            target_path = translation_target_index_path(project)
            _write_index(target_path, target_index)
            result.target_path = str(target_path)
            result.written.append("target")
            if write_jsonl:
                jsonl_path = target_path.with_suffix(".jsonl")
                _write_jsonl_index(jsonl_path, target_index.records)
        if "source-target" in requested:
            st_path = translation_source_target_index_path(project)
            _write_index(st_path, source_target_index)
            result.source_target_path = str(st_path)
            result.written.append("source-target")
            if write_jsonl:
                jsonl_path = st_path.with_suffix(".jsonl")
                _write_jsonl_index(jsonl_path, source_target_index.records)

    # Preserve canonical order in ``written`` regardless of request order.
    result.written = [kind for kind in _ALL_KINDS if kind in set(result.written)]

    if has_blocking and need_target_based:
        raise EditorIndexError(blocking, result)

    return result


def _write_jsonl_index(path: Path, records: list[Any] | Mapping[str, Any]) -> None:
    """Write model records (a list or mapping's values) as one JSON object per line."""
    record_iter = records.values() if isinstance(records, Mapping) else records
    lines: list[str] = []
    for rec in record_iter:
        if hasattr(rec, "model_dump"):
            lines.append(json.dumps(rec.model_dump(mode="json"), ensure_ascii=False))
        elif hasattr(rec, "as_dict"):
            lines.append(json.dumps(rec.as_dict(), ensure_ascii=False))
        elif isinstance(rec, dict):
            lines.append(json.dumps(rec, ensure_ascii=False))
    write_text_atomic(path, "\n".join(lines) + "\n")
