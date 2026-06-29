"""Direct unit tests for booktx.review_status coverage snapshots."""

from __future__ import annotations

import hashlib

from booktx.models import (
    QualityReviewConfig,
    ReviewPassConfig,
    StoredTranslationRecordV2,
    TranslationCandidate,
    TranslationReviewCandidate,
    TranslationStoreV2,
)
from booktx.review_status import compute_review_snapshot
from booktx.translation_store import sha256_text

_CREATED = "2026-06-25T10:00:00Z"


def _record(rid: str, *, target: str, reviews=None, active_review=None):
    cid, pid = (int(x) for x in rid.split("-"))
    return StoredTranslationRecordV2(
        chunk_id=cid,
        part_id=pid,
        source_sha256="x",
        source="src " + rid,
        active_version="1.1",
        active_review=active_review,
        versions=[
            TranslationCandidate(
                version=1,
                subversion=1,
                version_ref="1.1",
                target=target,
                created_at=_CREATED,
                updated_at=_CREATED,
            )
        ],
        reviews=reviews or [],
    )


def _review(target: str, *, base_target: str, review_ref="R1.1", pass_number=1):
    run = int(review_ref.split(".")[1])
    return TranslationReviewCandidate(
        pass_number=pass_number,
        run_number=run,
        review_ref=review_ref,
        base_kind="translation",
        base_ref="1.1",
        base_target_sha256=sha256_text(base_target),
        target=target,
        target_sha256=sha256_text(target),
        created_at=_CREATED,
        updated_at=_CREATED,
    )


def _review_from_review(target: str, *, base_target: str, review_ref, base_ref):
    pn = int(review_ref.split("R")[1].split(".")[0])
    run = int(review_ref.split(".")[1])
    return TranslationReviewCandidate(
        pass_number=pn,
        run_number=run,
        review_ref=review_ref,
        base_kind="review",
        base_ref=base_ref,
        base_target_sha256=sha256_text(base_target),
        target=target,
        target_sha256=sha256_text(target),
        created_at=_CREATED,
        updated_at=_CREATED,
    )


def test_snapshot_disabled_when_quality_review_not_enabled():
    store = TranslationStoreV2(
        records={"0001-000001": _record("0001-000001", target="t")}
    )
    snap = compute_review_snapshot(store, None)
    assert snap.enabled is False
    assert snap.passes == []

    cfg = QualityReviewConfig(enabled=False)
    snap = compute_review_snapshot(store, cfg)
    assert snap.enabled is False


def test_snapshot_pass1_missing_review():
    store = TranslationStoreV2(
        records={
            "0001-000001": _record("0001-000001", target="t1"),
            "0001-000002": _record("0001-000002", target="t2"),
        }
    )
    cfg = QualityReviewConfig(
        enabled=True,
        active_passes=[1],
        passes=[ReviewPassConfig(pass_number=1, enforce="warn")],
    )
    snap = compute_review_snapshot(store, cfg)
    p1 = snap.passes[0]
    assert p1.eligible_records == 2
    assert p1.reviewed_records == 0
    assert p1.missing_review_records == 2
    assert p1.status == "needs_review"


def test_snapshot_pass1_reviewed_and_missing():
    r2_target = "t2"
    r2 = _record(
        "0001-000002",
        target=r2_target,
        reviews=[_review(target="polished", base_target=r2_target)],
        active_review="R1.1",
    )
    store = TranslationStoreV2(
        records={
            "0001-000001": _record("0001-000001", target="t1"),
            "0001-000002": r2,
        }
    )
    cfg = QualityReviewConfig(
        enabled=True,
        active_passes=[1],
        passes=[ReviewPassConfig(pass_number=1, enforce="warn")],
    )
    snap = compute_review_snapshot(store, cfg)
    p1 = snap.passes[0]
    assert p1.eligible_records == 2
    assert p1.reviewed_records == 1
    assert p1.missing_review_records == 1
    assert p1.status == "needs_review"


def test_snapshot_stale_review_counted_separately():
    r1 = _record(
        "0001-000001",
        target="t1",
        reviews=[_review(target="stale", base_target="different")],
        active_review="R1.1",
    )
    store = TranslationStoreV2(records={"0001-000001": r1})
    cfg = QualityReviewConfig(
        enabled=True,
        active_passes=[1],
        passes=[ReviewPassConfig(pass_number=1, enforce="warn")],
    )
    snap = compute_review_snapshot(store, cfg)
    p1 = snap.passes[0]
    assert p1.eligible_records == 1
    assert p1.reviewed_records == 0
    assert p1.stale_review_records == 1


