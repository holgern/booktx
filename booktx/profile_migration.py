"""Legacy single-layout -> translation-profile migration.

This module quarantines the compatibility surface that migrates an old
single-layout project (all state under ``.booktx/``) into the profile layout
(shared source state under ``.booktx/``, mutable translation state under
``translations/<profile>/``).

The migration is staged and uses an explicit :class:`ProfileMigrationPlan` so
dry-run output, preflight validation, and tests share one typed structure.
CLI identity overrides (``--model``/``--actor``/``--harness``) are honored over
any legacy ``.booktx/identity.json``.
"""

# ruff: noqa: E501

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from booktx.config import (
    Project,
    _booktx_dir,
    _default_output_filename,
    _err,
    _is_legacy_layout,
    _is_profile_layout,
    _legacy_config_path,
    _legacy_manifest_path,
    _profile_context_json_path,
    _profile_context_md_path,
    _profile_identity_path,
    _profile_ingest_dir,
    _profile_ledger_path,
    _profile_output_dir,
    _profile_reports_dir,
    _profile_store_path,
    _profile_tasks_dir,
    _profile_translated_dir,
    _source_manifest_path,
    _write_source_config,
    load_identity,
    load_project,
    profile_dir,
    source_config_path,
    validate_profile_name,
    write_profile_config,
    write_profile_root_marker,
    write_profile_state,
)
from booktx.epub_manifest import sha256_path
from booktx.io_utils import write_json_model_atomic, write_json_text_atomic
from booktx.models import (
    ProfileConfig,
    ProfileIdentityConfig,
    ProfileState,
    SourceConfig,
    TranslationIdentity,
    TranslationTask,
)

__all__ = [
    "MigrationMove",
    "ProfileMigrationPlan",
    "build_profile_migration_plan",
    "migrate_current_project",
]


@dataclass(frozen=True, slots=True)
class MigrationMove:
    """One file or directory relocation performed during profile migration."""

    source: Path
    destination: Path
    kind: Literal["file", "dir", "either"] = "either"


@dataclass(frozen=True, slots=True)
class ProfileMigrationPlan:
    """Resolved, validated plan for migrating a legacy project to a profile.

    Captures the final source config, profile config, authoritative
    identity (honoring CLI overrides), and the ordered list of moves. The
    move list is typed so dry-run output, preflight validation, and tests
    share one explicit structure instead of ad-hoc ``(source, dest)`` tuples.
    """

    project_root: Path
    profile_name: str
    source_config: SourceConfig
    profile_config: ProfileConfig
    identity: TranslationIdentity
    moves: list[MigrationMove]


