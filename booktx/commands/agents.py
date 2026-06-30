"""Typer commands for generated ``AGENTS.md`` harness instructions.

Thin command layer for the ``agents`` group (write / status / clean). Commands
delegate to :mod:`booktx.workflows.agents` and render results with
project-relative paths in project-root mode and the literal ``AGENTS.md`` in
profile-root mode. JSON serialization never exposes the internal absolute
:class:`~pathlib.Path` values returned by the workflows.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from booktx.agents_md import AGENTS_FILENAME, AgentsMdStatusEntry, AgentsMdSyncResult
from booktx.cli_support import (
    _load_runtime_or_exit,
    console,
)
from booktx.errors import BooktxError
from booktx.tasks import project_relative
from booktx.workflows.agents import (
    agents_status_workflow,
    clean_agents_workflow,
    write_agents_workflow,
)

__all__ = ["agents_app"]

agents_app = typer.Typer(help="Manage generated AGENTS.md harness instructions.")

_WRITE_MODES = ("isolated", "collaborative")
_CLEAN_MODES = ("isolated", "collaborative", "all")

_HINTS: dict[str, str] = {
    "agents_unmanaged_target": (
        "inspect it manually, then rerun with --replace-unmanaged to replace it"
    ),
    "agents_profile_required": (
        "pass --profile PROFILE, or run `booktx profile select PROJECT PROFILE`"
    ),
    "agents_unmanaged_ancestor": (
        "resolve the project-root AGENTS.md first, then rerun "
        "`booktx agents write . --mode isolated --profile PROFILE`"
    ),
    "agents_collaborative_requires_project_root": (
        "run `booktx agents write . --mode collaborative` from the project root"
    ),
    "agents_legacy_isolated_unsupported": (
        "isolated mode needs a profile-layout project; use --mode collaborative"
    ),
    "agents_clean_all_profile_ambiguous": (
        "drop --profile, or choose --mode isolated --profile PROFILE"
    ),
    "agents_profile_root_not_ready": (
        "run `booktx agents write . --mode isolated --profile PROFILE` "
        "from the project root"
    ),
}


def _handle_agents_error(exc: BooktxError) -> None:
    console.print(f"[red]error:[/red] {exc}")
    hint = _HINTS.get(exc.code)
    if hint:
        console.print(f"[yellow]hint:[/yellow] {hint}")
    raise typer.Exit(code=1) from exc


def _validate_mode(value: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise BooktxError(
            "agents_invalid_mode",
            f"invalid --mode {value!r}; expected one of {', '.join(allowed)}",
        )
    return value


def _render_path(path: Path, runtime_root: Path, *, profile_root: bool) -> str:
    """Render a result path: literal AGENTS.md in profile-root mode,
    else project-relative."""
    if profile_root:
        return AGENTS_FILENAME
    return project_relative(path, runtime_root)


def _runtime_root_for_rendering(
    project_dir: Path, profile: str | None
) -> tuple[Path, bool]:
    """Return (project root, is_profile_root) for path rendering.

    Called only after a workflow succeeded, so runtime resolution is safe.
    """
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=False)
    return runtime.project.root, runtime.mode.kind == "profile-root"


# --------------------------------------------------------------------------
# write
# --------------------------------------------------------------------------


@agents_app.command(name="write")
def agents_write_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory or profile root."),
    mode: str = typer.Option(
        ...,
        "--mode",
        help="isolated (profile-local) or collaborative (project-root).",
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Target profile (project-root isolated only)."
    ),
    replace_unmanaged: bool = typer.Option(
        False,
        "--replace-unmanaged",
        help="Replace an unmanaged target AGENTS.md (regular file only).",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Write the managed AGENTS.md for the selected mode."""
    mode_val = _validate_mode(mode, _WRITE_MODES)
    try:
        result = write_agents_workflow(
            project_dir,
            mode=mode_val,  # type: ignore[arg-type]
            profile=profile,
            replace_unmanaged=replace_unmanaged,
        )
    except BooktxError as exc:
        _handle_agents_error(exc)
        return
    root, profile_root = _runtime_root_for_rendering(project_dir, profile)
    if as_json:
        payload = _sync_result_to_payload(result, root, profile_root=profile_root)
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    _render_write_human(result, root, profile_root=profile_root)


def _sync_result_to_payload(
    result: AgentsMdSyncResult, root: Path, *, profile_root: bool
) -> dict[str, Any]:
    return {
        "mode": result.mode,
        "profile": result.profile,
        "written": [
            _render_path(p, root, profile_root=profile_root) for p in result.written
        ],
        "deleted": [
            _render_path(p, root, profile_root=profile_root) for p in result.deleted
        ],
        "skipped": [
            {
                "path": _render_path(s.path, root, profile_root=profile_root),
                "reason": s.reason,
            }
            for s in result.skipped
        ],
    }


