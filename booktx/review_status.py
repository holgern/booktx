"""Review coverage and staleness snapshot per pass.

Computes per-pass review status for the ``booktx review status`` command and
for release-gating checks. Kept separate from ``status --json`` so the normal
status consumers are not affected by the optional quality-review feature.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from booktx.models import (
    QualityReviewConfig,
    ReviewPassConfig,
    StoredTranslationRecordV2,
    TranslationStoreV2,
)
from booktx.translation_store import active_candidate, review_chain_is_stale

__all__ = [
    "ReviewPassStatus",
    "ReviewStatusSnapshot",
    "ReviewGapIndex",
    "build_review_gap_index",
    "compute_review_snapshot",
]


def _accepted_review_for_pass(
    stored: StoredTranslationRecordV2, pass_number: int
) -> bool:
    """True when the record has an accepted, chain-valid review for a pass."""
    for review in stored.reviews:
        if review.pass_number != pass_number:
            continue
        if review.status != "accepted":
            continue
        if review_chain_is_stale(stored, review.review_ref):
            continue
        return True
    return False


def _has_stale_or_rejected_review(
    stored: StoredTranslationRecordV2, pass_number: int
) -> bool:
    """True when the record has only a stale/rejected review for a pass."""
    has_any = False
    for review in stored.reviews:
        if review.pass_number != pass_number:
            continue
        has_any = True
        if review.status == "accepted" and not review_chain_is_stale(
            stored, review.review_ref
        ):
            return False
    return has_any


def _eligible_for_pass(
    stored: StoredTranslationRecordV2, pcfg: ReviewPassConfig | None
) -> bool:
    """True when a record is an eligible base for this pass."""
    if pcfg is not None and pcfg.base == "active_review":
        required = pcfg.required_base_pass
        if required is not None:
            return _accepted_review_for_pass(stored, required)
    # Default: an accepted active translation version is the eligible base.
    active = active_candidate(stored)
    return active is not None and active.status == "accepted"


def _needs_review_for_pass(
    stored: StoredTranslationRecordV2,
    pass_number: int,
    pcfg: ReviewPassConfig | None,
) -> bool:
    """True when an eligible record still needs review for a pass.

    Covers both genuinely missing and stale/rejected-only records. Blocked
    records (eligible base unavailable) are excluded.
    """
    return eligible_for_pass(stored, pcfg) and not accepted_review_for_pass(
        stored, pass_number
    )


# Public aliases of the eligibility / accepted-review predicates. These were
# previously private (``_eligible_for_pass`` / ``_accepted_review_for_pass``)
# and re-imported under mangled names from ``booktx.review_todo``. Callers such
# as the review-todo gap index now use these stable public names.
eligible_for_pass = _eligible_for_pass
accepted_review_for_pass = _accepted_review_for_pass


@dataclass(frozen=True, slots=True)
class ReviewGapIndex:
    """Precomputed per-chapter review-gap counts for a single selection.

    Attributes:
        missing_by_chapter_pass: ``(chapter_id, pass_number)`` -> number of records
        in that chapter still needing review for that pass.
        missing_by_chapter: aggregate missing count per ``chapter_id`` across all
        active passes.
    """

    missing_by_chapter_pass: dict[tuple[str, int], int]
    missing_by_chapter: dict[str, int]


def build_review_gap_index(
    store: TranslationStoreV2,
    quality_cfg: QualityReviewConfig,
    *,
    record_order: list[tuple[str, str]] | None = None,
    chapter_records: Mapping[str, list[str]] | None = None,
) -> ReviewGapIndex:
    """Compute review-gap counts in a single pass over the store.

    Loads eligibility and accepted-review semantics once per record and
    aggregates per ``(chapter, pass)`` and per chapter. Pass-through uses the
    same eligibility/accepted predicates as ``compute_review_snapshot`` so the
    numbers match the snapshot.

    ``chapter_records`` defaults to None; callers that need a per-chapter split
    should pass the bundle's ``record_ids_by_chapter`` map. ``record_order`` is a
    ``(record_id, chapter_id)`` sequence in document order used when the chapter
    map is not available.
    """
    pass_cfg_by_number = {p.pass_number: p for p in quality_cfg.passes}
    by_cp: dict[tuple[str, int], int] = {}
    by_c: dict[str, int] = {}
    rid_to_chapter: dict[str, str] = {}
    if record_order:
        for rid, chapter in record_order:
            rid_to_chapter[rid] = chapter
    if chapter_records:
        for chapter, rids in chapter_records.items():
            for rid in rids:
                rid_to_chapter.setdefault(rid, chapter)
    for pass_number in quality_cfg.active_passes:
        pcfg = pass_cfg_by_number.get(pass_number)
        if pcfg is not None and not pcfg.enabled:
            continue
        for rid, stored in store.records.items():
            if not eligible_for_pass(stored, pcfg):
                continue
            if accepted_review_for_pass(stored, pass_number):
                continue
            chapter = rid_to_chapter.get(rid, "")
            by_cp[(chapter, pass_number)] = by_cp.get((chapter, pass_number), 0) + 1
            by_c[chapter] = by_c.get(chapter, 0) + 1
    return ReviewGapIndex(missing_by_chapter_pass=by_cp, missing_by_chapter=by_c)


class ReviewPassStatus(BaseModel):
    """Coverage status for one review pass."""

    model_config = ConfigDict(extra="forbid")

    pass_number: int
    name: str = ""
    enabled: bool
    enforce: Literal["off", "warn", "error"] = "off"
    eligible_records: int = 0
    reviewed_records: int = 0
    missing_review_records: int = 0
    stale_review_records: int = 0
    blocked_records: int = 0
    status: Literal["complete", "needs_review", "blocked", "disabled"] = "complete"
    first_missing_record: str | None = None
    first_missing_chapter: str | None = None


class ReviewStatusSnapshot(BaseModel):
    """Aggregate review coverage across configured passes."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    active_passes: list[int] = Field(default_factory=list)
    passes: list[ReviewPassStatus] = Field(default_factory=list)
    next_command: str | None = None
    first_missing_record: str | None = None
    first_missing_chapter: str | None = None


