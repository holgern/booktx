"""Direct unit tests for review store helpers in booktx.translation_store.

Covers effective target resolution, review staleness detection, and chain
staleness propagation through ``1.1 -> R1.1 -> R2.1``.
"""

from __future__ import annotations

from booktx.models import (
    StoredTranslationRecordV2,
    TranslationCandidate,
    TranslationReviewCandidate,
)
from booktx.translation_store import (
    active_review_candidate,
    effective_target_candidate,
    find_review_candidate,
    review_candidate_is_stale,
    review_chain_is_stale,
    sha256_text,
)

_CREATED = "2026-06-25T10:00:00Z"


def _version(target: str, *, version_ref: str = "1.1") -> TranslationCandidate:
    v, sub = (int(n) for n in version_ref.split("."))
    return TranslationCandidate(
        version=v,
        subversion=sub,
        version_ref=version_ref,
        target=target,
        created_at=_CREATED,
        updated_at=_CREATED,
    )


def _review(
    target: str,
    *,
    review_ref: str = "R1.1",
    base_kind: str = "translation",
    base_ref: str = "1.1",
    base_target: str | None = None,
) -> TranslationReviewCandidate:
    pass_number = int(review_ref.split("R")[1].split(".")[0])
    run_number = int(review_ref.split(".")[1])
    return TranslationReviewCandidate(
        pass_number=pass_number,
        run_number=run_number,
        review_ref=review_ref,
        base_kind=base_kind,
        base_ref=base_ref,
        base_target_sha256=sha256_text(base_target if base_target is not None else ""),
        target=target,
        target_sha256=sha256_text(target),
        created_at=_CREATED,
        updated_at=_CREATED,
    )


def _record(
    *,
    source: str = "src",
    active_version: str | None = "1.1",
    active_review: str | None = None,
    versions: list[TranslationCandidate] | None = None,
    reviews: list[TranslationReviewCandidate] | None = None,
) -> StoredTranslationRecordV2:
    return StoredTranslationRecordV2(
        chunk_id=2,
        part_id=17,
        source_sha256="x",
        source=source,
        active_version=active_version,
        active_review=active_review,
        versions=versions if versions is not None else [_version("first-pass")],
        reviews=reviews if reviews is not None else [],
    )


# --- effective target resolution -------------------------------------------


def test_effective_returns_active_version_when_no_review():
    rec = _record()
    eff = effective_target_candidate(rec)
    assert eff is not None
    assert eff.target == "first-pass"


def test_effective_returns_active_review_when_present_and_valid():
    r1 = _review("polished", base_target="first-pass")
    rec = _record(reviews=[r1], active_review="R1.1")
    eff = effective_target_candidate(rec)
    assert eff is not None
    assert eff.target == "polished"


def test_effective_falls_back_to_version_when_review_stale():
    # Review recorded a base hash that no longer matches the translation target.
    r1 = _review("polished", base_target="different-baseline")
    rec = _record(versions=[_version("first-pass")], reviews=[r1], active_review="R1.1")
    eff = effective_target_candidate(rec)
    assert eff is not None
    assert eff.target == "first-pass"


def test_active_review_candidate_returns_none_when_missing():
    # The model invariant forbids a dangling active_review, so simulate the
    # defensive case by copying after construction (no re-validation).
    rec = _record().model_copy(update={"active_review": "R9.9"})
    assert active_review_candidate(rec) is None


def test_active_review_candidate_returns_none_when_rejected():
    r1 = _review("polished", base_target="first-pass")
    r1 = r1.model_copy(update={"status": "rejected"})
    rec = _record(reviews=[r1], active_review="R1.1")
    assert active_review_candidate(rec) is None


# --- staleness detection ----------------------------------------------------


def test_review_candidate_is_stale_on_hash_drift():
    r1 = _review("polished", base_target="changed-baseline")
    rec = _record(versions=[_version("first-pass")], reviews=[r1])
    assert review_candidate_is_stale(rec, r1) is True


def test_review_candidate_is_not_stale_when_base_matches():
    r1 = _review("polished", base_target="first-pass")
    rec = _record(versions=[_version("first-pass")], reviews=[r1])
    assert review_candidate_is_stale(rec, r1) is False


def test_find_review_candidate_normalizes_ref():
    r1 = _review("polished", base_target="first-pass")
    rec = _record(reviews=[r1])
    assert find_review_candidate(rec, "R1.1") is not None
    assert find_review_candidate(rec, "  R1.1 ") is not None
    assert find_review_candidate(rec, "R2.1") is None


# --- chain staleness --------------------------------------------------------


def test_chain_staleness_propagates_to_higher_pass():
    """Changing 1.1 after R1.1 was created makes R1.1 and R2.1 both stale."""
    v11 = _version("first-pass")
    r1 = _review("pass1-output", review_ref="R1.1", base_target="first-pass")
    r2 = _review(
        "final-polish",
        review_ref="R2.1",
        base_kind="review",
        base_ref="R1.1",
        base_target="pass1-output",
    )
    # First: everything valid.
    rec = _record(versions=[v11], reviews=[r1, r2], active_review="R2.1")
    assert review_chain_is_stale(rec, "R1.1") is False
    assert review_chain_is_stale(rec, "R2.1") is False
    assert effective_target_candidate(rec).target == "final-polish"  # type: ignore[union-attr]

    # Now drift the translation target after R1.1/R2.1 were recorded.
    rec_drifted = _record(
        versions=[_version("first-pass-EDITED")], reviews=[r1, r2], active_review="R2.1"
    )
    assert review_chain_is_stale(rec_drifted, "R1.1") is True
    assert review_chain_is_stale(rec_drifted, "R2.1") is True
    # Effective output falls back to the active version.
    assert effective_target_candidate(rec_drifted).target == "first-pass-EDITED"  # type: ignore[union-attr]


def test_chain_stale_when_review_base_missing():
    v11 = _version("first-pass")
    r1 = _review("pass1-output", review_ref="R1.1", base_target="first-pass")
    rec = _record(versions=[v11], reviews=[r1], active_review="R1.1")
    # Remove the translation version: R1.1's base is now missing.
    rec = rec.model_copy(update={"versions": [], "active_version": None})
    assert review_chain_is_stale(rec, "R1.1") is True


def test_chain_stale_for_missing_review_ref():
    rec = _record()
    assert review_chain_is_stale(rec, "R9.9") is True


def test_chain_resolves_same_pass_rerun():
    """R1.2 based on R1.1 resolves as a valid (non-stale) same-pass chain."""
    v11 = _version("first-pass")
    r1 = _review("pass1-output", review_ref="R1.1", base_target="first-pass")
    r12 = _review(
        "rerun-output",
        review_ref="R1.2",
        base_kind="review",
        base_ref="R1.1",
        base_target="pass1-output",
    )
    rec = _record(versions=[v11], reviews=[r1, r12], active_review="R1.2")
    assert review_chain_is_stale(rec, "R1.2") is False
    from booktx.translation_store import review_chain_refs

    assert review_chain_refs(rec, "R1.2") == ["R1.1", "R1.2"]
    assert effective_target_candidate(rec).target == "rerun-output"  # type: ignore[union-attr]