def _render_write_human(
    result: AgentsMdSyncResult, root: Path, *, profile_root: bool
) -> None:
    console.print(f"mode: {result.mode}")
    if result.profile is not None:
        console.print(f"profile: {result.profile}")
    for path in result.written:
        console.print(f"written: {_render_path(path, root, profile_root=profile_root)}")
    for path in result.deleted:
        console.print(f"deleted: {_render_path(path, root, profile_root=profile_root)}")
    for skip in result.skipped:
        rendered = _render_path(skip.path, root, profile_root=profile_root)
        console.print(f"skipped: {rendered}: {skip.reason}")
    # Next-step hint: never leaks parent paths in profile-root mode.
    if result.mode == "isolated":
        if profile_root:
            console.print("next: start the agent harness in this directory")
        else:
            target_dir = result.written[0].parent if result.written else None
            rel = project_relative(target_dir, root) if target_dir is not None else "."
            console.print(f"next: cd {rel} and start the agent harness")
    else:  # collaborative
        console.print("next: start the agent harness at the project root")


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


@agents_app.command(name="status")
def agents_status_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory or profile root."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Report the ownership state of applicable AGENTS.md files."""
    try:
        entries = agents_status_workflow(project_dir)
    except BooktxError as exc:
        _handle_agents_error(exc)
        return
    runtime = _load_runtime_or_exit(project_dir, require_profile=False)
    root = runtime.project.root
    profile_root = runtime.mode.kind == "profile-root"
    if as_json:
        payload = [
            _status_entry_to_payload(e, root, profile_root=profile_root)
            for e in entries
        ]
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    _render_status_human(entries, root, profile_root=profile_root)


def _status_entry_to_payload(
    entry: AgentsMdStatusEntry, root: Path, *, profile_root: bool
) -> dict[str, Any]:
    meta = entry.inspection.metadata
    return {
        "path": _render_path(entry.path, root, profile_root=profile_root),
        "scope": entry.scope,
        "profile": entry.profile,
        "state": entry.inspection.state,
        "mode": meta.mode if meta is not None else None,
        "source_id": meta.source_id if meta is not None else None,
        "stale": entry.stale,
    }


def _render_status_human(
    entries: tuple[AgentsMdStatusEntry, ...], root: Path, *, profile_root: bool
) -> None:
    for entry in entries:
        rendered = _render_path(entry.path, root, profile_root=profile_root)
        scope_label = f"profile {entry.profile}" if entry.profile else "project"
        line = f"{rendered} ({scope_label}): {entry.inspection.state}"
        meta = entry.inspection.metadata
        if meta is not None:
            freshness = "stale" if entry.stale else "fresh"
            if entry.stale is None:
                freshness = "source-id unavailable"
            line += f" [{meta.mode}, {freshness}]"
        console.print(line, markup=False)


# --------------------------------------------------------------------------
# clean
# --------------------------------------------------------------------------


@agents_app.command(name="clean")
def agents_clean_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory or profile root."),
    mode: str = typer.Option(
        ...,
        "--mode",
        help="isolated, collaborative, or all (managed-mode filter).",
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Target profile (project-root isolated only)."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Delete booktx-managed AGENTS.md files matching the mode filter."""
    mode_val = _validate_mode(mode, _CLEAN_MODES)
    try:
        result = clean_agents_workflow(
            project_dir,
            mode=mode_val,  # type: ignore[arg-type]
            profile=profile,
        )
    except BooktxError as exc:
        _handle_agents_error(exc)
        return
    root, profile_root = _runtime_root_for_rendering(project_dir, profile)
    if as_json:
        payload = _sync_result_to_payload(result, root, profile_root=profile_root)
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    _render_clean_human(result, root, profile_root=profile_root)


def _render_clean_human(
    result: AgentsMdSyncResult, root: Path, *, profile_root: bool
) -> None:
    console.print(f"mode: {result.mode}")
    if result.profile is not None:
        console.print(f"profile: {result.profile}")
    if not result.deleted and not result.skipped:
        console.print("deleted: (none)")
    for path in result.deleted:
        console.print(f"deleted: {_render_path(path, root, profile_root=profile_root)}")
    for skip in result.skipped:
        rendered = _render_path(skip.path, root, profile_root=profile_root)
        console.print(f"skipped: {rendered}: {skip.reason}")
