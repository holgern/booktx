"""Pydantic data models for the booktx translation contract.

This module defines the JSON shapes that cross the boundary between booktx and
the translating coding agent:

- Source chunk  -> ``chunks/NNNN.json``  (written by ``booktx extract``)
- Translated chunk -> ``translated/NNNN.json`` (written by the agent)

Both must round-trip through JSON with stable field names and ordering.
"""

# ruff: noqa: E501

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from booktx.record_refs import (
    canonical_record_id,
    format_version_ref,
    parse_version_ref,
)

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
    "StatusTotals",
    "TranslationTodoChapter",
    "TranslationTodo",
    "NamesFile",
    "SourceConfig",
    "ProfileIdentityConfig",
    "ProfileConfig",
    "ProfileState",
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
    version: str | None = Field(
        default=None,
        description="Accepted translation version ref, e.g. 1.1",
    )
    target: str = Field(..., description="Translated text")

    @field_validator("version")
    @classmethod
    def _version_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return parse_version_ref(value).version_ref

    @model_serializer(mode="wrap")
    def _omit_none_version(self, handler: Any) -> dict[str, Any]:
        payload: dict[str, Any] = handler(self)
        if payload.get("version") is None:
            payload.pop("version", None)
        return payload


class Chunk(BaseModel):
    """A source chunk file (``chunks/NNNN.json``)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=2, description="Source chunk schema version")
    chunk_id: str = Field(..., description="Chunk id, e.g. 0001")
    chunk_size: int = Field(
        default=50, ge=1, description="Configured max records per chunk"
    )
    record_id_scheme: str = Field(
        default="chunk-local:v1",
        description="Record id scheme used by this extraction",
    )
    source_language: str = Field(..., description="BCP-47-ish source code, e.g. en")
    target_language: str = Field(
        default="",
        description="BCP-47-ish target code kept for legacy compatibility",
    )
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
    baseline_ref: str | None = None
    baseline_sha256: str | None = None
    context_view_sha256: str | None = None
    context_view_path: str | None = None
    context_notes_scope: str | None = None
    context_target_chapter_id: str | None = None
    context_notes_through_chapter_id: str | None = None
    target: str
    status: str = "accepted"
    created_at: str
    updated_at: str
    reviewed_at: str | None = None
    reviewed_by: str | None = None
    review_note: str | None = None

    @field_validator("version_ref", "baseline_ref")
    @classmethod
    def _version_ref_shape(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return parse_version_ref(value).version_ref

    @model_validator(mode="after")
    def _version_ref_matches_fields(self) -> TranslationCandidate:
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
    def _validate_versions(self) -> StoredTranslationRecordV2:
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
    def _validate_record_keys(self) -> TranslationStoreV2:
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
    baseline_sha256: str | None = None
    baseline_path: str | None = None
    legacy_full_context_sha256: str | None = None
    legacy_full_context_path: str | None = None
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
    def _validate_version_ref(self) -> TranslationSubversionLedgerEntry:
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
    def _validate_subversion_keys(self) -> TranslationTrackLedgerEntry:
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
    def _validate_track_keys(self) -> TranslationVersionLedger:
        for track_id, entry in self.tracks.items():
            if track_id != str(entry.version):
                raise ValueError(f"track key {track_id!r} must equal {entry.version!r}")
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
    profile: str = ""
    source_language: str
    target_language: str
    target_locale: str = ""
    translation_version: str | None = Field(
        default=None,
        description="Active translation version ref when the task was created",
    )
    baseline_ref: str | None = Field(
        default=None,
        description="Baseline version ref when the task was created",
    )
    baseline_sha256: str | None = Field(
        default=None,
        description="Baseline context hash when the task was created",
    )
    context_sha256: str | None = Field(
        default=None,
        description="Context hash when the task was created",
    )
    context_view_sha256: str | None = Field(
        default=None,
        description="Task-scoped effective context view hash",
    )
    context_view_path: str | None = Field(
        default=None,
        description="Project-relative path to the immutable task context snapshot",
    )
    context_notes_scope: str | None = Field(
        default=None,
        description="How chapter notes were selected for this task context",
    )
    context_target_chapter_id: str | None = Field(
        default=None,
        description="Target chapter id used to compose the task context",
    )
    context_notes_through_chapter_id: str | None = Field(
        default=None,
        description="Highest prior chapter note included in the task context",
    )
    source_sha256: str | None = Field(
        default=None,
        description="Project source hash when the task was created",
    )
    profile_config_sha256: str | None = Field(
        default=None,
        description="Canonical profile-config hash when the task was created",
    )
    source_config_sha256: str | None = Field(
        default=None,
        description="Canonical source-config hash when the task was created",
    )
    source_words: int = 0
    record_count: int = 0
    requested_max_words: int | None = None
    todo_id: str | None = None
    records: list[TranslationTaskRecord] = Field(default_factory=list)


class TranslationTodoChapter(BaseModel):
    """One chapter selected for a bounded agent translation run.

    Carries only coverage/state at the moment the todo was created; it never
    embeds source text. Source text belongs in per-task ``*.source.block.txt``.
    """

    model_config = ConfigDict(extra="forbid")

    chapter_id: str
    title: str = ""
    status: str
    records_total: int
    records_translated_at_start: int
    records_remaining_at_start: int
    source_words_remaining_at_start: int
    pending_chunk_ids: list[str] = Field(default_factory=list)


class StatusTotals(BaseModel):
    """Aggregate translation coverage totals.

    Used by ``status --json`` and persisted in ``TranslationTodo.start_totals``.
    Keep this model in ``models.py`` so durable todo validation never depends on
    a late Pydantic forward-reference rebuild.
    """

    model_config = ConfigDict(extra="forbid")

    source_words: int = 0
    translated_words: int = 0
    remaining_words: int = 0
    records_total: int = 0
    records_translated: int = 0
    records_remaining: int = 0
    chunks_total: int = 0
    chunks_complete: int = 0
    chunks_partial: int = 0
    chunks_pending: int = 0
    chapters_total: int = 0
    chapters_complete: int = 0
    chapters_partial: int = 0
    chapters_pending: int = 0
    invalid_translation_files: int = 0
    stale_translation_files: int = 0


class TranslationTodo(BaseModel):
    """A durable run-control artifact for a bounded multi-chapter agent run.

    Written by ``booktx translate todo-next`` under
    ``translations/<profile>/todos/<todo_id>.{json,md}``. This is NOT a
    translation submission: it tells the agent how many chapters to complete,
    the per-task word budget, and the stop conditions.
    """

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    todo_id: str
    profile: str
    target_language: str
    target_locale: str = ""
    chapters_requested: int
    batch_words: int
    max_run_words: int | None = None
    include_current: bool = True
    created_at: str
    baseline_ref: str | None = None
    baseline_sha256: str | None = None
    context_sha256: str | None = None
    source_sha256: str | None = None
    start_totals: StatusTotals
    chapters: list[TranslationTodoChapter] = Field(default_factory=list)


class NamesFile(BaseModel):
    """The ``.booktx/names.json`` file holding manually protected terms."""

    model_config = ConfigDict(extra="forbid")

    protected_terms: list[str] = Field(default_factory=list)


class SourceConfig(BaseModel):
    """Source-only config stored in ``.booktx/source-config.toml``."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    source_language: str = Field(default="en")
    source_file: str = Field(default="", description="Filename inside source/")
    format: str = Field(default="markdown", description="Document format")
    chunk_size: int = Field(default=50, ge=1, description="Max records per chunk")

    @field_validator("format")
    @classmethod
    def _source_format_ok(cls, v: str) -> str:
        v = (v or "").lower()
        if v not in {"markdown", "epub"}:
            raise ValueError("format must be 'markdown' or 'epub'")
        return v


