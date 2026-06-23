"""Translation-record acceptance service.

Centralizes the validate-and-persist flow that was duplicated between the
``translate insert`` (batch) and ``translate set-record`` (single-record)
commands. Both commands used to re-implement the same steps: look up the
source view, validate each record against the current context, bail on the
first ERROR finding, then mutate the translation store with a shared
timestamp and refresh the status snapshot.

This module owns the pure workflow:

1. Resolve each submitted record id against the source index (raising
   :class:`booktx.config.BooktxError` for duplicate / unknown / out-of-task
   ids, which the CLI renders exactly like any other user-facing error).
2. Load the translation context **once** and validate every record against it.
3. If any validation produced an ERROR finding, raise
   :class:`SubmissionValidationError` carrying those findings — the store is
   not touched.
4. Otherwise mutate the store atomically with one shared timestamp and return
   an :class:`AcceptResult` describing the post-accept progress for the first
   affected chapter.

The CLI wrappers parse options, call :func:`accept_translation_records` (or
:func:`accept_one_record`), then render the result. Console output is
intentionally not produced here so the service is unit-testable without
Typer/Rich.
"""

# ruff: noqa: E501

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from booktx.config import (
    Project,
    _err,
    load_translation_store,
    write_translation_store,
)
from booktx.context import load_context
from booktx.io_utils import utc_timestamp
from booktx.models import TranslatedRecord
from booktx.progress import count_words
from booktx.translation_store import ensure_store_record, upsert_translation_version
from booktx.validate import Severity, validate_record_pair
from booktx.versioning import resolve_current_version

if TYPE_CHECKING:
    from booktx.models import TranslationTask
    from booktx.status import StatusBundle
    from booktx.validate import Finding


__all__ = [
    "SubmittedRecord",
    "AcceptResult",
    "SubmissionValidationError",
    "accept_translation_records",
    "accept_one_record",
]


@dataclass(slots=True)
class SubmittedRecord:
    """One validated submission item (record id + target text)."""

    id: str
    target: str


@dataclass(slots=True)
class AcceptResult:
    """Post-accept progress for the first affected chapter.

    ``chapter_id`` is empty when the accepted record(s) could not be mapped to
    a chapter; the CLI treats that as "no chapter line to print".
    """

    accepted_records: int
    target_words: int
    version_ref: str = ""
    chapter_id: str = ""
    chapter_title: str = ""
    records_translated: int = 0
    records_total: int = 0
    records_remaining: int = 0


class SubmissionValidationError(Exception):
    """Raised when one or more submitted records failed ERROR-level validation.

    Carries the ERROR findings so the CLI can render them with the existing
    submission-failure renderer. The translation store is never written when
    this is raised.
    """

    def __init__(self, findings: list[Finding]) -> None:
        self.findings = findings
        super().__init__("submission failed validation")


def _error_findings(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == Severity.ERROR]


def _resolved_submission_version(
    proj: Project,
    *,
    task: TranslationTask | None,
    submission_translation_version: str | None,
) -> str:
    """Resolve the version for this write and reject stale task metadata."""
    resolution = resolve_current_version(proj)
    current_version_ref = resolution.version_ref

    if (
        task is not None
        and task.translation_version is not None
        and task.translation_version != current_version_ref
    ):
        raise _err(
            "stale_translation_task",
            "stale translation task "
            f"{task.task_id} was created for version {task.translation_version}, "
            f"but the current active version is {current_version_ref}.\n"
            "Run `booktx translate next .` to create a fresh task, or select the "
            "old version explicitly before submitting.",
        )

    if submission_translation_version is not None:
        expected_version = (
            task.translation_version
            if task is not None and task.translation_version is not None
            else current_version_ref
        )
        if submission_translation_version != expected_version:
            subject = (
                f"task {task.task_id} version {expected_version}"
                if task is not None and task.translation_version is not None
                else f"current active version {expected_version}"
            )
            raise _err(
                "submission_translation_version_mismatch",
                "submission translation_version "
                f"{submission_translation_version} does not match {subject}",
            )

    return current_version_ref


def _validate_task_profile(
    proj: Project,
    task: TranslationTask | None,
    submission_profile: str | None,
) -> None:
    selected = proj.profile or ""
    if task is not None and task.profile and task.profile != selected:
        raise _err(
            "task_profile_mismatch",
            f"task {task.task_id} belongs to profile {task.profile}, but selected profile is {selected or '<none>'}",
        )
    if submission_profile and submission_profile != selected:
        raise _err(
            "submission_profile_mismatch",
            f"submission profile {submission_profile} does not match selected profile {selected or '<none>'}",
        )