def compute_review_snapshot(
    store: TranslationStoreV2,
    quality_cfg: QualityReviewConfig | None,
    *,
    record_order: list[tuple[str, str]] | None = None,
) -> ReviewStatusSnapshot:
    """Compute per-pass review coverage for a store.

    ``record_order`` is an optional ``(record_id, chapter_id)`` sequence in
    document order. When supplied, each pass is annotated with the first record
    still needing review (missing or stale) and the snapshot's top-level
    ``first_missing_record``/``first_missing_chapter`` point at the first
    actionable pass. Returns a disabled snapshot when quality review is not
    enabled.
    """
    if quality_cfg is None or not quality_cfg.enabled:
        return ReviewStatusSnapshot(enabled=False, active_passes=[], passes=[])
    pass_cfg_by_number = {p.pass_number: p for p in quality_cfg.passes}
    active_passes = list(quality_cfg.active_passes)
    snapshot = ReviewStatusSnapshot(
        enabled=True, active_passes=active_passes, passes=[]
    )

    for pass_number in active_passes:
        pcfg = pass_cfg_by_number.get(pass_number)
        enabled = pcfg.enabled if pcfg is not None else True
        enforce = pcfg.enforce if pcfg is not None else "off"
        name = pcfg.name if pcfg is not None else ""
        status_obj = ReviewPassStatus(
            pass_number=pass_number,
            name=name,
            enabled=enabled,
            enforce=enforce,
        )
        if not enabled:
            status_obj.status = "disabled"
            snapshot.passes.append(status_obj)
            continue
        for stored in store.records.values():
            if not eligible_for_pass(stored, pcfg):
                continue
            status_obj.eligible_records += 1
            if accepted_review_for_pass(stored, pass_number):
                status_obj.reviewed_records += 1
            elif _has_stale_or_rejected_review(stored, pass_number):
                status_obj.stale_review_records += 1
            else:
                # Blocked when a required prior pass has no review for this record.
                required = pcfg.required_base_pass if pcfg is not None else None
                if (
                    required is not None
                    and required != pass_number
                    and not accepted_review_for_pass(stored, required)
                ):
                    status_obj.blocked_records += 1
                else:
                    status_obj.missing_review_records += 1
        if record_order is not None and (
            status_obj.missing_review_records > 0 or status_obj.stale_review_records > 0
        ):
            for rid, chapter in record_order:
                rec = store.records.get(rid)
                if rec is None:
                    continue
                if _needs_review_for_pass(rec, pass_number, pcfg):
                    status_obj.first_missing_record = rid
                    status_obj.first_missing_chapter = chapter
                    break
        if status_obj.blocked_records > 0 and status_obj.reviewed_records == 0:
            status_obj.status = "blocked"
        elif (
            status_obj.missing_review_records > 0 or status_obj.stale_review_records > 0
        ):
            status_obj.status = "needs_review"
        else:
            status_obj.status = "complete"
        snapshot.passes.append(status_obj)
    if record_order is not None:
        for p in snapshot.passes:
            if p.first_missing_record is not None:
                snapshot.first_missing_record = p.first_missing_record
                snapshot.first_missing_chapter = p.first_missing_chapter
                break
    return snapshot