class ProfileIdentityConfig(BaseModel):
    """Initial identity defaults captured when a profile is created.

    This is **not** the live identity. After creation, the authoritative
    identity lives in ``translations/<profile>/identity.json`` and is updated
    by ``booktx model set`` / ``actor set`` / ``harness set``. Profile list
    and show render the resolved ``identity.json`` value, not this field.
    """

    model_config = ConfigDict(extra="forbid")

    actor: str = "user:unknown"
    harness: str = "booktx"
    model: str = "human"


class ProfileConfig(BaseModel):
    """Per-profile translation config stored in ``translations/<profile>/config.toml``."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    kind: Literal["translation", "pass-through"] = Field(
        default="translation",
        description="Profile kind: 'translation' or 'pass-through' fixture.",
    )
    profile: str
    source_language: str = "en"
    target_language: str
    target_locale: str | None = None
    output_filename: str | None = None
    identity: ProfileIdentityConfig = Field(default_factory=ProfileIdentityConfig)
    """Initial identity defaults; the live identity is identity.json (see above)."""


class ProfileState(BaseModel):
    """Active-profile selector stored in ``.booktx/profile-state.json``."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    active_profile: str | None = None


class ProjectConfig(BaseModel):
    """Effective runtime config, or the legacy ``.booktx/config.toml`` shape.

    The TOML file mirrors these field names exactly so it stays human-editable.
    """

    model_config = ConfigDict(extra="forbid")

    source_language: str = Field(default="en")
    target_language: str = Field(default="", description="Target language code")
    target_locale: str | None = Field(default=None, description="Target locale code")
    output_filename: str | None = Field(
        default=None, description="Output filename override"
    )
    source_file: str = Field(default="", description="Filename inside source/")
    format: str = Field(
        default="markdown", description="Document format: 'markdown' or 'epub'"
    )
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
    target_language: str = ""
    sha256: str = Field(default="", description="Hex digest of source bytes")


class Manifest(BaseModel):
    """The ``.booktx/manifest.json`` content."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1)
    source: ManifestSource
    chunk_count: int = Field(default=0)
    record_count: int = Field(default=0)
    chunk_size: int = Field(default=50, ge=1)
    record_id_scheme: str = Field(default="chunk-local:v1")
    segmenter: dict[str, Any] = Field(default_factory=dict)
    names_sha256: str = Field(default="")
    # Mapping of record id -> placeholder-preserving template location. For
    # markdown this is enough to rebuild; epub rebuilds walk spine docs.
    template: dict[str, Any] = Field(default_factory=dict)
