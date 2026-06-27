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
    active_review_candidate,
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
    "REVIEW_SELECTIONS",
    "default_base_mode",
    "parse_review_base",
    "resolve_base",
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


REVIEW_SELECTIONS = ("missing", "stale", "reviewed", "all", "changed-base")


def default_base_mode(pcfg: ReviewPassConfig | None) -> str:
    """Derive the default base mode from the pass config.

    ``active_translation`` by default. When the pass config sets
    ``base=\"active_review\"`` with a ``required_base_pass``, the default base
    becomes ``pass:<required_base_pass>`` so the review is built on that pass's
    latest accepted review.
    """
    if (
        pcfg is not None
        and pcfg.base == "active_review"
        and pcfg.required_base_pass is not None
    ):
        return f"pass:{pcfg.required_base_pass}"
    return "active_translation"


def parse_review_base(base: str | None, pcfg: ReviewPassConfig | None) -> str:
    """Validate a ``--base`` value into a base mode.

    When ``base`` is None, falls back to :func:`default_base_mode`.
    """
    if base is None:
        return default_base_mode(pcfg)
    if base in ("active_translation", "active_review"):
        return base
    if base.startswith("pass:"):
        try:
            pass_n = int(base[len("pass:") :])
        except ValueError as exc:
            raise ValueError(
                f"invalid --base {base!r}: pass number must be an integer"
            ) from exc
        if pass_n < 1:
            raise ValueError(f"invalid --base {base!r}: pass number must be positive")
        return base
    raise ValueError(
        f"invalid --base {base!r}: expected active_translation, active_review, "
        "or pass:N"
    )


def _latest_accepted_review_for_pass(
    stored: StoredTranslationRecordV2, pass_number: int
) -> TranslationReviewCandidate | None:
    """Return the highest-run accepted, non-stale review for a pass, or None."""
    candidates = [
        r
        for r in stored.reviews
        if r.pass_number == pass_number
        and r.status == "accepted"
        and not review_chain_is_stale(stored, r.review_ref)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.run_number)


def resolve_base(
    stored: StoredTranslationRecordV2, base_mode: str
) -> tuple[str, str, str] | None:
    """Resolve ``(base_kind, base_ref, base_target)`` for a base mode.

    Returns None when the required base is unavailable (blocked).
    """
    if base_mode == "active_translation":
        active = active_candidate(stored)
        if active is None or active.status != "accepted":
            return None
        return ("translation", active.version_ref, active.target)
    if base_mode == "active_review":
        review = active_review_candidate(stored)
        if review is None:
            return None
        return ("review", review.review_ref, review.target)
    if base_mode.startswith("pass:"):
        try:
            pass_n = int(base_mode[len("pass:") :])
        except ValueError:
            return None
        review = _latest_accepted_review_for_pass(stored, pass_n)
        if review is None:
            return None
        return ("review", review.review_ref, review.target)
    return None


def _record_matches_selection(
    stored: StoredTranslationRecordV2,
    pass_number: int,
    base_target_sha: str,
    selection: str,
) -> bool:
    """True when a record matches the requested selection mode."""
    if selection == "all":
        return True
    accepted = _accepted_review_for_pass(stored, pass_number)
    if selection == "missing":
        # Default: no accepted review for this pass (includes stale/rejected-only).
        return accepted is None
    if selection == "reviewed":
        return accepted is not None
    if selection == "stale":
        return accepted is None and any(
            r.pass_number == pass_number for r in stored.reviews
        )
    if selection == "changed-base":
        latest = _latest_accepted_review_for_pass(stored, pass_number)
        if latest is None:
            return False
        return latest.base_target_sha256 != base_target_sha
    return False  # unreachable: selection validated by the caller


def select_review_records(
    bundle: StatusBundle,
    store_records: dict[str, StoredTranslationRecordV2],
    quality_cfg: QualityReviewConfig,
    *,
    pass_number: int,
    chapter_id: str | None = None,
    max_words: int | None = None,
    selection: str = "missing",
    base: str | None = None,
) -> list[ReviewSelectedRecord]:
    """Select records eligible for a review pass.

    ``selection`` chooses which records qualify (default ``missing``: records
    missing an accepted review for the pass). ``base`` chooses the candidate each
    new review is derived from; when None it is derived from the pass config
    (``active_translation`` by default, ``pass:<required_base_pass>`` for passes
    configured with ``base=\"active_review\"``). Records whose required base is
    unavailable are skipped (blocked).

    Backward-compatible default behavior is unchanged: the default ``missing``
    selection skips records that already have a current accepted review for the
    pass, and pass-2 selection still blocks when the required pass-1 review is
    missing.
    """
    pcfg = next((p for p in quality_cfg.passes if p.pass_number == pass_number), None)
    base_mode = parse_review_base(base, pcfg)
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
            resolved = resolve_base(stored, base_mode)
            if resolved is None:
                continue  # blocked: required base missing
            base_kind, base_ref, base_target = resolved
            if not _record_matches_selection(
                stored, pass_number, sha256_text(base_target), selection
            ):
                continue
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