def build_profile_migration_plan(
    project_root: Path,
    profile_name: str,
    legacy_project: Project,
    *,
    target_language: str,
    target_locale: str | None,
    actor: str | None,
    harness: str | None,
    model: str | None,
) -> ProfileMigrationPlan:
    """Build the resolved migration plan without writing anything."""
    legacy_identity = load_identity(legacy_project)
    identity_cfg = ProfileIdentityConfig(
        actor=actor
        or (legacy_identity.actor if legacy_identity is not None else "user:unknown"),
        harness=harness
        or (legacy_identity.harness if legacy_identity is not None else "booktx"),
        model=model
        or (legacy_identity.model if legacy_identity is not None else "human"),
    )
    source_cfg = SourceConfig(
        source_language=legacy_project.config.source_language,
        source_file=legacy_project.config.source_file,
        format=legacy_project.config.format,
        chunk_size=legacy_project.config.chunk_size,
    )
    profile_cfg = ProfileConfig(
        profile=profile_name,
        source_language=source_cfg.source_language,
        target_language=target_language,
        target_locale=target_locale or target_language,
        output_filename=_default_output_filename(source_cfg, target_language),
        identity=identity_cfg,
    )
    identity = TranslationIdentity(
        actor=identity_cfg.actor,
        harness=identity_cfg.harness,
        model=identity_cfg.model,
    )
    moves = [
        MigrationMove(
            _legacy_manifest_path(project_root), _source_manifest_path(project_root)
        ),
        MigrationMove(
            _booktx_dir(project_root) / "context.json",
            _profile_context_json_path(project_root, profile_name),
        ),
        MigrationMove(
            _booktx_dir(project_root) / "context.md",
            _profile_context_md_path(project_root, profile_name),
        ),
        MigrationMove(
            _booktx_dir(project_root) / "identity.json",
            _profile_identity_path(project_root, profile_name),
        ),
        MigrationMove(
            _booktx_dir(project_root) / "translation-store.json",
            _profile_store_path(project_root, profile_name),
        ),
        MigrationMove(
            _booktx_dir(project_root) / "translation-version-ledger.json",
            _profile_ledger_path(project_root, profile_name),
        ),
        MigrationMove(
            _booktx_dir(project_root) / "tasks",
            _profile_tasks_dir(project_root, profile_name),
            kind="dir",
        ),
        MigrationMove(
            _booktx_dir(project_root) / "ingest",
            _profile_ingest_dir(project_root, profile_name),
            kind="dir",
        ),
        MigrationMove(
            _booktx_dir(project_root) / "translated",
            _profile_translated_dir(project_root, profile_name),
            kind="dir",
        ),
        MigrationMove(
            _booktx_dir(project_root) / "reports",
            _profile_reports_dir(project_root, profile_name),
            kind="dir",
        ),
        MigrationMove(
            project_root / "output",
            _profile_output_dir(project_root, profile_name),
            kind="dir",
        ),
    ]
    return ProfileMigrationPlan(
        project_root=project_root,
        profile_name=profile_name,
        source_config=source_cfg,
        profile_config=profile_cfg,
        identity=identity,
        moves=moves,
    )


def _migration_manifest_entries(
    root: Path,
    moves: list[MigrationMove],
) -> list[dict[str, object]]:
    root_resolved = root.resolve()

    def _rel(path: Path) -> str:
        try:
            return str(path.resolve().relative_to(root_resolved))
        except ValueError:
            return str(path)

    entries: list[dict[str, object]] = []
    for move in moves:
        src = move.source
        entry: dict[str, object] = {
            "source": _rel(src),
            "destination": _rel(move.destination),
            "exists": src.exists(),
        }
        if src.is_file():
            entry["sha256"] = sha256_path(src)
        elif src.is_dir():
            entry["entry_count"] = sum(1 for _ in src.rglob("*"))
        entries.append(entry)
    return entries


def _validate_migration_destinations(
    project_root: Path,
    profile_name: str,
    moves: list[MigrationMove],
) -> None:
    """Preflight-check that moves only target the profile tree or source manifest.

    Migration replaces existing destinations, so guard the mutable surface
    explicitly: no destination may live outside
    ``translations/<profile>/`` except the shared ``.booktx/source-manifest.json``.
    """
    profile_root_resolved = profile_dir(project_root, profile_name).resolve()
    source_manifest_resolved = _source_manifest_path(project_root).resolve()
    for move in moves:
        dst_resolved = move.destination.resolve()
        if dst_resolved == source_manifest_resolved:
            continue
        if profile_root_resolved not in dst_resolved.parents:
            raise _err(
                "migration_destination_outside_profile",
                "migration destination is outside the translation profile tree: "
                f"{move.destination}",
            )


def _write_migration_manifest(root: Path, moves: list[MigrationMove]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest_path = _booktx_dir(root) / "migrations" / f"{stamp}-profile-migration.json"
    payload = {
        "version": 1,
        "timestamp": stamp,
        "moves": _migration_manifest_entries(root, moves),
    }
    write_json_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, ensure_ascii=False),
    )
    return manifest_path


