"""Runtime resolution for project-root and profile-root booktx commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from booktx.config import (
    _err,
    load_profile_config,
    load_profile_root_marker,
    load_project,
    profile_dir,
    project_source_id_or_unavailable,
    source_config_path,
)
from booktx.models import ProfileRootMarker

__all__ = [
    "RuntimeMode",
    "RuntimeContext",
    "find_project_root",
    "resolve_runtime",
]


@dataclass(frozen=True, slots=True)
class RuntimeMode:
    kind: Literal["project-root", "profile-root"]
    project_root: Path
    profile_root: Path | None
    profile_name: str | None
    isolated_output: bool
    source_access: Literal["brokered", "cached"] = "brokered"


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    project: object
    mode: RuntimeMode


def _is_project_root(path: Path) -> bool:
    booktx_dir = path / ".booktx"
    return (booktx_dir / "source-config.toml").is_file() or (
        booktx_dir / "config.toml"
    ).is_file()


def _is_profile_root(path: Path) -> bool:
    return (path / ".booktx-profile.json").is_file() and (
        path / "config.toml"
    ).is_file()


def find_project_root(start: Path) -> Path:
    current = start.expanduser().resolve()
    for candidate in (current, *current.parents):
        if _is_project_root(candidate):
            return candidate
    raise _err(
        "project_root_not_found",
        "could not locate the enclosing booktx project root for this profile root",
    )


def _validate_profile_root_marker(
    *,
    project_root: Path,
    profile_root: Path,
    marker: ProfileRootMarker,
) -> None:
    if marker.profile != profile_root.name:
        raise _err(
            "profile_root_marker_mismatch",
            "profile root marker does not match the enclosing profile directory name",
        )
    if not source_config_path(project_root).is_file():
        raise _err(
            "profile_root_invalid_project",
            "profile-root isolated mode requires a profile-layout booktx project",
        )
    expected_profile_dir = profile_dir(project_root, marker.profile).resolve()
    if expected_profile_dir != profile_root:
        raise _err(
            "profile_root_marker_mismatch",
            "profile root marker does not belong to this enclosing project root",
        )
    profile_cfg = load_profile_config(project_root, marker.profile)
    if profile_cfg.profile != marker.profile:
        raise _err(
            "profile_root_marker_mismatch",
            "profile root marker does not match the profile config",
        )
    if profile_cfg.target_language != marker.target_language:
        raise _err(
            "profile_root_marker_mismatch",
            "profile root marker target language does not match the profile config",
        )
    expected_locale = profile_cfg.target_locale or profile_cfg.target_language
    if expected_locale != marker.target_locale:
        raise _err(
            "profile_root_marker_mismatch",
            "profile root marker target locale does not match the profile config",
        )
    source_project = load_project(project_root)
    expected_source_id = project_source_id_or_unavailable(source_project)
    if marker.source_id != expected_source_id:
        raise _err(
            "stale_profile_root_marker",
            "profile root marker source id is stale"
            " for the current extracted source state",
        )


def resolve_runtime(
    project_arg: Path,
    *,
    profile: str | None = None,
    require_profile: bool = False,
) -> RuntimeContext:
    resolved = project_arg.expanduser().resolve()

    if _is_project_root(resolved):
        project = load_project(
            resolved, profile=profile, require_profile=require_profile
        )
        return RuntimeContext(
            project=project,
            mode=RuntimeMode(
                kind="project-root",
                project_root=project.root,
                profile_root=None,
                profile_name=project.profile,
                isolated_output=False,
            ),
        )

    if _is_profile_root(resolved):
        marker = load_profile_root_marker(resolved)
        project_root = find_project_root(resolved)
        _validate_profile_root_marker(
            project_root=project_root,
            profile_root=resolved,
            marker=marker,
        )
        if profile and profile != marker.profile:
            raise _err(
                "profile_root_profile_mismatch",
                "profile-root isolated mode cannot target a different profile",
            )
        project = load_project(
            project_root, profile=marker.profile, require_profile=True
        )
        return RuntimeContext(
            project=project,
            mode=RuntimeMode(
                kind="profile-root",
                project_root=project.root,
                profile_root=resolved,
                profile_name=marker.profile,
                isolated_output=True,
            ),
        )

    if profile is None:
        raise _err(
            "not_a_project",
            f"{resolved} is not a booktx project root or profile root",
        )
    raise _err(
        "not_a_project",
        f"{resolved} is not a booktx project root"
        f" or profile root for profile {profile}",
    )