def test_snapshot_two_pass_blocks_pass2_when_pass1_missing():
    store = TranslationStoreV2(
        records={"0001-000001": _record("0001-000001", target="t1")}
    )
    cfg = QualityReviewConfig(
        enabled=True,
        active_passes=[1, 2],
        passes=[
            ReviewPassConfig(pass_number=1, enforce="warn"),
            ReviewPassConfig(
                pass_number=2,
                enforce="warn",
                base="active_review",
                required_base_pass=1,
            ),
        ],
    )
    snap = compute_review_snapshot(store, cfg)
    p1, p2 = snap.passes
    assert p1.missing_review_records == 1
    # Pass 2 has no eligible records (pass 1 review missing) and is blocked.
    assert p2.eligible_records == 0
    assert p2.status == "complete"  # nothing eligible -> nothing blocked


def test_snapshot_two_pass_pass2_eligible_after_pass1_review():
    r1_target = "t1"
    r1 = _record(
        "0001-000001",
        target=r1_target,
        reviews=[_review(target="p1-out", base_target=r1_target, review_ref="R1.1")],
        active_review="R1.1",
    )
    store = TranslationStoreV2(records={"0001-000001": r1})
    cfg = QualityReviewConfig(
        enabled=True,
        active_passes=[1, 2],
        passes=[
            ReviewPassConfig(pass_number=1, enforce="warn"),
            ReviewPassConfig(
                pass_number=2,
                enforce="warn",
                base="active_review",
                required_base_pass=1,
            ),
        ],
    )
    snap = compute_review_snapshot(store, cfg)
    p1, p2 = snap.passes
    assert p1.reviewed_records == 1
    assert p1.status == "complete"
    assert p2.eligible_records == 1
    assert p2.missing_review_records == 1
    assert p2.status == "needs_review"


def test_snapshot_disabled_pass_is_marked_disabled():
    store = TranslationStoreV2(
        records={"0001-000001": _record("0001-000001", target="t1")}
    )
    cfg = QualityReviewConfig(
        enabled=True,
        active_passes=[1],
        passes=[ReviewPassConfig(pass_number=1, enabled=False, enforce="warn")],
    )
    snap = compute_review_snapshot(store, cfg)
    assert snap.passes[0].status == "disabled"


def test_snapshot_record_order_populates_first_missing():
    store = TranslationStoreV2(
        records={
            "0001-000001": _record("0001-000001", target="t1"),
            "0001-000002": _record("0001-000002", target="t2"),
        }
    )
    cfg = QualityReviewConfig(
        enabled=True,
        active_passes=[1],
        passes=[ReviewPassConfig(pass_number=1, enforce="warn")],
    )
    # Document order puts 000002 first.
    order = [("0001-000002", "0001"), ("0001-000001", "0001")]
    snap = compute_review_snapshot(store, cfg, record_order=order)
    p1 = snap.passes[0]
    assert p1.first_missing_record == "0001-000002"
    assert p1.first_missing_chapter == "0001"
    # Top-level points at the first actionable pass.
    assert snap.first_missing_record == "0001-000002"
    assert snap.first_missing_chapter == "0001"


def test_snapshot_first_missing_none_when_complete():
    r_target = "t1"
    r1 = _record(
        "0001-000001",
        target=r_target,
        reviews=[_review(target="polished", base_target=r_target)],
        active_review="R1.1",
    )
    store = TranslationStoreV2(records={"0001-000001": r1})
    cfg = QualityReviewConfig(
        enabled=True,
        active_passes=[1],
        passes=[ReviewPassConfig(pass_number=1, enforce="warn")],
    )
    snap = compute_review_snapshot(store, cfg, record_order=[("0001-000001", "0001")])
    assert snap.passes[0].first_missing_record is None
    assert snap.first_missing_record is None


# ---------------------------------------------------------------------------
# Public review-gap API (Phase 2).
#
# The public aliases ``eligible_for_pass`` / ``accepted_review_for_pass`` and the
# ``ReviewGapIndex`` / ``build_review_gap_index`` API must be testable
# independently of ``compute_review_snapshot``.
# ---------------------------------------------------------------------------


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_stored(
    *,
    source: str = "Alice ran fast.",
    has_active_translation: bool = True,
    reviews: list | None = None,
    chunk_id: int = 1,
    part_id: int = 1,
) -> StoredTranslationRecordV2:
    from booktx.models import (
        StoredTranslationRecordV2,
        TranslationCandidate,
    )

    versions: list = []
    if has_active_translation:
        versions.append(
            TranslationCandidate(
                version=1,
                subversion=1,
                version_ref="1.1",
                target=source,
                status="accepted",
                created_at="2026-06-22T12:00:00Z",
                updated_at="2026-06-22T12:00:00Z",
            )
        )
    return StoredTranslationRecordV2(
        chunk_id=chunk_id,
        part_id=part_id,
        source_sha256=_sha(source),
        source=source,
        active_version="1.1" if has_active_translation else None,
        versions=versions,
        reviews=list(reviews or []),
    )


