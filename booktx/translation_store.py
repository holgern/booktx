"""Helpers for working with nested translation stores."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from booktx.models import (
    StoredTranslationRecordV2,
    TranslationCandidate,
    TranslationStore,
    TranslationStoreV2,
)
from booktx.record_refs import RecordRef, parse_record_ref, parse_version_ref

if TYPE_CHECKING:
    from booktx.progress import SourceRecordView

__all__ = [
    "MigrationResult",
    "active_candidate",
    "ensure_store_record",
    "find_candidate",
    "migrate_legacy_store",
    "legacy_store_to_v2",
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


def upsert_translation_version(
    record: StoredTranslationRecordV2,
    version_ref: str,
    target: str,
    *,
    updated_at: str,
    status: str = "accepted",
    activate: bool = False,
) -> TranslationCandidate:
    """Insert or update a candidate version on one record."""
    parsed = parse_version_ref(version_ref)
    existing = find_candidate(record, parsed.version_ref)
    if existing is None:
        candidate = TranslationCandidate(
            version=parsed.version,
            subversion=parsed.subversion,
            version_ref=parsed.version_ref,
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
        candidate = existing
    if record.active_version is None and candidate.status == "accepted":
        record.active_version = candidate.version_ref
    elif activate:
        record.active_version = candidate.version_ref
    return candidate
