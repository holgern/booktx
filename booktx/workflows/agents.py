"""Domain workflows for generated ``AGENTS.md`` harness instructions.

This module owns mode resolution and all filesystem decisions for the
``booktx agents`` command group. It reuses :func:`booktx.runtime.resolve_runtime`
and the existing explicit/active/sole profile resolution
(:func:`booktx.config.resolve_profile_name`) so a second, subtly different
profile resolver is never introduced.

The command layer renders project-relative paths in project-root mode and the
literal ``AGENTS.md`` in profile-root mode. The workflow results may contain
absolute :class:`~pathlib.Path` objects internally; the command layer is
responsible for never serializing them directly in profile-root output.

Mutations follow a deterministic, idempotent reconciliation sequence:

1. Resolve runtime and target profile.
2. Discover and classify every target, ancestor, and cleanup candidate without
   mutation.
3. Render the new file and validate invariants in memory.
4. Fail before mutation for target or ancestor conflicts; record malformed,
   unmanaged, wrong-mode, and symlink cleanup candidates as skips.
5. Refresh the profile marker atomically when required (project-root isolated).
6. Write the new target atomically.
7. Delete eligible conflicting managed files.
8. Report any remaining files on cleanup failure; a rerun finishes it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from booktx.agents_md import (
    AGENTS_FILENAME,
    AgentMode,
    AgentsMdMetadata,
    AgentsMdSkippedPath,
    AgentsMdStatusEntry,
    AgentsMdSyncResult,
    delete_managed_agents_md,
    inspect_agents_md,
    render_agents_md,
    write_managed_agents_md,
)
from booktx.config import (
    list_profiles,
    load_profile_config,
    load_source_project,
    profile_dir,
    project_source_id_or_unavailable,
    resolve_profile_name,
    write_profile_root_marker,
)
from booktx.errors import BooktxError, _err
from booktx.runtime import RuntimeContext, resolve_runtime

__all__ = [
    "agents_status_workflow",
    "clean_agents_workflow",
    "write_agents_workflow",
]

_PROFILE_ROOT_MARKER_FILENAME = ".booktx-profile.json"


def _looks_like_profile_root(path: Path) -> bool:
    return (path / _PROFILE_ROOT_MARKER_FILENAME).is_file() and (
        path / "config.toml"
    ).is_file()


def _sanitized_profile_root_not_ready() -> BooktxError:
    """An error that never names or prints the parent project path."""
    return _err(
        "agents_profile_root_not_ready",
        "cannot prepare AGENTS.md from this profile root; "
        "run `booktx agents write . --mode isolated --profile PROFILE` "
        "from the project root",
    )


def _resolve_runtime_or_sanitized(
    project_dir: Path, profile: str | None
) -> RuntimeContext:
    """Resolve runtime; sanitize profile-root resolution failures."""
    resolved = project_dir.expanduser().resolve()
    profile_root_present = _looks_like_profile_root(resolved)
    try:
        return resolve_runtime(project_dir, profile=profile, require_profile=False)
    except BooktxError:
        if profile_root_present:
            raise _sanitized_profile_root_not_ready() from None
        raise


def _current_source_id(root: Path) -> str:
    return project_source_id_or_unavailable(load_source_project(root))


def _target_locale(root: Path, profile_name: str) -> str:
    cfg = load_profile_config(load_source_project(root), profile_name)
    return cfg.target_locale or cfg.target_language


def _reject_ancestor_conflict(root: Path, *, sanitize: bool) -> None:
    """Block isolated preparation when the project-root AGENTS.md is incompatible."""
    ancestor = root / AGENTS_FILENAME
    state = inspect_agents_md(ancestor).state
    if state in ("unmanaged", "managed-malformed", "symlink"):
        if sanitize:
            raise _err(
                "agents_unmanaged_ancestor",
                "isolated AGENTS.md preparation is blocked by an incompatible "
                "project-root AGENTS.md; run `booktx agents write . --mode "
                "isolated --profile PROFILE` from the project root",
            )
        raise _err(
            "agents_unmanaged_ancestor",
            f"project-root AGENTS.md is {state}; resolve it before preparing "
            "isolated profile instructions",
        )


def _safe_delete(
    path: Path,
    *,
    expected_mode: AgentMode | None,
    deleted: list[Path],
    skipped: list[AgentsMdSkippedPath],
) -> None:
    """Delete ``path`` when it is an eligible managed file; otherwise record a skip.

    ``expected_mode=None`` is write reconciliation (any managed-valid file).
    A concrete ``expected_mode`` is the ``clean`` mode filter.
    """
    try:
        if delete_managed_agents_md(path, expected_mode=expected_mode):
            deleted.append(path)
    except BooktxError as exc:
        skipped.append(AgentsMdSkippedPath(path=path, reason=str(exc)))


# --------------------------------------------------------------------------
# write
# --------------------------------------------------------------------------


def write_agents_workflow(
    project_dir: Path,
    *,
    mode: AgentMode,
    profile: str | None = None,
    replace_unmanaged: bool = False,
) -> AgentsMdSyncResult:
    """Prepare the managed ``AGENTS.md`` file(s) for ``mode``."""
    runtime = _resolve_runtime_or_sanitized(project_dir, profile)
    if runtime.mode.kind == "profile-root":
        return _write_profile_root(
            runtime, mode=mode, replace_unmanaged=replace_unmanaged
        )
    return _write_project_root(
        runtime, mode=mode, profile=profile, replace_unmanaged=replace_unmanaged
    )


def _write_project_root(
    runtime: RuntimeContext,
    *,
    mode: AgentMode,
    profile: str | None,
    replace_unmanaged: bool,
) -> AgentsMdSyncResult:
    project = runtime.project
    root = project.root
    source_id = _current_source_id(root)

    if mode == "collaborative":
        target = root / AGENTS_FILENAME
        text = render_agents_md(mode="collaborative", profile=None, source_id=source_id)
        write_managed_agents_md(target, text, replace_unmanaged=replace_unmanaged)
        deleted: list[Path] = []
        skipped: list[AgentsMdSkippedPath] = []
        if project.layout_version == "profiles":
            for name in list_profiles(project):
                _safe_delete(
                    profile_dir(root, name) / AGENTS_FILENAME,
                    expected_mode=None,
                    deleted=deleted,
                    skipped=skipped,
                )
        return AgentsMdSyncResult(
            mode="collaborative",
            written=(target,),
            deleted=tuple(deleted),
            skipped=tuple(skipped),
        )

    # mode == isolated
    if project.layout_version == "legacy":
        raise _err(
            "agents_legacy_isolated_unsupported",
            "isolated AGENTS.md mode requires a profile-layout project; "
            "this project uses the legacy single layout",
        )
    target_profile = resolve_profile_name(project, profile, require_profile=False)
    if target_profile is None:
        raise _err(
            "agents_profile_required",
            "isolated AGENTS.md mode requires a target profile; "
            "pass --profile or select an active profile",
        )
    _reject_ancestor_conflict(root, sanitize=False)

    # Refresh the profile marker atomically before writing the target.
    write_profile_root_marker(root, target_profile)

    target = profile_dir(root, target_profile) / AGENTS_FILENAME
    text = render_agents_md(
        mode="isolated",
        profile=target_profile,
        source_id=source_id,
        target_locale=_target_locale(root, target_profile),
    )
    write_managed_agents_md(target, text, replace_unmanaged=replace_unmanaged)

    deleted = []
    skipped = []
    # Reconcile the root file (managed-valid only) and sibling profiles.
    _safe_delete(
        root / AGENTS_FILENAME,
        expected_mode=None,
        deleted=deleted,
        skipped=skipped,
    )
    for name in list_profiles(project):
        if name == target_profile:
            continue
        _safe_delete(
            profile_dir(root, name) / AGENTS_FILENAME,
            expected_mode=None,
            deleted=deleted,
            skipped=skipped,
        )
    return AgentsMdSyncResult(
        mode="isolated",
        written=(target,),
        deleted=tuple(deleted),
        skipped=tuple(skipped),
        profile=target_profile,
    )


def _write_profile_root(
    runtime: RuntimeContext,
    *,
    mode: AgentMode,
    replace_unmanaged: bool,
) -> AgentsMdSyncResult:
    if mode == "collaborative":
        raise _err(
            "agents_collaborative_requires_project_root",
            "collaborative AGENTS.md mode must be prepared from the project root",
        )
    root = runtime.project.root
    _reject_ancestor_conflict(root, sanitize=True)
    profile_root = runtime.mode.profile_root
    target_profile = runtime.mode.profile_name
    assert profile_root is not None  # noqa: S101 - invariant of profile-root mode
    assert target_profile is not None  # noqa: S101 - invariant of profile-root mode
    source_id = _current_source_id(root)
    text = render_agents_md(
        mode="isolated",
        profile=target_profile,
        source_id=source_id,
        target_locale=_target_locale(root, target_profile),
    )
    target = profile_root / AGENTS_FILENAME
    write_managed_agents_md(target, text, replace_unmanaged=replace_unmanaged)
    # Profile-root writes never touch anything outside the profile root.
    return AgentsMdSyncResult(
        mode="isolated",
        written=(target,),
        deleted=(),
        skipped=(),
        profile=target_profile,
    )


# --------------------------------------------------------------------------
# clean
# --------------------------------------------------------------------------


def _validate_clean_options(
    mode: AgentMode | Literal["all"], profile: str | None
) -> None:
    if mode == "all" and profile is not None:
        raise _err(
            "agents_clean_all_profile_ambiguous",
            "--mode all is project-wide; do not combine it with --profile",
        )
    if mode == "collaborative" and profile is not None:
        raise _err(
            "agents_clean_collaborative_profile_unsupported",
            "--mode collaborative cleanup does not accept --profile",
        )


def clean_agents_workflow(
    project_dir: Path,
    *,
    mode: AgentMode | Literal["all"],
    profile: str | None = None,
) -> AgentsMdSyncResult:
    """Delete managed ``AGENTS.md`` files according to the clean matrix."""
    _validate_clean_options(mode, profile)
    runtime = _resolve_runtime_or_sanitized(project_dir, profile)
    if runtime.mode.kind == "profile-root":
        return _clean_profile_root(runtime, mode=mode, profile=profile)
    return _clean_project_root(runtime, mode=mode, profile=profile)


def _clean_project_root(
    runtime: RuntimeContext,
    *,
    mode: AgentMode | Literal["all"],
    profile: str | None,
) -> AgentsMdSyncResult:
    project = runtime.project
    root = project.root
    deleted: list[Path] = []
    skipped: list[AgentsMdSkippedPath] = []

    if mode == "collaborative":
        _safe_delete(
            root / AGENTS_FILENAME,
            expected_mode="collaborative",
            deleted=deleted,
            skipped=skipped,
        )
        return AgentsMdSyncResult(
            mode="collaborative",
            written=(),
            deleted=tuple(deleted),
            skipped=tuple(skipped),
        )

    if mode == "all":
        _safe_delete(
            root / AGENTS_FILENAME,
            expected_mode=None,
            deleted=deleted,
            skipped=skipped,
        )
        if project.layout_version == "profiles":
            for name in list_profiles(project):
                _safe_delete(
                    profile_dir(root, name) / AGENTS_FILENAME,
                    expected_mode=None,
                    deleted=deleted,
                    skipped=skipped,
                )
        return AgentsMdSyncResult(
            mode="all",
            written=(),
            deleted=tuple(deleted),
            skipped=tuple(skipped),
        )

    # mode == isolated
    if project.layout_version == "legacy":
        raise _err(
            "agents_legacy_isolated_unsupported",
            "isolated AGENTS.md cleanup requires a profile-layout project",
        )
    target_profile = resolve_profile_name(project, profile, require_profile=False)
    if target_profile is None:
        raise _err(
            "agents_profile_required",
            "isolated AGENTS.md cleanup requires a target profile; "
            "pass --profile or select an active profile",
        )
    _safe_delete(
        profile_dir(root, target_profile) / AGENTS_FILENAME,
        expected_mode="isolated",
        deleted=deleted,
        skipped=skipped,
    )
    return AgentsMdSyncResult(
        mode="isolated",
        written=(),
        deleted=tuple(deleted),
        skipped=tuple(skipped),
        profile=target_profile,
    )


def _clean_profile_root(
    runtime: RuntimeContext,
    *,
    mode: AgentMode | Literal["all"],
    profile: str | None,
) -> AgentsMdSyncResult:
    if mode == "collaborative":
        raise _err(
            "agents_collaborative_requires_project_root",
            "collaborative AGENTS.md cleanup must be run from the project root",
        )
    if profile is not None:
        raise _err(
            "agents_profile_not_allowed_in_profile_root",
            "--profile is not valid inside a profile root; "
            "run `booktx agents clean . --mode isolated` instead",
        )
    profile_root = runtime.mode.profile_root
    assert profile_root is not None  # noqa: S101 - invariant of profile-root mode
    deleted: list[Path] = []
    skipped: list[AgentsMdSkippedPath] = []
    expected: AgentMode | None = "isolated" if mode == "isolated" else None
    _safe_delete(
        profile_root / AGENTS_FILENAME,
        expected_mode=expected,
        deleted=deleted,
        skipped=skipped,
    )
    return AgentsMdSyncResult(
        mode=mode,
        written=(),
        deleted=tuple(deleted),
        skipped=tuple(skipped),
        profile=runtime.mode.profile_name,
    )


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


def agents_status_workflow(
    project_dir: Path,
) -> tuple[AgentsMdStatusEntry, ...]:
    """Report the ownership state of every applicable ``AGENTS.md``.

    In profile-root mode, only the local ``AGENTS.md`` is reported; the project
    root and sibling profiles are never enumerated. Paths are absolute
    internally and rendered by the command layer.
    """
    runtime = resolve_runtime(project_dir, require_profile=False)
    root = runtime.project.root
    current_source_id = _current_source_id(root)

    def stale_for(metadata: AgentsMdMetadata | None) -> bool | None:
        if metadata is None:
            return None
        if current_source_id == "unavailable":
            return None
        return metadata.source_id != current_source_id

    if runtime.mode.kind == "profile-root":
        profile_root = runtime.mode.profile_root
        assert profile_root is not None  # noqa: S101 - invariant of profile-root mode
        local_path = profile_root / AGENTS_FILENAME
        inspection = inspect_agents_md(local_path)
        return (
            AgentsMdStatusEntry(
                path=local_path,
                scope="profile",
                profile=runtime.mode.profile_name,
                inspection=inspection,
                stale=stale_for(inspection.metadata),
            ),
        )

    entries: list[AgentsMdStatusEntry] = []
    root_path = root / AGENTS_FILENAME
    root_inspection = inspect_agents_md(root_path)
    entries.append(
        AgentsMdStatusEntry(
            path=root_path,
            scope="project",
            profile=None,
            inspection=root_inspection,
            stale=stale_for(root_inspection.metadata),
        )
    )
    if runtime.project.layout_version == "profiles":
        for name in list_profiles(runtime.project):
            local_path = profile_dir(root, name) / AGENTS_FILENAME
            inspection = inspect_agents_md(local_path)
            entries.append(
                AgentsMdStatusEntry(
                    path=local_path,
                    scope="profile",
                    profile=name,
                    inspection=inspection,
                    stale=stale_for(inspection.metadata),
                )
            )
    return tuple(entries)