def _make_accepted_review(target: str = "reviewed") -> TranslationReviewCandidate:
    from booktx.models import TranslationReviewCandidate

    return TranslationReviewCandidate(
        pass_number=1,
        run_number=1,
        review_ref="R1.1",
        base_kind="translation",
        base_ref="1.1",
        base_target_sha256=_sha("Alice ran fast."),
        target=target,
        target_sha256=_sha(target),
        status="accepted",
        created_at="2026-06-22T12:00:00Z",
        updated_at="2026-06-22T12:00:00Z",
        review_task_id=None,
        review_note="ok",
    )


def test_eligible_for_pass_accepts_active_translation():
    from booktx.review_status import eligible_for_pass

    stored = _make_stored()
    assert eligible_for_pass(stored, None) is True
    from booktx.models import ReviewPassConfig

    assert (
        eligible_for_pass(
            stored, ReviewPassConfig(pass_number=1, base="active_translation")
        )
        is True
    )


def test_eligible_for_pass_rejects_missing_translation():
    from booktx.review_status import eligible_for_pass

    stored = _make_stored(has_active_translation=False)
    assert eligible_for_pass(stored, None) is False


def test_accepted_review_for_pass_finds_accepted_review():
    from booktx.review_status import accepted_review_for_pass

    stored = _make_stored(reviews=[_make_accepted_review()])
    assert accepted_review_for_pass(stored, 1) is True
    assert accepted_review_for_pass(stored, 2) is False


def test_build_review_gap_index_counts_missing_per_chapter_pass():
    from booktx.models import QualityReviewConfig, ReviewPassConfig
    from booktx.review_status import build_review_gap_index
    from booktx.translation_store import TranslationStoreV2

    rec1 = _make_stored(source="rec-1", part_id=1)
    rec2 = _make_stored(source="rec-2", part_id=2)
    store = TranslationStoreV2(records={"0001-000001": rec1, "0001-000002": rec2})
    cfg = QualityReviewConfig(
        enabled=True,
        active_passes=[1],
        passes=[ReviewPassConfig(pass_number=1, enforce="warn")],
    )
    idx = build_review_gap_index(
        store,
        cfg,
        chapter_records={"ch1": ["0001-000001", "0001-000002"]},
    )
    assert idx.missing_by_chapter == {"ch1": 2}
    assert idx.missing_by_chapter_pass == {("ch1", 1): 2}


def test_build_review_gap_index_respects_active_passes():
    from booktx.models import QualityReviewConfig, ReviewPassConfig
    from booktx.review_status import build_review_gap_index
    from booktx.translation_store import TranslationStoreV2

    store = TranslationStoreV2(records={"0001-000001": _make_stored()})
    cfg = QualityReviewConfig(
        enabled=True,
        active_passes=[1, 2],
        passes=[
            ReviewPassConfig(pass_number=1, enforce="warn"),
            ReviewPassConfig(pass_number=2, enforce="warn"),
        ],
    )
    idx = build_review_gap_index(store, cfg, chapter_records={"ch1": ["0001-000001"]})
    # 0001-000001 needs review for both active passes.
    assert idx.missing_by_chapter == {"ch1": 2}
    assert idx.missing_by_chapter_pass == {("ch1", 1): 1, ("ch1", 2): 1}


def test_build_review_gap_index_skips_already_accepted_reviews():
    from booktx.models import QualityReviewConfig, ReviewPassConfig
    from booktx.review_status import build_review_gap_index
    from booktx.translation_store import TranslationStoreV2

    store = TranslationStoreV2(
        records={"0001-000001": _make_stored(reviews=[_make_accepted_review()])}
    )
    cfg = QualityReviewConfig(
        enabled=True,
        active_passes=[1],
        passes=[ReviewPassConfig(pass_number=1, enforce="warn")],
    )
    idx = build_review_gap_index(store, cfg, chapter_records={"ch1": ["0001-000001"]})
    # 0001-000001 already has an accepted pass-1 review, so nothing is missing.
    assert idx.missing_by_chapter == {}
    assert idx.missing_by_chapter_pass == {}
