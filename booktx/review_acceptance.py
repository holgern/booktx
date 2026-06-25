"""Atomic review-submission acceptance service.

Mirrors :mod:`booktx.acceptance` for the review workflow: validate every
submitted review target against the source and the review task's recorded base,
reject the whole submission on any ERROR finding or base drift, then persist
review candidates and activate conservatively. The store is written exactly
once.
"""

# ruff: noqa: E501

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from booktx.acceptance import SubmissionValidationError
from booktx.config import (
    Project,
    _err,
    load_translation_store,
    write_translation_store,
)
from booktx.io_utils import utc_timestamp
from booktx.models import (
    QualityReviewConfig,
    StoredTranslationRecordV2,
    TranslationReviewCandidate,
    TranslationReviewTask,
)
from booktx.translation_store import (
    find_review_candidate,
    resolve_review_base,
    sha256_text,
)
from booktx.validate import (
    Severity,
    load_validation_context,
    validate_record_pair,
)

if TYPE_CHECKING:
    from booktx.status import StatusBundle
    from booktx.validate import Finding

__all__ = [
    "SubmittedReview",
    "ReviewAcceptResult",
    "accept_review_submission",
]


@dataclass(slots=True)
class SubmittedReview:
    """One submitted review target (record id + target text)."""

    id: str
    target: str


@dataclass(slots=True)
class ReviewAcceptResult:
    """Outcome of accepting a review submission."""

    accepted_records: int
    activated: bool
    review_refs: list[str]


def _error_findings(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == Severity.ERROR]


def _validate_task_profile(project: Project, task: TranslationReviewTask) -> None:
    selected = project.profile or ""
    if task.profile and task.profile != selected:
        raise _err(
            "review_task_profile_mismatch",
            f"review task {task.review_task_id} belongs to profile {task.profile}, "
            f"but selected profile is {selected or '<none>'}",
        )


def _validate_task_evidence(project: Project, task: TranslationReviewTask) -> None:
    """Reject source/profile-config/source-config drift since the task was created."""
    from booktx.versioning import canonical_json_sha256

    if task.source_sha256:
        from booktx.config import current_source_sha256

        if task.source_sha256 != current_source_sha256(project):
            raise _err(
                "review_source_drift",
                f"project source changed since review task {task.review_task_id} was created",
            )
    if task.profile_config_sha256 is not None and project.profile_config is not None:
        actual = canonical_json_sha256(project.profile_config.model_dump(mode="json"))
        if actual != task.profile_config_sha256:
            raise _err(
                "review_profile_config_drift",
                f"profile config changed since review task {task.review_task_id} was created",
            )
    if task.source_config_sha256 is not None:
        actual = canonical_json_sha256(project.source_config.model_dump(mode="json"))
        if actual != task.source_config_sha256:
            raise _err(
                "review_source_config_drift",
                f"source config changed since review task {task.review_task_id} was created",
            )


def _terminal_pass(quality_cfg: QualityReviewConfig | None) -> int | None:
    if quality_cfg is None or not quality_cfg.active_passes:
        return None
    return max(quality_cfg.active_passes)


def _should_activate(
    stored: StoredTranslationRecordV2,
    review: TranslationReviewCandidate,
    *,
    quality_cfg: QualityReviewConfig | None,
    activate: bool,
    no_activate: bool,
) -> bool:
    """Conservative activation policy."""
    if no_activate:
        return False
    if activate:
        return True
    # No active review yet.
    if stored.active_review is None:
        return True
    # Replacing the same pass.
    existing = find_review_candidate(stored, stored.active_review)
    if existing is not None and existing.pass_number == review.pass_number:
        return True
    # Inserted review is the terminal required pass.
    terminal = _terminal_pass(quality_cfg)
    if terminal is not None and review.pass_number == terminal:
        return True
    return False


