"""Helpers for working with nested translation stores.

This module also hosts the **legacy compatibility surface** for the old
flat (v1) ``TranslationStore``: :func:`legacy_store_to_v2` and
:func:`migrate_legacy_store` convert legacy stores into the nested v2 shape.
They are kept here, clearly named, so legacy import/export stays quarantined
from the active v2 store logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Literal

from booktx.models import (
    StoredTranslationRecordV2,
    TranslationCandidate,
    TranslationReviewCandidate,
    TranslationStore,
    TranslationStoreV2,
)
from booktx.record_refs import RecordRef, parse_record_ref, parse_version_ref
from booktx.review_refs import parse_review_ref

if TYPE_CHECKING:
    from booktx.progress import SourceRecordView

__all__ = [
    "MigrationResult",
    "EffectiveCandidateError",
    "EffectiveCandidateSelection",
    "active_candidate",
    "active_review_candidate",
    "effective_candidate_selection",
    "effective_target_candidate",
    "ensure_store_record",
    "find_candidate",
    "find_review_candidate",
    "migrate_legacy_store",
    "legacy_store_to_v2",
    "resolve_review_base",
    "review_candidate_is_stale",
    "review_chain_is_stale",
    "review_chain_refs",
    "sha256_text",
    "upsert_translation_version",
]


@dataclass(slots=True)
class MigrationResult:
    """Result of converting a legacy flat store into the v2 nested shape."""

    store: TranslationStoreV2
    migrated_records: int
    missing_source_ids: list[str]


def legacy_store_to_v2(
    legacy: TranslationStore,
    *,
    source_records: dict[str, SourceRecordView] | None = None,
) -> TranslationStoreV2:
    """Convert a legacy flat store into the nested v2 shape in memory."""
    records: dict[str, StoredTranslationRecordV2] = {}
    for record_id, stored in legacy.records.items():
        source_view = (
            source_records.get(record_id) if source_records is not None else None
        )
        record_ref = parse_record_ref(record_id)
        source_text = source_view.source if source_view is not None else ""
        source_sha256 = stored.source_sha256 or (
            source_view.source_sha256 if source_view is not None else ""
        )
        timestamp = stored.updated_at or ""
        records[record_ref.canonical_id] = StoredTranslationRecordV2(
            chunk_id=record_ref.chunk_id,
            part_id=record_ref.part_id,
            source_sha256=source_sha256,
            source=source_text,
            active_version="1.1",
            versions=[
                TranslationCandidate(
                    version=1,
                    subversion=1,
                    version_ref="1.1",
                    target=stored.target,
                    status=stored.status,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            ],
        )
    return TranslationStoreV2(source_sha256=legacy.source_sha256, records=records)


def migrate_legacy_store(
    legacy: TranslationStore,
    *,
    source_records: dict[str, SourceRecordView],
    version_ref: str = "1.1",
) -> MigrationResult:
    """Convert a legacy store into v2 and report any records missing source data."""
    parsed_version = parse_version_ref(version_ref)
    records: dict[str, StoredTranslationRecordV2] = {}
    missing_source_ids: list[str] = []
    migrated_records = 0
    for record_id, stored in legacy.records.items():
        source_view = source_records.get(record_id)
        if source_view is None:
            missing_source_ids.append(record_id)
            continue
        record_ref = parse_record_ref(record_id)
        timestamp = stored.updated_at or ""
        records[record_ref.canonical_id] = StoredTranslationRecordV2(
            chunk_id=record_ref.chunk_id,
            part_id=record_ref.part_id,
            source_sha256=source_view.source_sha256,
            source=source_view.source,
            active_version=parsed_version.version_ref,
            versions=[
                TranslationCandidate(
                    version=parsed_version.version,
                    subversion=parsed_version.subversion,
                    version_ref=parsed_version.version_ref,
                    target=stored.target,
                    status=stored.status,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            ],
        )
        migrated_records += 1
    return MigrationResult(
        store=TranslationStoreV2(source_sha256=legacy.source_sha256, records=records),
        migrated_records=migrated_records,
        missing_source_ids=sorted(missing_source_ids),
    )


def ensure_store_record(
    store: TranslationStoreV2,
    record_ref: RecordRef | str,
    *,
    source: str,
    source_sha256: str,
) -> StoredTranslationRecordV2:
    """Return the v2 store record, creating it when needed."""
    if isinstance(record_ref, str):
        record_ref = parse_record_ref(record_ref)
    record_id = record_ref.canonical_id
    record = store.records.get(record_id)
    if record is None:
        record = StoredTranslationRecordV2(
            chunk_id=record_ref.chunk_id,
            part_id=record_ref.part_id,
            source_sha256=source_sha256,
            source=source,
        )
        store.records[record_id] = record
        return record
    record.source_sha256 = source_sha256
    if source:
        record.source = source
    return record


def find_candidate(
    record: StoredTranslationRecordV2,
    version_ref: str,
) -> TranslationCandidate | None:
    """Return the matching candidate, if present."""
    normalized = parse_version_ref(version_ref).version_ref
    for candidate in record.versions:
        if candidate.version_ref == normalized:
            return candidate
    return None


def active_candidate(record: StoredTranslationRecordV2) -> TranslationCandidate | None:
    """Return the active candidate for a record, if any."""
    if record.active_version is None:
        return None
    return find_candidate(record, record.active_version)


def sha256_text(text: str) -> str:
    """Return the canonical SHA256 hex digest for a target/base text."""
    return sha256(text.encode("utf-8")).hexdigest()


def find_review_candidate(
    record: StoredTranslationRecordV2,
    review_ref: str,
) -> TranslationReviewCandidate | None:
    """Return the matching review candidate, if present."""
    normalized = parse_review_ref(review_ref).review_ref
    for candidate in record.reviews:
        if candidate.review_ref == normalized:
            return candidate
    return None


def resolve_review_base(
    record: StoredTranslationRecordV2,
    base_kind: Literal["translation", "review"],
    base_ref: str,
) -> TranslationCandidate | TranslationReviewCandidate | None:
    """Resolve the base candidate a review was derived from."""
    if base_kind == "translation":
        return find_candidate(record, base_ref)
    return find_review_candidate(record, base_ref)


def review_candidate_is_stale(
    record: StoredTranslationRecordV2,
    review: TranslationReviewCandidate,
) -> bool:
    """Return True when the direct base of a review is missing or drifted."""
    base = resolve_review_base(record, review.base_kind, review.base_ref)
    if base is None:
        return True
    return sha256_text(base.target) != review.base_target_sha256


def review_chain_is_stale(
    record: StoredTranslationRecordV2,
    review_ref: str,
) -> bool:
    """Return True when any base in the review derivation chain is missing or drifted.

    Walks from ``review_ref`` back to the translation base, rejecting missing
    bases, target-hash mismatches, and cycles.
    """
    seen: set[str] = set()
    current = find_review_candidate(record, review_ref)
    while current is not None:
        if current.review_ref in seen:
            return True
        seen.add(current.review_ref)
        if review_candidate_is_stale(record, current):
            return True
        if current.base_kind == "translation":
            return False
        current = find_review_candidate(record, current.base_ref)
    return True


def active_review_candidate(
    record: StoredTranslationRecordV2,
) -> TranslationReviewCandidate | None:
    """Return the usable active review candidate, or None if unusable.

    A review is usable only when present, accepted, and chain-valid. Stale,
    rejected, cyclic, or invalid-pass-order active reviews are not returned.
    """
    if record.active_review is None:
        return None
    candidate = find_review_candidate(record, record.active_review)
    if candidate is None or candidate.status != "accepted":
        return None
    if review_chain_is_stale(record, candidate.review_ref):
        return None
    return candidate


def effective_target_candidate(
    record: StoredTranslationRecordV2,
) -> TranslationCandidate | TranslationReviewCandidate | None:
    """Resolve the effective output target for a record.

    Prefers the active review candidate when present and chain-valid;
    otherwise falls back to the active translation version.
    """
    review = active_review_candidate(record)
    if review is not None:
        return review
    return active_candidate(record)


@dataclass(slots=True)
class EffectiveCandidateSelection:
    """Effective output candidate plus provenance metadata.

    ``selected_kind``/``selected_ref`` identify which candidate produces the
    output. ``version_ref`` is the base translation version the output derives
    from; ``review_ref`` is the selected review ref when the output is a
    review candidate. ``review_chain`` is the ordered review chain (earliest
    first) for review selections and empty for direct translations.
    """

    candidate: TranslationCandidate | TranslationReviewCandidate
    selected_kind: Literal["translation", "review"]
    selected_ref: str
    version_ref: str | None
    review_ref: str | None
    review_chain: list[str]


@dataclass(slots=True)
class EffectiveCandidateError:
    """A blocking problem resolving the effective candidate for a record.

    ``rule`` mirrors the validation rule names (``active_review_missing``,
    ``active_review_not_accepted``, ``active_review_base_drift``) so editor
    index errors stay aligned with build/validate findings.
    """

    rule: str
    message: str


def review_chain_refs(
    record: StoredTranslationRecordV2,
    review_ref: str,
) -> list[str] | None:
    """Return the ordered review chain ending at ``review_ref`` (earliest first).

    The chain excludes the translation base. For ``R2.1`` based on ``R1.1``
    based on a translation version, returns ``["R1.1", "R2.1"]``. Returns
    ``None`` when the chain is missing, stale (``base_target_sha256`` drift),
    cyclic, or violates the lexicographic ``(pass, run)`` order (so same-pass
    reruns such as ``R1.2`` from ``R1.1`` are valid).
    """
    normalized = parse_review_ref(review_ref).review_ref
    ordered: list[str] = []
    seen: set[str] = set()
    current = find_review_candidate(record, normalized)
    while current is not None:
        if current.review_ref in seen:
            return None  # cycle
        seen.add(current.review_ref)
        # Missing base or drifted target hash invalidates the chain.
        if review_candidate_is_stale(record, current):
            return None
        ordered.append(current.review_ref)
        if current.base_kind == "translation":
            ordered.reverse()
            return ordered
        base_review = find_review_candidate(record, current.base_ref)
        if base_review is None or (
            current.pass_number,
            current.run_number,
        ) <= (base_review.pass_number, base_review.run_number):
            return None
        current = base_review
    return None


def _review_base_version_ref(
    record: StoredTranslationRecordV2,
    review: TranslationReviewCandidate,
) -> str | None:
    """Walk a review chain to its translation base version ref."""
    current: TranslationReviewCandidate | None = review
    seen: set[str] = set()
    while current is not None:
        if current.review_ref in seen:
            return None
        seen.add(current.review_ref)
        if current.base_kind == "translation":
            return current.base_ref
        current = find_review_candidate(record, current.base_ref)
    return None


def _strict_error(
    strict: bool,
    rule: str,
    message: str,
) -> EffectiveCandidateError | None:
    """Return an error when strict, otherwise fall back to translation."""
    return EffectiveCandidateError(rule, message) if strict else None


def _select_review(
    record: StoredTranslationRecordV2,
    *,
    strict: bool,
) -> EffectiveCandidateSelection | EffectiveCandidateError | None:
    """Attempt to select the active review candidate.

    Returns :class:`EffectiveCandidateSelection` when the active review is
    valid and chain-accepted. Returns :class:`EffectiveCandidateError` when
    the review is unusable and ``strict`` is set. Returns ``None`` when there
    is no active review or it is unusable without strict mode.
    """
    if record.active_review is None:
        return None
    candidate = find_review_candidate(record, record.active_review)
    if candidate is None:
        return _strict_error(
            strict,
            "active_review_missing",
            f"active review {record.active_review!r} has no matching review candidate",
        )
    if candidate.status != "accepted":
        return _strict_error(
            strict,
            "active_review_not_accepted",
            f"active review {candidate.review_ref} is {candidate.status!r},"
            " not accepted",
        )
    chain = review_chain_refs(record, candidate.review_ref)
    base_version = (
        _review_base_version_ref(record, candidate) if chain is not None else None
    )
    if chain is None or base_version is None:
        return _strict_error(
            strict,
            "active_review_base_drift",
            f"active review {candidate.review_ref} has a stale, missing,"
            " or cyclic base chain",
        )
    return EffectiveCandidateSelection(
        candidate=candidate,
        selected_kind="review",
        selected_ref=candidate.review_ref,
        version_ref=base_version,
        review_ref=candidate.review_ref,
        review_chain=chain,
    )


def effective_candidate_selection(
    record: StoredTranslationRecordV2,
    *,
    strict_active_review: bool = True,
) -> EffectiveCandidateSelection | EffectiveCandidateError | None:
    """Resolve the effective candidate plus selection metadata for a record.

    With ``strict_active_review=True`` (the default), a set-but-unusable
    ``active_review`` returns an :class:`EffectiveCandidateError` instead of
    silently falling back to the active translation. This is the safe mode for
    editor indexes, which must never mask an invalid active review. With
    ``strict_active_review=False`` an unusable active review falls back to the
    accepted active translation, mirroring :func:`effective_target_candidate`.

    Returns ``None`` only when there is no accepted effective candidate.
    """
    review_result = _select_review(record, strict=strict_active_review)
    if isinstance(review_result, EffectiveCandidateError):
        return review_result
    if review_result is not None:
        return review_result
    # No usable active review: select the accepted active translation version.
    translation = active_candidate(record)
    if translation is None or translation.status != "accepted":
        return None
    return EffectiveCandidateSelection(
        candidate=translation,
        selected_kind="translation",
        selected_ref=translation.version_ref,
        version_ref=translation.version_ref,
        review_ref=None,
        review_chain=[],
    )


def upsert_translation_version(
    record: StoredTranslationRecordV2,
    version_ref: str,
    target: str,
    *,
    updated_at: str,
    status: str = "accepted",
    activate: bool = False,
    baseline_ref: str | None = None,
    baseline_sha256: str | None = None,
    context_view_sha256: str | None = None,
    context_view_path: str | None = None,
    context_notes_scope: str | None = None,
    context_target_chapter_id: str | None = None,
    context_notes_through_chapter_id: str | None = None,
) -> TranslationCandidate:
    """Insert or update a candidate version on one record."""
    parsed = parse_version_ref(version_ref)
    existing = find_candidate(record, parsed.version_ref)
    if existing is None:
        candidate = TranslationCandidate(
            version=parsed.version,
            subversion=parsed.subversion,
            version_ref=parsed.version_ref,
            baseline_ref=baseline_ref,
            baseline_sha256=baseline_sha256,
            context_view_sha256=context_view_sha256,
            context_view_path=context_view_path,
            context_notes_scope=context_notes_scope,
            context_target_chapter_id=context_target_chapter_id,
            context_notes_through_chapter_id=context_notes_through_chapter_id,
            target=target,
            status=status,
            created_at=updated_at,
            updated_at=updated_at,
        )
        record.versions.append(candidate)
    else:
        existing.target = target
        existing.status = status
        existing.updated_at = updated_at
        if baseline_ref is not None:
            existing.baseline_ref = baseline_ref
        if baseline_sha256 is not None:
            existing.baseline_sha256 = baseline_sha256
        if context_view_sha256 is not None:
            existing.context_view_sha256 = context_view_sha256
        if context_view_path is not None:
            existing.context_view_path = context_view_path
        if context_notes_scope is not None:
            existing.context_notes_scope = context_notes_scope
        if context_target_chapter_id is not None:
            existing.context_target_chapter_id = context_target_chapter_id
        if context_notes_through_chapter_id is not None:
            existing.context_notes_through_chapter_id = context_notes_through_chapter_id
        candidate = existing
    if record.active_version is None and candidate.status == "accepted":
        record.active_version = candidate.version_ref
    elif activate:
        record.active_version = candidate.version_ref
    return candidate
