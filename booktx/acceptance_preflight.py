"""Shared staged EPUB inline-XHTML preflight for submission acceptance.

Layers submitted records on top of the current effective translations and runs
the EPUB inline-XHTML preflight, returning the blocking findings (errors, and
warnings when ``fail_on_warnings``). Non-EPUB projects and projects whose
effective chunks cannot be loaded return an empty list; the caller decides how
to handle that.

Keeping this logic out of ``booktx.cli`` lets ``translate insert``,
``translation revise-record``, and ``review insert`` share one acceptance path.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from booktx.acceptance import SubmittedRecord
from booktx.epub_preflight import EpubPreflightFinding

if TYPE_CHECKING:
    from booktx.config import Project

__all__ = ["run_staged_preflight"]


def run_staged_preflight(
    project: Project,
    submitted_records: Sequence[SubmittedRecord],
    submitted_ids: set[str],
    *,
    fail_on_warnings: bool = False,
) -> list[EpubPreflightFinding]:
    """Stage submitted records over effective translations and run preflight.

    Returns the blocking findings: every error, plus every warning when
    ``fail_on_warnings`` is set. Returns an empty list for non-EPUB projects or
    when the current effective chunks cannot be loaded (the caller's own
    acceptance step will still validate the records).
    """
    from booktx.models import TranslatedChunk, TranslatedRecord
    from booktx.validate import load_effective_translated_chunks

    # Only run for EPUB projects.
    if project.config.format != "epub":
        return []

    try:
        effective = load_effective_translated_chunks(project)
    except Exception:  # noqa: BLE001
        return []  # can't check; let the caller's acceptance handle it

    from booktx.epub_preflight import validate_epub_inline_preflight
    from booktx.progress import load_source_chunks

    # Build a staged effective chunks view with submitted records overlaid.
    # Even when effective chunks are empty, staged chunks are built from source
    # chunks so new submissions can be validated.
    source_chunks = {c.chunk_id: c for c in load_source_chunks(project)}
    submitted_by_id = {record.id: record for record in submitted_records}
    staged_chunks: dict[str, TranslatedChunk] = {}
    for chunk_id, source_chunk in source_chunks.items():
        existing = effective.chunks.get(chunk_id)
        staged_records = []
        if existing is not None:
            for rec in existing.records:
                if rec.id in submitted_ids:
                    submitted = submitted_by_id.get(rec.id)
                    if submitted is not None:
                        staged_records.append(
                            TranslatedRecord(id=submitted.id, target=submitted.target)
                        )
                    else:
                        staged_records.append(rec)
                else:
                    staged_records.append(rec)
        # Add submitted records that are in this source chunk but not yet in
        # effective translations (new submissions).
        existing_ids = {r.id for r in staged_records}
        source_ids = {r.id for r in source_chunk.records}
        for submitted_rec in submitted_records:
            if submitted_rec.id in source_ids and submitted_rec.id not in existing_ids:
                staged_records.append(
                    TranslatedRecord(id=submitted_rec.id, target=submitted_rec.target)
                )
        if staged_records:
            staged_chunks[chunk_id] = TranslatedChunk(
                records=staged_records, chunk_id=chunk_id
            )

    preflight_findings = validate_epub_inline_preflight(
        project, record_ids=submitted_ids, effective_chunks=staged_chunks
    )
    blocking = [f for f in preflight_findings if f.severity == "error"]
    if fail_on_warnings:
        blocking.extend(f for f in preflight_findings if f.severity == "warn")
    return blocking
