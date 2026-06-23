"""Helpers for canonical record and version references."""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "RecordRef",
    "VersionRef",
    "canonical_record_id",
    "format_version_ref",
    "parse_record_ref",
    "parse_version_ref",
    "resolve_record_range",
]

_RECORD_REF_RE = re.compile(r"^(?P<chunk>\d+)(?P<sep>[@-])(?P<part>\d+)$")
_VERSION_REF_RE = re.compile(r"^(?P<version>[1-9]\d*)\.(?P<subversion>[1-9]\d*)$")


def canonical_record_id(chunk_id: int, part_id: int) -> str:
    """Return the canonical padded record id for integer ids."""
    if chunk_id <= 0 or part_id <= 0:
        raise ValueError("record references must use positive integer ids")
    return f"{chunk_id:04d}-{part_id:06d}"


def format_version_ref(version: int, subversion: int) -> str:
    """Return the canonical dotted version reference."""
    if version <= 0 or subversion <= 0:
        raise ValueError("version references must use positive integer ids")
    return f"{version}.{subversion}"


@dataclass(frozen=True, order=True, slots=True)
class RecordRef:
    """Parsed record reference with stable canonical rendering."""

    chunk_id: int
    part_id: int

    def __post_init__(self) -> None:
        canonical_record_id(self.chunk_id, self.part_id)

    @property
    def canonical_id(self) -> str:
        return canonical_record_id(self.chunk_id, self.part_id)

    @property
    def compact_ref(self) -> str:
        return f"{self.chunk_id}@{self.part_id}"

    def __str__(self) -> str:
        return self.canonical_id


@dataclass(frozen=True, order=True, slots=True)
class VersionRef:
    """Parsed dotted version reference with numeric ordering."""

    version: int
    subversion: int

    def __post_init__(self) -> None:
        format_version_ref(self.version, self.subversion)

    @property
    def version_ref(self) -> str:
        return format_version_ref(self.version, self.subversion)

    def __str__(self) -> str:
        return self.version_ref


def parse_record_ref(value: str) -> RecordRef:
    """Parse a record reference into canonical integer parts."""
    text = value.strip()
    match = _RECORD_REF_RE.fullmatch(text)
    if match is None:
        raise ValueError(f"invalid record reference: {value!r}")
    return RecordRef(
        chunk_id=int(match.group("chunk")),
        part_id=int(match.group("part")),
    )


def parse_version_ref(value: str) -> VersionRef:
    """Parse and validate a dotted version reference."""
    text = value.strip()
    match = _VERSION_REF_RE.fullmatch(text)
    if match is None:
        raise ValueError(f"invalid version reference: {value!r}")
    return VersionRef(
        version=int(match.group("version")),
        subversion=int(match.group("subversion")),
    )


def resolve_record_range(
    value: str,
    *,
    ordered_record_ids: list[str],
    chapter_record_ids: dict[str, list[str]] | None = None,
) -> list[str]:
    """Resolve one record range selector against source reading order."""
    text = value.strip()
    if ".." in text:
        start_text, end_text = text.split("..", 1)
        start_id = parse_record_ref(start_text).canonical_id
        end_id = parse_record_ref(end_text).canonical_id
        try:
            start_index = ordered_record_ids.index(start_id)
            end_index = ordered_record_ids.index(end_id)
        except ValueError as exc:
            raise ValueError(f"unknown record in range: {value!r}") from exc
        if start_index > end_index:
            raise ValueError(f"range start must not come after range end: {value!r}")
        return ordered_record_ids[start_index : end_index + 1]
    if "+" in text:
        start_text, count_text = text.split("+", 1)
        start_id = parse_record_ref(start_text).canonical_id
        try:
            start_index = ordered_record_ids.index(start_id)
        except ValueError as exc:
            raise ValueError(f"unknown record in range: {value!r}") from exc
        try:
            count = int(count_text)
        except ValueError as exc:
            raise ValueError(f"invalid record count in range: {value!r}") from exc
        if count <= 0:
            raise ValueError(f"range count must be positive: {value!r}")
        return ordered_record_ids[start_index : start_index + count]
    if text.startswith("chunk:"):
        chunk_id = f"{int(text.split(':', 1)[1]):04d}"
        return [record_id for record_id in ordered_record_ids if record_id.startswith(f"{chunk_id}-")]
    if text.startswith("chapter:"):
        if chapter_record_ids is None:
            raise ValueError("chapter ranges require chapter context")
        chapter_id = f"{int(text.split(':', 1)[1]):04d}"
        return list(chapter_record_ids.get(chapter_id, []))
    return [parse_record_ref(text).canonical_id]