def migrate_current_project(
    root: Path | str,
    profile_name: str,
    *,
    target_language: str | None = None,
    target_locale: str | None = None,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    select: bool = False,
    dry_run: bool = False,
) -> dict[str, object]:
    validate_profile_name(profile_name)
    project_root = Path(root).expanduser().resolve()
    if _is_profile_layout(project_root):
        raise _err(
            "already_profile_project",
            "project already uses translation profiles; use `booktx profile create` instead",
        )
    if not _is_legacy_layout(project_root):
        raise _err(
            "legacy_project_required",
            "project does not use the legacy single-layout format",
        )

    legacy_project = load_project(project_root)
    legacy_target = target_language or legacy_project.config.target_language
    if not legacy_target:
        raise _err(
            "profile_config_invalid",
            "legacy project has no target language; pass --target to migrate-current",
        )
    profile_root = profile_dir(project_root, profile_name)
    if profile_root.exists() and any(profile_root.iterdir()):
        raise _err(
            "migration_target_exists",
            f"migration target already exists and is not empty: {profile_root}",
        )

    plan = build_profile_migration_plan(
        project_root,
        profile_name,
        legacy_project,
        target_language=legacy_target,
        target_locale=target_locale,
        actor=actor,
        harness=harness,
        model=model,
    )
    profile_cfg = plan.profile_config

    summary = {
        "project": str(project_root),
        "profile": profile_name,
        "target_language": legacy_target,
        "target_locale": profile_cfg.target_locale or legacy_target,
        "dry_run": dry_run,
        "moves": [
            {
                "source": str(move.source.relative_to(project_root)),
                "destination": str(move.destination.relative_to(project_root)),
                "exists": move.source.exists(),
            }
            for move in plan.moves
        ],
    }
    if dry_run:
        return summary

    profile_root.mkdir(parents=True, exist_ok=True)
    # Create every standard profile-local directory up front so a migration
    # from a legacy project that lacked some of these dirs still yields a
    # complete profile tree.
    for path in (
        _profile_tasks_dir(project_root, profile_name),
        _profile_ingest_dir(project_root, profile_name),
        _profile_translated_dir(project_root, profile_name),
        _profile_reports_dir(project_root, profile_name),
        _profile_output_dir(project_root, profile_name),
    ):
        path.mkdir(parents=True, exist_ok=True)

    # Preflight: no destination may escape the profile tree or source manifest.
    _validate_migration_destinations(project_root, profile_name, plan.moves)

    # Record the migration plan (with source checksums) before mutating.
    migration_manifest = _write_migration_manifest(project_root, plan.moves)

    # Move mutable legacy state first. If this phase raises, we have not yet
    # written the new profile config/identity or removed the legacy
    # ``config.toml``, so the project can be retried as a legacy project.
    for move in plan.moves:
        src = move.source
        dst = move.destination
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        shutil.move(str(src), str(dst))

    # Write final config/identity/state only after all moves succeeded. The
    # plan's identity is authoritative and honors CLI overrides over the
    # moved legacy identity (P0 override-overwrite fix).
    _write_source_config(source_config_path(project_root), plan.source_config)
    write_profile_config(project_root, plan.profile_config)
    write_json_model_atomic(
        _profile_identity_path(project_root, profile_name),
        plan.identity,
    )
    write_profile_root_marker(
        project_root,
        profile_name,
        profile_config=plan.profile_config,
    )
    if select:
        write_profile_state(project_root, ProfileState(active_profile=profile_name))

    # Remove the legacy config only after every required write/move has
    # succeeded, so a failure never leaves a project that is neither legacy
    # nor profile layout.
    legacy_config = _legacy_config_path(project_root)
    if legacy_config.exists():
        legacy_config.unlink()

    tasks_root = _profile_tasks_dir(project_root, profile_name)
    if tasks_root.is_dir():
        for task_path in tasks_root.glob("*.json"):
            task = TranslationTask.model_validate_json(task_path.read_text("utf-8"))
            if not task.profile:
                task.profile = profile_name
            if not task.target_locale:
                task.target_locale = profile_cfg.target_locale or legacy_target
            write_json_model_atomic(task_path, task)

    summary["migration_manifest"] = str(migration_manifest.relative_to(project_root))
    return summary
