"""Review-task selection and durable artifact rendering.

Mirrors ``booktx.tasks`` for the review workflow: select records eligible for a
review pass, build a durable :class:`TranslationReviewTask`, and render the
source block plus the prefilled ingest block. The ingest block is prefilled
with the base target so the reviewing agent edits only when quality improves.
"""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from booktx.config import (
    Project,
    load_translation_store,
    translation_review_ingest_block_path,
    translation_review_source_block_path,
)
from booktx.io_utils import write_text_atomic
from booktx.models import (
    QualityReviewConfig,
    ReviewContextRecord,
    ReviewPassConfig,
    StoredTranslationRecordV2,
    TranslationReviewCandidate,
    TranslationReviewTask,
    TranslationReviewTaskRecord,
)
from booktx.translation_store import (
    active_candidate,
    effective_target_candidate,
    review_chain_is_stale,
    sha256_text,
)
from booktx.versioning import canonical_json_sha256

if TYPE_CHECKING:
    from booktx.status import ChapterProgress, StatusBundle

__all__ = [
    "ReviewTaskPaths",
    "ReviewSelectedRecord",
    "make_review_task_id",
    "select_review_records",
    "create_review_task",
    "write_review_source_block",
    "write_review_ingest_block",
]


@dataclass(frozen=True, slots=True)
class ReviewTaskPaths:
    """The durable files owned by one review task."""

    task_json: object  # path resolved lazily via config helpers
    source_block: object
    ingest_block: object


