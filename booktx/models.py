"""Pydantic data models for the booktx translation contract.

This module defines the JSON shapes that cross the boundary between booktx and
the translating coding agent:

- Source chunk  -> ``chunks/NNNN.json``  (written by ``booktx extract``)
- Translated chunk -> ``translated/NNNN.json`` (written by the agent)

Both must round-trip through JSON with stable field names and ordering.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from booktx.record_refs import canonical_record_id, format_version_ref, parse_version_ref

__all__ = [
    "Placeholder",
    "Record",
    "TranslatedRecord",
    "Chunk",
    "TranslatedChunk",
    "StoredTranslationRecord",
    "TranslationStore",
    "TranslationCandidate",
    "StoredTranslationRecordV2",
    "TranslationStoreV2",
    "TranslationSubversionLedgerEntry",
    "TranslationTrackLedgerEntry",
    "TranslationVersionLedger",
    "TranslationIdentity",
    "TranslationTaskRecord",
    "TranslationTask",
    "NamesFile",
    "ProjectConfig",
    "EpubSpanRef",
    "EpubNavigationRef",
    "EpubTemplateData",
    "ManifestSource",
    "Manifest",
]


class Placeholder(BaseModel):
    """A non-translatable span that was replaced by a token during extraction.

    ``token`` is the exact placeholder text that appears in the source field
    (e.g. ``__NAME_001__`` or ``__TAG_001__``); ``original`` is the verbatim
    text that must be restored verbatim during build.
    """

    model_config = ConfigDict(extra="forbid")

    token: str = Field(..., description="Placeholder token, e.g. __NAME_001__")
    original: str = Field(..., description="Original text to restore verbatim")
    kind: Literal["name", "tag"] = Field("tag", description="Placeholder kind")


class Record(BaseModel):
    """A single translatable source record inside a source chunk."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Record id, e.g. 0001-000001")
    source: str = Field(..., description="Source text with placeholders")
    protected_terms: list[str] = Field(
        default_factory=list,
        description="Protected names present in this record",
    )
    placeholders: list[Placeholder] = Field(
        default_factory=list,
        description="Placeholders present in this record",
    )
    span_index: int | None = Field(
        default=None,
        description="Original prose-span index for paragraph-aware grouping",
    )
    span_record_index: int | None = Field(
        default=None,
        description="Sentence index inside the original prose span",
    )

    @field_validator("source")
    @classmethod
    def _source_not_none(cls, v: str) -> str:
        if v is None:  # pragma: no cover - pydantic guards None already
            raise ValueError("source must not be null")
        return v


class TranslatedRecord(BaseModel):
    """A single translated record inside a translated chunk."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Record id matching the source chunk")
    target: str = Field(..., description="Translated text")


class Chunk(BaseModel):
    """A source chunk file (``chunks/NNNN.json``)."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str = Field(..., description="Chunk id, e.g. 0001")
    source_language: str = Field(..., description="BCP-47-ish source code, e.g. en")
    target_language: str = Field(..., description="BCP-47-ish target code, e.g. de")
    records: list[Record] = Field(default_factory=list)


class TranslatedChunk(BaseModel):
    """A translated chunk file (``translated/NNNN.json``)."""

    # Allow extra keys to be *detectable* rather than silently dropped, but we
    # do not want pydantic to throw on load; the validator enforces the contract.
    model_config = ConfigDict(extra="allow")

    chunk_id: str = Field(..., description="Chunk id, e.g. 0001")
    records: list[TranslatedRecord] = Field(default_factory=list)


class StoredTranslationRecord(BaseModel):
    """One accepted record stored in ``translation-store.json``."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str = Field(..., description="Owning chunk id, e.g. 0001")
    source_sha256: str = Field(
        default="", description="SHA256 of the source record text when accepted"
    )
    target: str = Field(..., description="Accepted target text")
    status: Literal["accepted"] = Field(default="accepted")
    updated_at: str = Field(
        default="", description="UTC timestamp of the latest accepted update"
    )


class TranslationStore(BaseModel):
    """Primary record-level translation state owned by booktx."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1)
    source_sha256: str = Field(default="", description="Current project source SHA256")
    records: dict[str, StoredTranslationRecord] = Field(default_factory=dict)