def _validate_submitted(
    proj: Project,
    bundle: StatusBundle,
    submitted: list[SubmittedRecord],
    *,
    task: TranslationTask | None,
) -> list[Finding]:
    """Validate submitted records, raising BooktxError on id problems.

    Context is loaded exactly once. Returns all findings (any severity); the
    caller decides whether ERROR findings should block the store write.
    """
    source_by_id = bundle.index.source_by_id
    source_chunks = bundle.index.source_chunks
    allowed_ids = {record.id for record in task.records} if task is not None else None

    context = load_context(proj)
    findings: list[Finding] = []
    seen_ids: set[str] = set()
    for item in submitted:
        record_id = item.id
        if record_id in seen_ids:
            raise _err(
                "duplicate_record_id",
                f"duplicate record id in submission: {record_id}",
            )
        seen_ids.add(record_id)
        if record_id not in source_by_id:
            raise _err("unknown_record_id", f"unknown source record id: {record_id}")
        if allowed_ids is not None and record_id not in allowed_ids:
            raise _err(
                "record_not_in_task",
                f"record {record_id} is not part of task {task.task_id}",  # type: ignore[union-attr]
            )
        source_view = source_by_id[record_id]
        translated = TranslatedRecord(id=record_id, target=item.target)
        source_chunk = source_chunks[source_view.chunk_id]
        source_record = next(
            record for record in source_chunk.records if record.id == record_id
        )
        findings.extend(
            validate_record_pair(
                source_record, translated, source_chunk.chunk_id, context
            )
        )
    return findings


def _write_accepted(
    proj: Project,
    bundle: StatusBundle,
    submitted: list[SubmittedRecord],
    *,
    version_ref: str,
) -> tuple[str, str]:
    """Persist accepted records atomically and return timestamp plus version_ref."""
    source_by_id = bundle.index.source_by_id
    updated_at = utc_timestamp()
    store = load_translation_store(proj)
    store.source_sha256 = bundle.snapshot.source.source_sha256
    for item in submitted:
        source_view = source_by_id[item.id]
        record = ensure_store_record(
            store,
            item.id,
            source=source_view.source,
            source_sha256=source_view.source_sha256,
        )
        upsert_translation_version(
            record,
            version_ref,
            item.target,
            updated_at=updated_at,
        )
    write_translation_store(proj, store)
    return updated_at, version_ref


def accept_translation_records(
    proj: Project,
    submitted: list[SubmittedRecord],
    *,
    bundle: StatusBundle,
    task: TranslationTask | None = None,
    submission_translation_version: str | None = None,
    submission_profile: str | None = None,
    enforce_task_version: bool = False,
) -> AcceptResult:
    """Validate and atomically persist a batch of accepted records.

    Raises :class:`BooktxError` for duplicate / unknown / out-of-task ids and
    :class:`SubmissionValidationError` when any record fails ERROR-level
    validation. On success the store is written once and an
    :class:`AcceptResult` is returned.

    Note: callers pass a *fresh* ``bundle`` built before acceptance. The
    returned progress fields reflect a *refreshed* snapshot taken after the
    store write, matching the historical CLI output exactly.
    """
    if not submitted:
        raise _err("empty_submission", "no records to accept")

    _validate_task_profile(proj, task, submission_profile)

    if enforce_task_version:
        version_ref = _resolved_submission_version(
            proj,
            task=task,
            submission_translation_version=submission_translation_version,
        )
    else:
        version_ref = resolve_current_version(proj).version_ref
    findings = _validate_submitted(proj, bundle, submitted, task=task)
    errors = _error_findings(findings)
    if errors:
        raise SubmissionValidationError(errors)

    _updated_at, version_ref = _write_accepted(
        proj, bundle, submitted, version_ref=version_ref
    )

    # Refresh to report post-accept progress for the first submitted record's
    # chapter. This mirrors the original two-pass behavior.
    from booktx.status import build_status_snapshot

    ctx = bundle.snapshot.context
    refreshed = build_status_snapshot(
        proj, context_exists=ctx.exists, context_ready=ctx.ready
    )
    first_record_id = submitted[0].id
    chapter_id = refreshed.index.record_to_chapter.get(first_record_id, "")
    chapter = refreshed.index.chapters_by_id.get(chapter_id)
    target_words = sum(count_words(item.target) for item in submitted)
    if chapter is None:
        return AcceptResult(
            accepted_records=len(submitted),
            target_words=target_words,
            version_ref=version_ref,
        )
    return AcceptResult(
        accepted_records=len(submitted),
        target_words=target_words,
        version_ref=version_ref,
        chapter_id=chapter.chapter_id,
        chapter_title=chapter.title,
        records_translated=chapter.records_translated,
        records_total=chapter.records_total,
        records_remaining=chapter.records_remaining,
    )


def accept_one_record(
    proj: Project,
    record_id: str,
    target: str,
    *,
    bundle: StatusBundle,
    task: TranslationTask | None = None,
    submission_translation_version: str | None = None,
    submission_profile: str | None = None,
    enforce_task_version: bool = False,
) -> AcceptResult:
    """Validate and persist a single accepted record.

    Equivalent to :func:`accept_translation_records` with one item, but also
    enforces that a non-empty target was supplied (the single-record command
    rejects empty targets before reaching the store).
    """
    if not target.strip():
        raise _err("empty_target", f"empty target for record {record_id}")
    return accept_translation_records(
        proj,
        [SubmittedRecord(id=record_id, target=target)],
        bundle=bundle,
        task=task,
        submission_translation_version=submission_translation_version,
        submission_profile=submission_profile,
        enforce_task_version=enforce_task_version,
    )
