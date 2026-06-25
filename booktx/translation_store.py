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
    "active_candidate",
    "active_review_candidate",
    "effective_target_candidate",
    "ensure_store_record",
    "find_candidate",
    "find_review_candidate",
    "migrate_legacy_store",
    "legacy_store_to_v2",
    "resolve_review_base",
    "review_candidate_is_stale",
    "review_chain_is_stale",
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
