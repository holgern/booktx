"""Version-ledger identity, context hashing, and version resolution helpers."""

from __future__ import annotations

import getpass
import hashlib
import json
from dataclasses import dataclass

from booktx.config import (
    Project,
    _err,
    identity_path,
    load_identity,
    load_translation_version_ledger,
    project_source_sha256,
    write_translation_version_ledger,
)
from booktx.context import load_context
from booktx.io_utils import utc_timestamp
from booktx.models import (
    TranslationIdentity,
    TranslationSubversionLedgerEntry,
    TranslationTrackLedgerEntry,
    TranslationVersionLedger,
)
from booktx.record_refs import format_version_ref, parse_version_ref

__all__ = [
    "VersionResolution",
    "canonical_json_sha256",
    "current_context_sha256",
    "default_identity",
    "fork_current_context",
    "lookup_version",
    "resolve_current_version",
    "resolve_identity",
    "select_active_version",
    "set_track_label",
]


@dataclass(slots=True)
class VersionResolution:
    """Resolved current translation version metadata."""

    ledger: TranslationVersionLedger
    identity: TranslationIdentity
    version_ref: str
    version: int
    subversion: int
    context_sha256: str
    created_track: bool
    created_subversion: bool


def canonical_json_sha256(data: object) -> str:
    """Hash canonical JSON with stable key ordering and separators."""
    payload = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def default_identity() -> TranslationIdentity:
    """Return deterministic local defaults when no identity file exists."""
    username = getpass.getuser() or "unknown"
    return TranslationIdentity(
        actor=f"user:{username}",
        harness="booktx",
        model="human",
    )


def resolve_identity(
    project: Project,
    *,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
) -> TranslationIdentity:
    """Resolve identity from explicit overrides, stored defaults, and fallbacks."""
    stored = load_identity(project)
    fallback = default_identity()
    return TranslationIdentity(
        actor=actor or (stored.actor if stored is not None else fallback.actor),
        harness=harness or (stored.harness if stored is not None else fallback.harness),
        model=model or (stored.model if stored is not None else fallback.model),
    )


def current_context_sha256(project: Project) -> str:
    """Return the canonical context hash for the current project context."""
    context = load_context(project)
    if context is None:
        raise _err(
            "missing_context",
            "translation context is missing. Run: booktx context init .",
        )
    return canonical_json_sha256(context.model_dump(mode="json", by_alias=True))


def lookup_version(
    ledger: TranslationVersionLedger,
    version_ref: str,
) -> tuple[TranslationTrackLedgerEntry, TranslationSubversionLedgerEntry]:
    """Resolve one dotted version reference against the ledger."""
    parsed = parse_version_ref(version_ref)
    track = ledger.tracks.get(str(parsed.version))
    if track is None:
        raise _err("unknown_version_ref", f"version {parsed.version_ref} not found")
    subversion = track.subversions.get(str(parsed.subversion))
    if subversion is None:
        raise _err("unknown_version_ref", f"version {parsed.version_ref} not found")
    return track, subversion


def resolve_current_version(
    project: Project,
    *,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    force_new_context: bool = False,
    context_label: str | None = None,
    note: str | None = None,
) -> VersionResolution:
    """Resolve and persist the current ledger version for a translation write."""
    ledger = load_translation_version_ledger(project)
    identity = resolve_identity(project, actor=actor, harness=harness, model=model)
    context_sha256 = current_context_sha256(project)
    now = utc_timestamp()
    created_track = False
    created_subversion = False

    track = next(
        (
            candidate
            for candidate in ledger.tracks.values()
            if candidate.actor == identity.actor
            and candidate.harness == identity.harness
            and candidate.model == identity.model
        ),
        None,
    )
    if track is None:
        next_version = (
            max((entry.version for entry in ledger.tracks.values()), default=0) + 1
        )
        track = TranslationTrackLedgerEntry(
            version=next_version,
            actor=identity.actor,
            harness=identity.harness,
            model=identity.model,
            created_at=now,
            updated_at=now,
        )
        ledger.tracks[str(track.version)] = track
        created_track = True
    else:
        track.updated_at = now

    subversion = None
    if not force_new_context:
        subversion = next(
            (
                entry
                for entry in track.subversions.values()
                if entry.context_sha256 == context_sha256
            ),
            None,
        )
    if subversion is None:
        next_subversion = (
            max((entry.subversion for entry in track.subversions.values()), default=0)
            + 1
        )
        version_ref = format_version_ref(track.version, next_subversion)
        subversion = TranslationSubversionLedgerEntry(
            version=track.version,
            subversion=next_subversion,
            version_ref=version_ref,
            context_sha256=context_sha256,
            context_path=str(
                identity_path(project)
                .parent.joinpath("context.json")
                .relative_to(project.root)
            ),
            context_label=context_label,
            created_at=now,
            updated_at=now,
            notes=note,
            forced=force_new_context,
        )
        track.subversions[str(subversion.subversion)] = subversion
        created_subversion = True
    else:
        subversion.updated_at = now
        if context_label is not None:
            subversion.context_label = context_label
        if note is not None:
            subversion.notes = note

    version_ref = subversion.version_ref
    ledger.source_sha256 = project_source_sha256(project)
    ledger.active_version = version_ref
    write_translation_version_ledger(project, ledger)
    return VersionResolution(
        ledger=ledger,
        identity=identity,
        version_ref=version_ref,
        version=track.version,
        subversion=subversion.subversion,
        context_sha256=context_sha256,
        created_track=created_track,
        created_subversion=created_subversion,
    )


def select_active_version(
    project: Project, version_ref: str
) -> TranslationVersionLedger:
    """Select the project-wide active version in the ledger."""
    ledger = load_translation_version_ledger(project)
    lookup_version(ledger, version_ref)
    ledger.active_version = parse_version_ref(version_ref).version_ref
    write_translation_version_ledger(project, ledger)
    return ledger


def set_track_label(
    project: Project, major_version: int, label: str
) -> TranslationVersionLedger:
    """Set the label for one major track."""
    ledger = load_translation_version_ledger(project)
    track = ledger.tracks.get(str(major_version))
    if track is None:
        raise _err("unknown_track", f"track {major_version} not found")
    track.label = label
    write_translation_version_ledger(project, ledger)
    return ledger


def fork_current_context(
    project: Project,
    *,
    note: str | None = None,
    context_label: str | None = None,
) -> VersionResolution:
    """Force a new subversion even when the current context hash is unchanged."""
    return resolve_current_version(
        project,
        force_new_context=True,
        context_label=context_label,
        note=note,
    )
