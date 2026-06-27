"""Direct unit tests for booktx.review_status coverage snapshots."""

from __future__ import annotations

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