class TranslationCandidate(BaseModel):
    """One candidate translation stored under a record version."""

    model_config = ConfigDict(extra="forbid")

    version: int
    subversion: int
    version_ref: str
    target: str
    status: str = "accepted"
    created_at: str
    updated_at: str
    reviewed_at: str | None = None
    reviewed_by: str | None = None
    review_note: str | None = None

    @field_validator("version_ref")
    @classmethod
    def _version_ref_shape(cls, value: str) -> str:
        return parse_version_ref(value).version_ref

    @model_validator(mode="after")
    def _version_ref_matches_fields(self) -> "TranslationCandidate":
        expected = format_version_ref(self.version, self.subversion)
        if self.version_ref != expected:
            raise ValueError(
                f"version_ref {self.version_ref!r} must equal {expected!r}"
            )
        return self


class StoredTranslationRecordV2(BaseModel):
    """One source record with nested versioned translation candidates."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: int
    part_id: int
    source_sha256: str
    source: str
    active_version: str | None = None
    versions: list[TranslationCandidate] = Field(default_factory=list)

    @field_validator("active_version")
    @classmethod
    def _active_version_shape(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return parse_version_ref(value).version_ref

    @model_validator(mode="after")
    def _validate_versions(self) -> "StoredTranslationRecordV2":
        seen: set[str] = set()
        for candidate in self.versions:
            if candidate.version_ref in seen:
                raise ValueError(
                    f"duplicate version_ref {candidate.version_ref!r} in record"
                )
            seen.add(candidate.version_ref)
        if self.active_version is not None and self.active_version not in seen:
            raise ValueError(
                f"active_version {self.active_version!r} is not present in versions"
            )
        return self


class TranslationStoreV2(BaseModel):
    """Primary nested translation state for booktx."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[2] = 2
    source_sha256: str = ""
    records: dict[str, StoredTranslationRecordV2] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_record_keys(self) -> "TranslationStoreV2":
        for record_id, record in self.records.items():
            expected = canonical_record_id(record.chunk_id, record.part_id)
            if record_id != expected:
                raise ValueError(
                    f"record key {record_id!r} must equal canonical id {expected!r}"
                )
        return self


class TranslationSubversionLedgerEntry(BaseModel):
    """One context-scoped subversion entry inside a major track."""

    model_config = ConfigDict(extra="forbid")

    version: int
    subversion: int
    version_ref: str
    context_sha256: str
    context_path: str = ".booktx/context.json"
    context_label: str | None = None
    created_at: str
    updated_at: str
    notes: str | None = None
    forced: bool = False

    @field_validator("version_ref")
    @classmethod
    def _subversion_version_ref_shape(cls, value: str) -> str:
        return parse_version_ref(value).version_ref

    @model_validator(mode="after")
    def _validate_version_ref(self) -> "TranslationSubversionLedgerEntry":
        expected = format_version_ref(self.version, self.subversion)
        if self.version_ref != expected:
            raise ValueError(
                f"version_ref {self.version_ref!r} must equal {expected!r}"
            )
        if not self.context_sha256:
            raise ValueError("context_sha256 must not be empty")
        return self


class TranslationTrackLedgerEntry(BaseModel):
    """One major version track storing stable identity and subversions."""

    model_config = ConfigDict(extra="forbid")

    version: int
    actor: str
    harness: str
    model: str
    label: str | None = None
    created_at: str
    updated_at: str
    subversions: dict[str, TranslationSubversionLedgerEntry] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def _validate_subversion_keys(self) -> "TranslationTrackLedgerEntry":
        for subversion_id, entry in self.subversions.items():
            if subversion_id != str(entry.subversion):
                raise ValueError(
                    f"subversion key {subversion_id!r} must equal {entry.subversion!r}"
                )
            if entry.version != self.version:
                raise ValueError(
                    f"subversion {entry.version_ref!r} does not match track {self.version}"
                )
        return self