def accept_review_submission(
    project: Project,
    task: TranslationReviewTask,
    submitted: list[SubmittedReview],
    *,
    bundle: StatusBundle,
    quality_cfg: QualityReviewConfig | None = None,
    activate: bool = False,
    no_activate: bool = False,
) -> ReviewAcceptResult:
    """Validate and atomically persist a batch of review candidates.

    Raises :class:`booktx.config.BooktxError` for task/profile/evidence
    problems and :class:`SubmissionValidationError` when any target fails
    ERROR-level validation or the recorded base has drifted. On success the
    store is written once.
    """
    if not submitted:
        raise _err("empty_submission", "no review records to accept")

    _validate_task_profile(project, task)
    _validate_task_evidence(project, task)

    task_records = {r.id: r for r in task.records}
    source_by_id = bundle.index.source_by_id
    source_chunks = bundle.index.source_chunks

    context = load_validation_context(project)

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
        task_rec = task_records.get(record_id)
        if task_rec is None:
            raise _err(
                "record_not_in_task",
                f"record {record_id} is not part of review task {task.review_task_id}",
            )
        if record_id not in source_by_id:
            raise _err("unknown_record_id", f"unknown source record id: {record_id}")
        view = source_by_id[record_id]
        source_chunk = source_chunks[view.chunk_id]
        source_record = next(r for r in source_chunk.records if r.id == record_id)
        from booktx.models import TranslatedRecord

        translated = TranslatedRecord(id=record_id, target=item.target)
        findings.extend(
            validate_record_pair(
                source_record, translated, source_chunk.chunk_id, context
            )
        )

    errors = _error_findings(findings)
    if errors:
        raise SubmissionValidationError(errors)

    # Base drift and idempotency checks against the live store.
    store = load_translation_store(project)
    timestamp = utc_timestamp()
    activated = False
    created_refs: list[str] = []

    for item in submitted:
        record_id = item.id
        task_rec = task_records[record_id]
        stored = store.records.get(record_id)
        if stored is None:
            raise _err(
                "missing_store_record",
                f"record {record_id} has no stored translation to review",
            )
        base = resolve_review_base(stored, task_rec.base_kind, task_rec.base_ref)
        if base is None:
            raise _err(
                "review_base_missing",
                f"record {record_id} base {task_rec.base_ref!r} is missing",
            )
        if sha256_text(base.target) != task_rec.base_target_sha256:
            raise _err(
                "review_base_drift",
                f"record {record_id} base {task_rec.base_ref!r} changed since "
                f"review task {task.review_task_id} was created",
            )
        target_sha = sha256_text(item.target)
        existing = find_review_candidate(stored, task_rec.review_ref)
        if existing is not None:
            # Idempotent only with same provenance and identical target.
            if (
                existing.review_task_id == task.review_task_id
                and existing.target_sha256 == target_sha
            ):
                created_refs.append(existing.review_ref)
                continue
            raise _err(
                "review_ref_conflict",
                f"review_ref {task_rec.review_ref!r} already exists for record "
                f"{record_id} with different provenance or target; use a new run ref",
            )
        candidate = TranslationReviewCandidate(
            pass_number=task_rec.pass_number,
            run_number=int(task_rec.review_ref.split(".")[1]),
            review_ref=task_rec.review_ref,
            base_kind=task_rec.base_kind,
            base_ref=task_rec.base_ref,
            base_target_sha256=task_rec.base_target_sha256,
            target=item.target,
            target_sha256=target_sha,
            status="accepted",
            created_at=timestamp,
            updated_at=timestamp,
            review_task_id=task.review_task_id,
        )
        stored.reviews.append(candidate)
        created_refs.append(candidate.review_ref)
        if _should_activate(
            stored,
            candidate,
            quality_cfg=quality_cfg,
            activate=activate,
            no_activate=no_activate,
        ):
            stored.active_review = candidate.review_ref
            activated = True

    write_translation_store(project, store)
    return ReviewAcceptResult(
        accepted_records=len(submitted),
        activated=activated,
        review_refs=created_refs,
    )
