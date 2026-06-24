"""Pass-through profile generation.

Creates identity translated chunks from extracted source chunks so the
reconstruction pipeline can be validated without translation.
"""

# ruff: noqa: E501

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from booktx.build import BuildResult, build_project
from booktx.config import (
    BooktxError,
    Project,
    _err,
    create_profile,
    current_source_sha256,
    extracted_source_sha256,
    load_profile_project,
    load_source_project,
    load_translation_store,
    write_translation_store,
)
from booktx.io_utils import write_json_model_atomic
from booktx.models import (
    Chunk,
    TranslatedChunk,
    TranslatedRecord,
    TranslationStoreV2,
)
from booktx.validate import ValidationReport, validate_project

PASS_THROUGH_ACTOR = "booktx:pass-through"
PASS_THROUGH_HARNESS = "booktx"
PASS_THROUGH_MODEL = "booktx/pass-through"


@dataclass(slots=True)
class PassThroughResult:
    profile: str
    chunks_written: int
    records_written: int
    stale_removed: int
    translated_dir: Path
    validation_report: ValidationReport
    build_result: BuildResult | None = None


def ensure_pass_through_profile(
    root: Path | str,
    profile: str,
    *,
    create: bool = False,
    select: bool = False,
    output_filename: str | None = None,
) -> Project:
    try:
        project = load_profile_project(root, profile)
    except BooktxError as exc:
        if exc.code != "profile_not_found" or not create:
            raise
        source_project = load_source_project(root)
        target_language = source_project.source_config.source_language
        return create_profile(
            root,
            profile,
            target_language=target_language,
            target_locale=target_language,
            actor=PASS_THROUGH_ACTOR,
            harness=PASS_THROUGH_HARNESS,
            model=PASS_THROUGH_MODEL,
            output_filename=output_filename,
            select=select,
            kind="pass-through",
        )

    if project.profile_config is None or project.profile_config.kind != "pass-through":
        raise _err(
            "not_pass_through_profile",
            f"profile {profile} is not a pass-through profile; "
            "create one with `booktx pass-through PROJECT --profile PROFILE --create`",
        )
    return project


def _identity_translated_chunk(chunk: Chunk) -> TranslatedChunk:
    return TranslatedChunk(
        chunk_id=chunk.chunk_id,
        records=[
            TranslatedRecord(id=record.id, target=record.source)
            for record in chunk.records
        ],
    )


def write_pass_through_chunks(
    project: Project,
    *,
    force: bool = True,
    prune_stale: bool = True,
) -> tuple[int, int, int]:
    if project.translated_dir is None:
        raise _err("profile_required", "pass-through requires a translation profile")
    chunk_paths = project.chunks()
    if not chunk_paths:
        raise _err(
            "missing_chunks",
            "no extracted chunks found; run `booktx extract PROJECT_DIR` first",
        )

    extracted = extracted_source_sha256(project)
    current = current_source_sha256(project)
    if extracted and current and extracted != current:
        raise _err(
            "source_drift",
            "source file has changed since extraction; run `booktx extract` first",
        )

    project.translated_dir.mkdir(parents=True, exist_ok=True)

    source_chunk_ids: set[str] = set()
    chunks_written = 0
    records_written = 0

    for chunk_path in sorted(chunk_paths, key=lambda path: path.stem):
        chunk = Chunk.model_validate_json(chunk_path.read_text("utf-8"))
        source_chunk_ids.add(chunk.chunk_id)
        target_path = project.translated_dir / f"{chunk.chunk_id}.json"

        if target_path.exists() and not force:
            raise _err(
                "pass_through_target_exists",
                f"translated chunk already exists: {target_path}",
            )

        translated = _identity_translated_chunk(chunk)
        write_json_model_atomic(target_path, translated)
        chunks_written += 1
        records_written += len(translated.records)

    stale_removed = 0
    if prune_stale:
        for path in sorted(project.translated_dir.glob("*.json")):
            if path.stem not in source_chunk_ids:
                path.unlink()
                stale_removed += 1

    return chunks_written, records_written, stale_removed


def ensure_no_store_override(project: Project, *, clear_store: bool = False) -> None:
    store = load_translation_store(project)
    if store.records and not clear_store:
        raise _err(
            "pass_through_store_not_empty",
            "pass-through output would be overridden by translation-store.json; "
            "use a fresh pass-through profile or pass --clear-store",
        )
    if store.records and clear_store:
        write_translation_store(
            project,
            TranslationStoreV2(source_sha256=current_source_sha256(project)),
        )


def run_pass_through(
    project: Project,
    *,
    force: bool = True,
    prune_stale: bool = True,
    clear_store: bool = False,
    build: bool = True,
    allow_warnings: bool = False,
) -> PassThroughResult:
    ensure_no_store_override(project, clear_store=clear_store)

    chunks_written, records_written, stale_removed = write_pass_through_chunks(
        project,
        force=force,
        prune_stale=prune_stale,
    )

    report = validate_project(project)
    if report.errors:
        first = report.errors[0]
        raise _err(
            "pass_through_validation_failed",
            f"pass-through validation failed: {first.chunk_id} {first.rule}: {first.message}",
        )
    if report.warnings and not allow_warnings:
        first = report.warnings[0]
        raise _err(
            "pass_through_validation_warning",
            f"pass-through validation warning: {first.chunk_id} {first.rule}: {first.message}",
        )
    if report.chunks_missing_translation:
        raise _err(
            "pass_through_incomplete",
            f"pass-through did not cover {report.chunks_missing_translation} source chunk(s)",
        )

    build_result = build_project(project, require_complete=True) if build else None
    translated_dir = project.translated_dir
    if translated_dir is None:
        raise _err("profile_required", "pass-through requires a translation profile")

    return PassThroughResult(
        profile=project.profile or "",
        chunks_written=chunks_written,
        records_written=records_written,
        stale_removed=stale_removed,
        translated_dir=translated_dir,
        validation_report=report,
        build_result=build_result,
    )