class TranslationVersionLedger(BaseModel):
    """Project-wide ledger assigning meaning to translation versions."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    source_sha256: str = ""
    active_version: str | None = None
    tracks: dict[str, TranslationTrackLedgerEntry] = Field(default_factory=dict)

    @field_validator("active_version")
    @classmethod
    def _ledger_active_version_shape(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return parse_version_ref(value).version_ref

    @model_validator(mode="after")
    def _validate_track_keys(self) -> "TranslationVersionLedger":
        for track_id, entry in self.tracks.items():
            if track_id != str(entry.version):
                raise ValueError(
                    f"track key {track_id!r} must equal {entry.version!r}"
                )
        return self


class TranslationIdentity(BaseModel):
    """Stored defaults for new translation-version work."""

    model_config = ConfigDict(extra="forbid")

    actor: str
    harness: str
    model: str


class TranslationTaskRecord(BaseModel):
    """A record assigned to a CLI-created translation task."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Record id, e.g. 0001-000001")
    chunk_id: str = Field(..., description="Owning chunk id, e.g. 0001")
    source: str = Field(..., description="Source text with placeholders")
    protected_terms: list[str] = Field(default_factory=list)
    placeholders: list[Placeholder] = Field(default_factory=list)


class TranslationTask(BaseModel):
    """A persisted work item returned by ``booktx translate next``."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1)
    task_id: str
    unit: Literal["paragraph", "batch", "chunk", "chapter"]
    chapter_id: str = ""
    chapter_title: str = ""
    source_language: str
    target_language: str
    source_words: int = 0
    record_count: int = 0
    records: list[TranslationTaskRecord] = Field(default_factory=list)


class NamesFile(BaseModel):
    """The ``.booktx/names.json`` file holding manually protected terms."""

    model_config = ConfigDict(extra="forbid")

    protected_terms: list[str] = Field(default_factory=list)


class ProjectConfig(BaseModel):
    """The ``.booktx/config.toml`` content, exposed as a model.

    The TOML file mirrors these field names exactly so it stays human-editable.
    """

    model_config = ConfigDict(extra="forbid")

    source_language: str = Field(default="en")
    target_language: str = Field(..., description="Target language code")
    source_file: str = Field(..., description="Filename inside source/")
    format: str = Field(..., description="Document format: 'markdown' or 'epub'")
    chunk_size: int = Field(default=50, ge=1, description="Max records per chunk")

    @field_validator("format")
    @classmethod
    def _format_ok(cls, v: str) -> str:
        v = (v or "").lower()
        if v not in {"markdown", "epub"}:
            raise ValueError("format must be 'markdown' or 'epub'")
        return v


class EpubSpanRef(BaseModel):
    """Stored span-to-EPUB-block mapping for the migrated EPUB pipeline."""

    model_config = ConfigDict(extra="forbid")

    span_index: int
    block_id: str
    document_href: str
    spine_index: int | None = None
    tag_name: str
    source_text: str
    source_text_sha256: str
    source_char_start: int | None = None
    source_char_end: int | None = None
    placeholders: list[Placeholder] = Field(default_factory=list)
    protected_terms: list[str] = Field(default_factory=list)


class EpubNavigationRef(BaseModel):
    """Stored navigation metadata for EPUB chapter detection."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    href: str | None = None
    document_href: str | None = None
    fragment: str | None = None
    spine_index: int | None = None
    source_char_start: int | None = None
    source_byte_start: int | None = None
    level: int = 1
    parent_id: str | None = None
    order: int = 0
    children: list[str] = Field(default_factory=list)
    source: str = "nav"


class EpubTemplateData(BaseModel):
    """Typed payload stored in ``Manifest.template`` for EPUB v2 projects."""

    model_config = ConfigDict(extra="forbid")

    pipeline: str
    epub2text_schema: str
    text2epub_manifest: dict[str, Any] = Field(default_factory=dict)
    spans: list[EpubSpanRef] = Field(default_factory=list)
    navigation: list[EpubNavigationRef] = Field(default_factory=list)


class ManifestSource(BaseModel):
    """Metadata about the source document recorded in the manifest."""

    model_config = ConfigDict(extra="forbid")

    filename: str
    format: str
    source_language: str
    target_language: str
    sha256: str = Field(default="", description="Hex digest of source bytes")


class Manifest(BaseModel):
    """The ``.booktx/manifest.json`` content."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1)
    source: ManifestSource
    chunk_count: int = Field(default=0)
    record_count: int = Field(default=0)
    # Mapping of record id -> placeholder-preserving template location. For
    # markdown this is enough to rebuild; epub rebuilds walk spine docs.
    template: dict[str, Any] = Field(default_factory=dict)