def make_review_task_id(
    chapter_id: str, first_record_id: str, pass_number: int, record_ids: list[str]
) -> str:
    """Derive a deterministic, path-safe review task id."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    record_part = first_record_id.replace("-", "")
    digest = hashlib.blake2s(
        "|".join(record_ids).encode("utf-8"), digest_size=4
    ).hexdigest()
    return f"btr-{stamp}-{chapter_id}-r{pass_number}-{record_part}-{digest}"


@dataclass(slots=True)
class ReviewSelectedRecord:
    """One record selected for a review pass."""

    record_id: str
    chunk_id: str
    source: str
    base_kind: str
    base_ref: str
    base_target: str
    base_target_sha256: str
    review_ref: str
    pass_number: int


def _next_run_number(stored: StoredTranslationRecordV2, pass_number: int) -> int:
    """Return the next run number for a pass on a record (>=1)."""
    runs = [r.run_number for r in stored.reviews if r.pass_number == pass_number]
    return (max(runs) + 1) if runs else 1


def _accepted_review_for_pass(
    stored: StoredTranslationRecordV2, pass_number: int
) -> TranslationReviewCandidate | None:
    for review in stored.reviews:
        if review.pass_number != pass_number:
            continue
        if review.status != "accepted":
            continue
        if review_chain_is_stale(stored, review.review_ref):
            continue
        return review
    return None


def _resolve_base(
    stored: StoredTranslationRecordV2,
    pcfg: ReviewPassConfig | None,
) -> tuple[str, str, str] | None:
    """Resolve (base_kind, base_ref, base_target) for a review pass base.

    Returns None when the required base is unavailable (blocked).
    """
    if pcfg is not None and pcfg.base == "active_review":
        required = pcfg.required_base_pass
        if required is None:
            return None
        base_review = _accepted_review_for_pass(stored, required)
        if base_review is None:
            return None
        return ("review", base_review.review_ref, base_review.target)
    active = active_candidate(stored)
    if active is None or active.status != "accepted":
        return None
    return ("translation", active.version_ref, active.target)


def select_review_records(
    bundle: StatusBundle,
    store_records: dict[str, StoredTranslationRecordV2],
    quality_cfg: QualityReviewConfig,
    *,
    pass_number: int,
    chapter_id: str | None = None,
    max_words: int | None = None,
) -> list[ReviewSelectedRecord]:
    """Select records eligible for a review pass.

    Skips records that already have a current non-stale accepted review for the
    pass, and skips records whose required base is unavailable (blocked). Only
    records with an accepted active translation version are considered for the
    default active_translation base.
    """
    pcfg = next((p for p in quality_cfg.passes if p.pass_number == pass_number), None)
    source_by_id = bundle.index.source_by_id
    chapter_ids = (
        [chapter_id]
        if chapter_id is not None
        else list(bundle.index.record_ids_by_chapter)
    )
    selected: list[ReviewSelectedRecord] = []
    total_words = 0
    for cid in chapter_ids:
        for record_id in bundle.index.record_ids_by_chapter.get(cid, []):
            stored = store_records.get(record_id)
            if stored is None:
                continue
            # Source drift guard: the stored source must match the current source.
            view = source_by_id.get(record_id)
            if view is None or stored.source != view.source:
                continue
            base = _resolve_base(stored, pcfg)
            if base is None:
                continue  # blocked: required base missing
            # Skip records with a current non-stale accepted review for this pass.
            if _accepted_review_for_pass(stored, pass_number) is not None:
                continue
            base_kind, base_ref, base_target = base
            run_number = _next_run_number(stored, pass_number)
            selected.append(
                ReviewSelectedRecord(
                    record_id=record_id,
                    chunk_id=view.chunk_id,
                    source=view.source,
                    base_kind=base_kind,
                    base_ref=base_ref,
                    base_target=base_target,
                    base_target_sha256=sha256_text(base_target),
                    review_ref=f"R{pass_number}.{run_number}",
                    pass_number=pass_number,
                )
            )
            total_words += view.source_words
            if max_words is not None and total_words >= max_words:
                return selected
    return selected


def _context_window(
    record_id: str,
    bundle: StatusBundle,
    store_records: dict[str, StoredTranslationRecordV2],
    before_records: int,
    after_records: int,
) -> tuple[list[ReviewContextRecord], list[ReviewContextRecord]]:
    """Build before/after neighbor context with effective targets."""
    ordered = bundle.index.record_ids_by_chapter
    # Flatten all record ids in chapter order.
    flat: list[str] = []
    for cid in ordered:
        flat.extend(ordered[cid])
    try:
        idx = flat.index(record_id)
    except ValueError:
        return ([], [])
    before_ids = flat[max(0, idx - before_records) : idx]
    after_ids = flat[idx + 1 : idx + 1 + after_records]

    def _ctx(rid: str, role: str) -> ReviewContextRecord | None:
        view = bundle.index.source_by_id.get(rid)
        if view is None:
            return None
        stored = store_records.get(rid)
        effective_ref = None
        effective_target = None
        if stored is not None:
            eff = effective_target_candidate(stored)
            if eff is not None:
                effective_target = eff.target
                effective_ref = getattr(eff, "review_ref", None) or getattr(
                    eff, "version_ref", None
                )
        return ReviewContextRecord(
            id=rid,
            chunk_id=view.chunk_id,
            source=view.source,
            effective_target=effective_target,
            effective_ref=effective_ref,
            role=role,  # type: ignore[arg-type]
        )

    before = [c for c in (_ctx(rid, "before") for rid in before_ids) if c is not None]
    after = [c for c in (_ctx(rid, "after") for rid in after_ids) if c is not None]
    return (before, after)


def create_review_task(
    project: Project,
    bundle: StatusBundle,
    quality_cfg: QualityReviewConfig,
    selected: list[ReviewSelectedRecord],
    *,
    pass_number: int,
    chapter: ChapterProgress,
) -> TranslationReviewTask:
    """Build and persist a review task plus its source/ingest block artifacts."""
    from booktx.config import write_translation_review_task

    store = load_translation_store(project)
    store_records = store.records
    pcfg = next((p for p in quality_cfg.passes if p.pass_number == pass_number), None)
    before_n = pcfg.before_records if pcfg is not None else 2
    after_n = pcfg.after_records if pcfg is not None else 2
    source_by_id = bundle.index.source_by_id

    records: list[TranslationReviewTaskRecord] = []
    for sel in selected:
        before, after = _context_window(
            sel.record_id, bundle, store_records, before_n, after_n
        )
        window_payload = "\n".join(
            r.source
            for r in (
                before
                + [
                    ReviewContextRecord(
                        id=sel.record_id,
                        chunk_id=sel.chunk_id,
                        source=sel.source,
                        role="selected",
                    )
                ]
                + after
            )
        )
        records.append(
            TranslationReviewTaskRecord(
                id=sel.record_id,
                chunk_id=sel.chunk_id,
                source=sel.source,
                base_kind=sel.base_kind,  # type: ignore[arg-type]
                base_ref=sel.base_ref,
                base_target=sel.base_target,
                base_target_sha256=sel.base_target_sha256,
                review_ref=sel.review_ref,
                pass_number=pass_number,
                review_window_sha256=sha256_text(window_payload),
                before=before,
                after=after,
            )
        )

    first_id = selected[0].record_id if selected else "0000-000000"
    review_task_id = make_review_task_id(
        chapter.chapter_id, first_id, pass_number, [s.record_id for s in selected]
    )
    task = TranslationReviewTask(
        review_task_id=review_task_id,
        profile=project.profile or "",
        chapter_id=chapter.chapter_id,
        chapter_title=chapter.title,
        pass_number=pass_number,
        pass_name=pcfg.name if pcfg is not None else "",
        pass_instructions=pcfg.instructions if pcfg is not None else "",
        source_language=project.config.source_language,
        target_language=project.config.target_language,
        target_locale=project.config.target_locale or project.config.target_language,
        context_view_sha256=None,
        context_view_path=None,
        source_sha256=bundle.snapshot.source.source_sha256 or None,
        profile_config_sha256=(
            canonical_json_sha256(project.profile_config.model_dump(mode="json"))
            if project.profile_config is not None
            else None
        ),
        source_config_sha256=canonical_json_sha256(
            project.source_config.model_dump(mode="json")
        ),
        review_policy_sha256=None,
        before_records=before_n,
        after_records=after_n,
        source_words=sum(source_by_id[s.record_id].source_words for s in selected),
        record_count=len(selected),
        created_at=datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        records=records,
    )
    write_translation_review_task(project, task)
    write_review_source_block(project, task)
    write_review_ingest_block(project, task)
    return task


def write_review_source_block(project: Project, task: TranslationReviewTask) -> object:
    """Write the review source block with neighbors and the selected base target."""
    path = translation_review_source_block_path(project, task.review_task_id)
    parts = [
        "# booktx review source",
        f"# profile: {task.profile or 'none'}",
        f"# review_task: {task.review_task_id}",
        f"# pass: R{task.pass_number} {task.pass_name}".rstrip(),
        f"# chapter: {task.chapter_id} {task.chapter_title}".rstrip(),
        f"# instruction: {task.pass_instructions}".rstrip(),
        "",
    ]
    for rec in task.records:
        for ctx in rec.before:
            parts.append(f"=== {ctx.id} BEFORE")
            parts.append(f"SOURCE: {ctx.source}")
            parts.append(f"TARGET: {ctx.effective_target or ''}")
            parts.append(f"REF: {ctx.effective_ref or ''}")
            parts.append("")
        parts.append(f">>> {rec.id} REVIEW {rec.review_ref} FROM {rec.base_ref}")
        parts.append(f"SOURCE: {rec.source}")
        parts.append(f"CURRENT: {rec.base_target}")
        parts.append("")
        for ctx in rec.after:
            parts.append(f"=== {ctx.id} AFTER")
            parts.append(f"SOURCE: {ctx.source}")
            parts.append(f"TARGET: {ctx.effective_target or ''}")
            parts.append(f"REF: {ctx.effective_ref or ''}")
            parts.append("")
    write_text_atomic(path, "\n".join(parts).rstrip() + "\n")
    return path


def write_review_ingest_block(project: Project, task: TranslationReviewTask) -> object:
    """Write the prefilled ingest block (base target under each record header)."""
    path = translation_review_ingest_block_path(project, task.review_task_id)
    headers = [
        "# booktx review block submission",
        f"# profile: {task.profile or 'none'}",
        f"# review_task: {task.review_task_id}",
        f"# pass: {task.pass_number}",
        f"# submit: booktx review insert . --review-task-id {task.review_task_id} "
        f"--file {path.name} --format block",
        "",
    ]
    parts: list[str] = list(headers)
    for rec in task.records:
        parts.append(f">>> {rec.id}")
        parts.append(rec.base_target)
        parts.append("")
    write_text_atomic(path, "\n".join(parts).rstrip() + "\n")
    return path
