"""Pydantic data models for the booktx translation contract.

This module defines the JSON shapes that cross the boundary between booktx and
the translating coding agent:

- Source chunk  -> ``chunks/NNNN.json``  (written by ``booktx extract``)
- Translated chunk -> ``translated/NNNN.json`` (written by the agent)

Both must round-trip through JSON with stable field names and ordering.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "Placeholder",
    "Record",
    "TranslatedRecord",
    "Chunk",
    "TranslatedChunk",
    "NamesFile",
    "ProjectConfig",
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
    kind: str = Field("tag", description="Placeholder kind: 'name' or 'tag'")


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
