"""Typer CLI for booktx.

Commands (see ``booktx_coding_agent_start.md``)::

    booktx init ./book --target de
    booktx inspect ./book
    booktx extract ./book
    booktx next ./book
    booktx validate ./book
    booktx build ./book

booktx never translates text; it extracts, validates, and rebuilds.
"""

# ruff: noqa: E501

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from booktx import __version__
from booktx.acceptance import (
    SubmissionValidationError,
    accept_one_record,
    accept_translation_records,
)
from booktx.build import BuildError, build_project
from booktx.chapters import (
    ChapterMap,
    detect_chapters,
    load_chapter_map,
    write_chapter_map,
)
from booktx.chunking import RECORD_ID_SCHEME, segmenter_metadata, spans_to_chunks
from booktx.command_hints import (
    build_command,
    context_chapter_note_command,
    translate_next_command,
    translate_todo_resume_command,
    translate_todo_status_command,
)
from booktx.config import (
    BooktxError,
    Project,
    _err,
    create_profile,
    find_source_file,
    identity_path,
    init_project,
    load_identity,
    load_manifest,
    load_profile_config,
    load_profile_project,
    load_project,
    load_source_project,
    load_translation_store,
    load_translation_task,
    load_translation_version_ledger,
    migrate_current_project,
    project_source_sha256,
    protected_terms_sha256,
    select_profile,
    translation_ingest_block_path,
    translation_ingest_path,
    translation_store_path,
    translation_task_path,
    translation_task_source_block_path,
    write_identity,
    write_translation_store,
)
from booktx.context import (
    ChapterContext,
    ContextMarkdownDrift,
    ContextQuestion,
    GlossaryEntry,
    TranslationContext,
    analyze_context_markdown_drift,
    apply_answer_to_context,
    context_markdown_path,
    context_path,
    default_context,
    ensure_context_markdown_safe_to_overwrite,
    hydrate_chapter_contexts_from_chapter_map,
    load_context,
    merge_chapter_contexts,
    parse_context_markdown_chapter_notes,
    render_context_markdown,
    upsert_chapter_context,
    write_context,
    write_context_markdown,
)
from booktx.epub_io import EpubExtraction, extract_epub
from booktx.epub_manifest import EPUB2TEXT_SCHEMA, EPUB_TEMPLATE_PIPELINE
from booktx.html_io import build_xhtml  # noqa: F401  (kept for downstream use)
from booktx.identity import identity_payload
from booktx.markdown_io import extract_markdown
from booktx.models import (
    Chunk,
    Manifest,
    NamesFile,
    StoredTranslationRecordV2,
    TranslatedChunk,
    TranslatedRecord,
    TranslationCandidate,
    TranslationIdentity,
    TranslationStore,
    TranslationTask,
)
from booktx.pass_through import (
    ensure_pass_through_profile,
    run_pass_through,
)
from booktx.path_display import display_path
from booktx.progress import (
    SourceRecordView,
    load_source_chunks,
    load_source_records,
    source_record_sha256,
)
from booktx.record_refs import parse_record_ref, resolve_record_range
from booktx.runtime import RuntimeContext, RuntimeMode, resolve_runtime
from booktx.status import (
    ChapterProgress,
    ProfilesOverview,
    StatusBundle,
    build_profiles_overview,
    build_status_snapshot,
)
from booktx.submissions import resolve_submission
from booktx.tasks import create_translation_task, select_translation_record_ids
from booktx.todo_resume import resolve_translation_todo, resume_translation_todo
from booktx.todo_status import build_todo_status, load_translation_todo
from booktx.translation_store import (
    active_candidate,
    ensure_store_record,
    find_candidate,
    migrate_legacy_store,
    upsert_translation_version,
)
from booktx.validate import (
    Finding,
    Severity,
    strict_load_translated,
    validate_chunk_pair,
    validate_project,
    validate_record_pair,
    write_report,
)
from booktx.versioning import (
    default_identity,
    fork_current_context,
    lookup_version,
    resolve_current_version,
    resolve_identity,
    select_active_version,
    set_track_label,
)

app = typer.Typer(
    name="booktx",
    help=(
        "Prepare Markdown and EPUB documents for translation by a coding agent. "
        "booktx does NOT translate text; it extracts, validates, and rebuilds."
    ),
    invoke_without_command=True,
    add_completion=False,
)

console = Console()
context_app = typer.Typer(help="Build, inspect, and render translation context.")
translate_app = typer.Typer(help="Command-based translation workflow.")
source_app = typer.Typer(help="Inspect brokered source records without path leaks.")
doctor_app = typer.Typer(help="Diagnostic commands.")
actor_app = typer.Typer(help="Manage translation actor defaults.")
harness_app = typer.Typer(help="Manage translation harness defaults.")
model_app = typer.Typer(help="Manage translation model defaults.")
identity_app = typer.Typer(
    help="Inspect resolved translation identity and project state."
)
version_app = typer.Typer(help="Inspect and manage translation version tracks.")
profile_app = typer.Typer(help="Manage isolated translation profiles.")
app.add_typer(context_app, name="context")
app.add_typer(translate_app, name="translate")
app.add_typer(translate_app, name="translation")
app.add_typer(source_app, name="source")
app.add_typer(doctor_app, name="doctor")
app.add_typer(actor_app, name="actor")
app.add_typer(harness_app, name="harness")
app.add_typer(model_app, name="model")
app.add_typer(identity_app, name="identity")
app.add_typer(version_app, name="version")
app.add_typer(profile_app, name="profile")


def _die(message: str, code: int = 1) -> None:
    """Print an error and exit with ``code``."""
    console.print(f"[red]error:[/red] {message}")
    raise typer.Exit(code=code)


def _handle_booktx_error(exc: BooktxError) -> None:
    _die(str(exc))


def _isolated_mode_error() -> str:
    return (
        "command is not available in profile-root isolated mode.\n"
        "Run this from the project root for collaborative/admin workflows."
    )


def _reject_if_isolated(runtime: RuntimeContext) -> None:
    if runtime.mode.isolated_output:
        _die(_isolated_mode_error())


def _display_path(path: Path, mode: RuntimeMode | None) -> str:
    if mode is not None:
        return display_path(path, mode)
    return path.as_posix()


def _load_runtime_or_exit(
    project_dir: Path,
    *,
    profile: str | None = None,
    require_profile: bool = False,
) -> RuntimeContext:
    try:
        return resolve_runtime(
            project_dir,
            profile=profile,
            require_profile=require_profile,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        raise typer.Exit(code=1) from exc


def _submission_ingest_hint(
    proj: Project,
    task_id: str | None,
    *,
    mode: RuntimeMode | None = None,
) -> str | None:
    """Project-relative path to the canonical profile-local ingest file.

    Used to point agents at the generated submission location when a
    ``--file``/``--json-file`` path is missing. Returns ``None`` when no
    profile is selected or the task id is unknown.
    """
    if proj.profile is None or not task_id:
        return None
    from booktx.config import translation_ingest_block_path

    return (
        display_path(translation_ingest_block_path(proj, task_id), mode)
        if mode is not None
        else _project_relative(translation_ingest_block_path(proj, task_id), proj.root)
    )


def _resolve_project_value_args(
    arg1: str,
    arg2: str | None,
    *,
    value_name: str,
    project_dir: Path | None = None,
) -> tuple[Path, str]:
    """Accept VALUE, VALUE PROJECT_DIR, or PROJECT_DIR VALUE."""
    if project_dir is not None:
        if arg2 is not None:
            _die(f"--project cannot be combined with a second positional {value_name}")
        return project_dir.expanduser(), arg1

    if arg2 is None:
        return Path("."), arg1

    p1 = Path(arg1).expanduser()
    p2 = Path(arg2).expanduser()
    p1_is_project = (p1 / ".booktx" / "config.toml").is_file() or (
        p1 / ".booktx" / "source-config.toml"
    ).is_file()
    p2_is_project = (p2 / ".booktx" / "config.toml").is_file() or (
        p2 / ".booktx" / "source-config.toml"
    ).is_file()

    if p1_is_project and not p2_is_project:
        return p1, arg2
    if p2_is_project and not p1_is_project:
        return p2, arg1
    return p1, arg2


def _render_identity_human(payload: dict[str, Any]) -> None:
    context_payload = payload["context"]
    store_payload = payload["store"]
    context_state = {
        "ready": "READY",
        "not_ready": "NOT_READY",
        "missing": "MISSING",
        "invalid": "INVALID",
    }[str(context_payload["status"])]
    rows = [
        ("actor", payload["actor"]),
        ("harness", payload["harness"]),
        ("model", payload["model"]),
        ("active_version", payload["active_version"] or "none"),
        ("context", f"{context_state} {context_payload['path']}"),
        ("context_sha256", context_payload["sha256"] or "none"),
        ("source_sha256", payload["source_sha256"] or "none"),
        (
            "store_version",
            store_payload["version"]
            if store_payload["version"] is not None
            else "none",
        ),
        (
            "store_records",
            store_payload["record_count"]
            if store_payload["record_count"] is not None
            else "none",
        ),
    ]
    width = max(len(label) for label, _ in rows)
    console.print(f"booktx identity: {payload['project_dir']}", soft_wrap=True)
    for label, value in rows:
        console.print(f"{label + ':':<{width + 2}} {value}", soft_wrap=True)


def _print_identity(project_dir: Path, *, profile: str | None, as_json: bool) -> None:
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    payload = identity_payload(runtime.project, mode=runtime.mode)
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    _render_identity_human(payload)


# --- version -----------------------------------------------------------------


@version_app.callback(invoke_without_command=True)
def version_root(ctx: typer.Context) -> None:
    """Translation-version command group."""
    if ctx.invoked_subcommand is None:
        console.print(
            "[red]error:[/red] `booktx version` is a translation-version command "
            "group. Use `booktx --version` for the CLI package version, or "
            "`booktx version current PROJECT_DIR` for the active translation "
            "version.",
            soft_wrap=True,
        )
        raise typer.Exit(code=2)


@app.command(name="whoami")
def whoami(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Show resolved translation identity and project status."""
    _print_identity(project_dir, profile=profile, as_json=as_json)


@identity_app.command(name="whoami")
def identity_whoami(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Alias for the top-level whoami command."""
    _print_identity(project_dir, profile=profile, as_json=as_json)


@app.command(name="mode")
def mode_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory or profile root."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Show how booktx resolved the current working path."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=False)
    payload = {
        "mode": runtime.mode.kind,
        "profile": runtime.mode.profile_name,
        "profiles_visible": not runtime.mode.isolated_output,
        "cross_profile_access": not runtime.mode.isolated_output,
        "safe_for_model_evaluation": runtime.mode.isolated_output,
        "source_access": runtime.mode.source_access,
    }
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    console.print(f"mode: {payload['mode']}")
    if payload["profile"]:
        console.print(f"profile: {payload['profile']}")
    console.print(f"profiles visible: {'yes' if payload['profiles_visible'] else 'no'}")
    console.print(
        f"cross-profile access: {'yes' if payload['cross_profile_access'] else 'no'}"
    )
    console.print(
        "safe for model evaluation: "
        f"{'yes' if payload['safe_for_model_evaluation'] else 'no'}"
    )


@source_app.command(name="status")
def source_status_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory or profile root."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Show a safe summary of extracted source state."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=False)
    proj = runtime.project
    manifest = load_manifest(proj)
    source_records = load_source_records(proj)
    chapter_map = load_chapter_map(proj) or detect_chapters(proj)
    payload = {
        "source": "available" if proj.chunks() else "missing",
        "format": proj.config.format,
        "source_language": proj.config.source_language,
        "records": len(source_records),
        "chunks": len(proj.chunks()),
        "chapters": len(chapter_map.chapters),
        "source_sha256": manifest.source.sha256 if manifest is not None else "",
    }
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    console.print(f"source: {payload['source']}")
    console.print(f"format: {payload['format']}")
    console.print(f"source language: {payload['source_language']}")
    console.print(f"records: {payload['records']}")
    console.print(f"chunks: {payload['chunks']}")
    console.print(f"chapters: {payload['chapters']}")


@source_app.command(name="record")
def source_record_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory or profile root."),
    record_ref: str = typer.Argument(..., help="Record id or record ref."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    output_format: str = typer.Option(
        "block",
        "--format",
        help="Output format: block, text, or json.",
    ),
) -> None:
    """Print one source record without exposing chunk paths."""
    if output_format not in {"block", "text", "json"}:
        _die("--format must be block, text, or json")
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=False)
    proj = runtime.project
    canonical_id = parse_record_ref(record_ref).canonical_id
    source_by_id = {record.record_id: record for record in load_source_records(proj)}
    record = source_by_id.get(canonical_id)
    if record is None:
        _die(f"unknown source record id: {canonical_id}")
        return
    payload = {"id": record.record_id, "source": record.source}
    if output_format == "json":
        console.print_json(json.dumps(payload, ensure_ascii=False))
    elif output_format == "text":
        console.print(f"{record.record_id}\t{record.source}")
    else:
        console.print(f">>> {record.record_id}")
        console.print(record.source)


@source_app.command(name="chapter")
def source_chapter_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory or profile root."),
    chapter_id: str = typer.Argument(..., help="Chapter id, e.g. 0001."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    output_format: str = typer.Option(
        "block",
        "--format",
        help="Output format: block, text, or json.",
    ),
) -> None:
    """Print all source records for one chapter without exposing chunk paths."""
    if output_format not in {"block", "text", "json"}:
        _die("--format must be block, text, or json")
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=False)
    proj = runtime.project
    bundle = _project_status_snapshot(proj)
    record_ids = bundle.index.record_ids_by_chapter.get(chapter_id)
    chapter = bundle.index.chapters_by_id.get(chapter_id)
    if not record_ids or chapter is None:
        _die(f"unknown chapter id: {chapter_id}")
        return
    records = [
        {"id": record_id, "source": bundle.index.source_by_id[record_id].source}
        for record_id in record_ids
    ]
    if output_format == "json":
        console.print_json(
            json.dumps(
                {
                    "chapter_id": chapter.chapter_id,
                    "title": chapter.title,
                    "records": records,
                },
                ensure_ascii=False,
            )
        )
        return
    for item in records:
        if output_format == "text":
            console.print(f"{item['id']}\t{item['source']}")
        else:
            console.print(f">>> {item['id']}")
            console.print(item["source"])
            if item != records[-1]:
                console.print()


@doctor_app.command(name="isolation")
def doctor_isolation_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory or profile root."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Check whether the current path is ready for isolated evaluation."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=False)
    proj = runtime.project
    marker_exists = bool(
        runtime.mode.profile_root
        and (runtime.mode.profile_root / ".booktx-profile.json").is_file()
    )
    profile_local_context = bool(
        proj.context_json_path is not None
        and runtime.mode.profile_root is not None
        and proj.context_json_path.parent == runtime.mode.profile_root
    )
    profile_local_store = bool(
        proj.store_path is not None
        and runtime.mode.profile_root is not None
        and proj.store_path.parent == runtime.mode.profile_root
    )
    profile_local_ledger = bool(
        proj.ledger_path is not None
        and runtime.mode.profile_root is not None
        and proj.ledger_path.parent == runtime.mode.profile_root
    )
    redacted_samples = [
        display_path(proj.root, runtime.mode),
        display_path(proj.chunks_dir, runtime.mode),
        display_path(proj.profile_dir or proj.root, runtime.mode),
    ]
    path_redaction_pass = all(
        not sample.startswith("/")
        and "../" not in sample
        and (runtime.mode.profile_name or "") not in sample.replace(".", "")
        for sample in redacted_samples[:2]
    )
    source_available = bool(proj.chunks())
    passed = (
        runtime.mode.isolated_output
        and marker_exists
        and source_available
        and profile_local_context
        and profile_local_store
        and profile_local_ledger
        and path_redaction_pass
    )
    payload = {
        "isolation": "PASS" if passed else "FAIL",
        "mode": runtime.mode.kind,
        "profile": runtime.mode.profile_name,
        "source_broker": "available" if source_available else "unavailable",
        "cross_profile_commands": "blocked"
        if runtime.mode.isolated_output
        else "available",
        "path_redaction": "PASS" if path_redaction_pass else "FAIL",
        "source_access": runtime.mode.source_access,
    }
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
    else:
        console.print(f"isolation: {payload['isolation']}")
        console.print(f"mode: {payload['mode']}")
        if payload["profile"]:
            console.print(f"profile: {payload['profile']}")
        console.print(f"source broker: {payload['source_broker']}")
        console.print(f"cross-profile commands: {payload['cross_profile_commands']}")
        console.print(f"path redaction: {payload['path_redaction']}")
    if not passed:
        raise typer.Exit(code=1)


@actor_app.command(name="whoami")
def actor_whoami(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Show the resolved actor default for translation versioning."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    console.print(_resolved_identity(proj).actor)


@actor_app.command(name="set")
def actor_set(
    arg1: str = typer.Argument(
        ..., help="Actor value, or project directory when using the legacy order."
    ),
    arg2: str | None = typer.Argument(
        None, help="Optional project directory or actor value."
    ),
    project: Path | None = typer.Option(None, "--project", help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Persist the actor default used for new version tracks."""
    project_dir, actor = _resolve_project_value_args(
        arg1, arg2, value_name="actor", project_dir=project
    )
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    identity = _write_identity_defaults(proj, actor=actor)
    console.print(identity.actor)


@actor_app.command(name="clear")
def actor_clear(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Clear the stored actor default back to the local fallback."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    identity = _clear_identity_field(proj, "actor")
    console.print(identity.actor)


@harness_app.command(name="whoami")
def harness_whoami(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Show the resolved harness default for translation versioning."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    console.print(_resolved_identity(proj).harness)


@harness_app.command(name="set")
def harness_set(
    arg1: str = typer.Argument(
        ..., help="Harness value, or project directory when using the legacy order."
    ),
    arg2: str | None = typer.Argument(
        None, help="Optional project directory or harness value."
    ),
    project: Path | None = typer.Option(None, "--project", help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Persist the harness default used for new version tracks."""
    project_dir, harness = _resolve_project_value_args(
        arg1, arg2, value_name="harness", project_dir=project
    )
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    identity = _write_identity_defaults(proj, harness=harness)
    console.print(identity.harness)


@harness_app.command(name="clear")
def harness_clear(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Clear the stored harness default back to the local fallback."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    identity = _clear_identity_field(proj, "harness")
    console.print(identity.harness)


@model_app.command(name="whoami")
def model_whoami(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Show the resolved model default for translation versioning."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    console.print(_resolved_identity(proj).model)


@model_app.command(name="set")
def model_set(
    arg1: str = typer.Argument(
        ..., help="Model value, or project directory when using the legacy order."
    ),
    arg2: str | None = typer.Argument(
        None, help="Optional project directory or model value."
    ),
    project: Path | None = typer.Option(None, "--project", help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Persist the model default used for new version tracks."""
    project_dir, model = _resolve_project_value_args(
        arg1, arg2, value_name="model", project_dir=project
    )
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    identity = _write_identity_defaults(proj, model=model)
    console.print(identity.model)


@model_app.command(name="clear")
def model_clear(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Clear the stored model default back to the local fallback."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    identity = _clear_identity_field(proj, "model")
    console.print(identity.model)


@version_app.command(name="current")
def version_current(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Show the current ledger-wide active version."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    ledger = load_translation_version_ledger(proj)
    payload = {
        "active_version": ledger.active_version,
        "track_count": len(ledger.tracks),
    }
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    console.print(ledger.active_version or "none")


@version_app.command(name="list")
def version_list(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """List all known major tracks and subversions."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    ledger = load_translation_version_ledger(proj)
    if not ledger.tracks:
        console.print("no versions")
        return
    for track_id in sorted(ledger.tracks, key=int):
        track = ledger.tracks[track_id]
        active_marker = (
            "*"
            if ledger.active_version
            and ledger.active_version.startswith(f"{track.version}.")
            else " "
        )
        console.print(
            f"{active_marker} track {track.version}: {track.actor} / {track.harness} / "
            f"{track.model}{f' [{track.label}]' if track.label else ''}"
        )
        for sub_id in sorted(track.subversions, key=int):
            sub = track.subversions[sub_id]
            current_marker = (
                " (active)" if ledger.active_version == sub.version_ref else ""
            )
            scope_label = (
                f"baseline:{sub.baseline_sha256}"
                if sub.baseline_sha256 is not None
                else f"legacy-context:{sub.context_sha256}"
            )
            console.print(f"    {sub.version_ref}  {scope_label}{current_marker}")


@version_app.command(name="select")
def version_select(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    version_ref: str = typer.Argument(..., help="Version ref such as 1.2."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Select the ledger-wide active version."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    ledger = select_active_version(proj, version_ref)
    console.print(ledger.active_version or "none")


@version_app.command(name="set-label")
def version_set_label(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    major_version: int = typer.Argument(..., help="Major track number."),
    label: str = typer.Argument(..., help="Human label for the track."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Set the label for one major version track."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    ledger = set_track_label(proj, major_version, label)
    console.print(ledger.tracks[str(major_version)].label or "")


@version_app.command(name="fork-context")
def version_fork_context(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    note: str | None = typer.Option(None, "--note", help="Reason for the forced fork."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Force a new subversion for the current track even when context hash matches."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    resolution = fork_current_context(proj, note=note)
    console.print(resolution.version_ref)


@version_app.command(name="show")
def version_show(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    selector: str = typer.Argument(..., help="Track number or dotted version ref."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Show one major track or one specific dotted version entry."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    ledger = load_translation_version_ledger(proj)
    if "." in selector:
        track, sub = lookup_version(ledger, selector)
        payload = {
            "version_ref": sub.version_ref,
            "version": track.version,
            "subversion": sub.subversion,
            "actor": track.actor,
            "harness": track.harness,
            "model": track.model,
            "label": track.label,
            "context_sha256": sub.context_sha256,
            "baseline_sha256": sub.baseline_sha256,
            "baseline_path": sub.baseline_path,
            "legacy_full_context_sha256": sub.legacy_full_context_sha256,
            "legacy_full_context_path": sub.legacy_full_context_path,
            "context_label": sub.context_label,
            "forced": sub.forced,
        }
    else:
        track_entry = ledger.tracks.get(str(int(selector)))
        if track_entry is None:
            _die(f"track {selector} not found")
            return
        payload = {
            "version": track_entry.version,
            "actor": track_entry.actor,
            "harness": track_entry.harness,
            "model": track_entry.model,
            "label": track_entry.label,
            "subversions": [
                sub.model_dump(mode="json") for sub in track_entry.subversions.values()
            ],
        }
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    console.print_json(json.dumps(payload, ensure_ascii=False, indent=2))


# --- profile ------------------------------------------------------------------


def _render_profiles_overview_human(overview: ProfilesOverview) -> None:
    console.print(f"project: {overview.project}")
    if overview.source:
        console.print(f"source: {overview.source}")
    if overview.source_records:
        console.print(f"source records: {overview.source_records}")
    if not overview.profiles:
        console.print("profiles: none")
        return
    console.print("profiles:")
    for item in overview.profiles:
        marker = "*" if item.active else " "
        coverage = (
            f"translated={item.translated_records}/{item.total_records}"
            if item.total_records
            else "translated=0/0"
        )
        console.print(
            f"  {marker} {item.profile}   kind={item.kind}  "
            f"target={item.target_locale or item.target_language}  "
            f"model={item.model or 'human'}  {coverage}"
        )
    if overview.active_profile:
        console.print()
        console.print(f"active profile: {overview.active_profile}")


def _profile_detail_payload(project_dir: Path, profile_name: str) -> dict[str, Any]:
    profile_project = load_profile_project(project_dir, profile_name)
    profile_cfg = load_profile_config(project_dir, profile_name)
    resolved_identity = resolve_identity(profile_project)
    context = load_context(profile_project)
    active_version = load_translation_version_ledger(profile_project).active_version
    records_translated = 0
    records_total = 0
    chapters_complete = 0
    chapters_total = 0
    if profile_project.chunks():
        bundle = build_status_snapshot(
            profile_project,
            context_exists=context is not None,
            context_ready=bool(context and context.ready),
        )
        records_translated = bundle.snapshot.totals.records_translated
        records_total = bundle.snapshot.totals.records_total
        chapters_complete = bundle.snapshot.totals.chapters_complete
        chapters_total = bundle.snapshot.totals.chapters_total
    return {
        "profile": profile_name,
        "kind": profile_cfg.kind,
        "path": _project_relative(profile_project.profile_dir, profile_project.root)
        if profile_project.profile_dir is not None
        else "",
        "target_language": profile_cfg.target_language,
        "target_locale": profile_cfg.target_locale or profile_cfg.target_language,
        "output_filename": profile_cfg.output_filename,
        # Live identity comes from translations/<profile>/identity.json;
        # profile_cfg.identity is only the initial default captured at creation.
        "actor": resolved_identity.actor,
        "harness": resolved_identity.harness,
        "model": resolved_identity.model,
        "context_ready": bool(context and context.ready),
        "active_version": active_version,
        "records_translated": records_translated,
        "records_total": records_total,
        "chapters_complete": chapters_complete,
        "chapters_total": chapters_total,
    }


@profile_app.command(name="create")
def profile_create_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile_name: str = typer.Argument(..., help="Translation profile name."),
    target: str = typer.Option(..., "--target", help="Target language code, e.g. de."),
    target_locale: str | None = typer.Option(
        None, "--target-locale", help="Target locale code, e.g. de-DE."
    ),
    model: str | None = typer.Option(None, "--model", help="Profile model label."),
    harness: str | None = typer.Option(
        None, "--harness", help="Profile harness label."
    ),
    actor: str | None = typer.Option(None, "--actor", help="Profile actor label."),
    output_filename: str | None = typer.Option(
        None, "--output-filename", help="Optional output filename override."
    ),
    select: bool = typer.Option(False, "--select", help="Select the created profile."),
) -> None:
    try:
        project = create_profile(
            project_dir,
            profile_name,
            target_language=target,
            target_locale=target_locale,
            actor=actor,
            harness=harness,
            model=model,
            output_filename=output_filename,
            select=select,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    console.print(f"created profile: {project.profile}")
    if select:
        console.print(f"selected active profile: {project.profile}")


@profile_app.command(name="list")
def profile_list_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    runtime = _load_runtime_or_exit(project_dir, require_profile=False)
    _reject_if_isolated(runtime)
    project = load_source_project(runtime.project.root)
    overview = build_profiles_overview(project)
    if as_json:
        console.print_json(
            json.dumps(overview.model_dump(mode="json"), ensure_ascii=False)
        )
        return
    _render_profiles_overview_human(overview)


@profile_app.command(name="select")
def profile_select_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile_name: str = typer.Argument(..., help="Translation profile name."),
) -> None:
    runtime = _load_runtime_or_exit(project_dir, require_profile=False)
    _reject_if_isolated(runtime)
    try:
        project = select_profile(runtime.project.root, profile_name)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    console.print(project.profile or profile_name)


@profile_app.command(name="show")
def profile_show_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile_name: str = typer.Argument(..., help="Translation profile name."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    runtime = _load_runtime_or_exit(project_dir, require_profile=False)
    _reject_if_isolated(runtime)
    try:
        payload = _profile_detail_payload(runtime.project.root, profile_name)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    console.print(f"profile: {payload['profile']}")
    console.print(f"kind: {payload['kind']}")
    console.print(f"path: {payload['path']}")
    console.print(f"target: {payload['target_locale']}")
    console.print(f"model: {payload['model']}")
    console.print(f"context: {'ready' if payload['context_ready'] else 'not ready'}")
    console.print(f"active version: {payload['active_version'] or 'none'}")
    console.print(
        f"records translated: {payload['records_translated']}/{payload['records_total']}"
    )
    console.print(
        f"chapters complete: {payload['chapters_complete']}/{payload['chapters_total']}"
    )


@profile_app.command(name="compare")
def profile_compare_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profiles: str = typer.Option(
        ..., "--profiles", help="Comma-separated profile names."
    ),
    record: str = typer.Option(
        ..., "--record", help="Record ref or canonical record id."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    runtime = _load_runtime_or_exit(project_dir, require_profile=False)
    _reject_if_isolated(runtime)
    source_project = load_source_project(runtime.project.root)
    requested = [item.strip() for item in profiles.split(",") if item.strip()]
    if len(requested) < 2:
        _die("--profiles must contain at least two profile names")
        return
    canonical_id = parse_record_ref(record).canonical_id
    source_by_id = {
        item.record_id: item for item in load_source_records(source_project)
    }
    source_record = source_by_id.get(canonical_id)
    if source_record is None:
        _die(f"unknown source record id: {canonical_id}")
        return
    comparisons: list[dict[str, Any]] = []
    for profile_name in requested:
        try:
            profile_project = load_profile_project(runtime.project.root, profile_name)
        except BooktxError as exc:
            _handle_booktx_error(exc)
            return
        store = load_translation_store(profile_project)
        stored = store.records.get(canonical_id)
        candidate = active_candidate(stored) if stored is not None else None
        comparisons.append(
            {
                "profile": profile_name,
                "target_language": profile_project.config.target_language,
                "target_locale": profile_project.config.target_locale,
                "active_version": stored.active_version if stored is not None else None,
                "target": candidate.target if candidate is not None else None,
                "status": candidate.status if candidate is not None else None,
            }
        )
    payload = {
        "record_ref": canonical_id,
        "source": source_record.source,
        "comparisons": comparisons,
    }
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    console.print(f"record: {canonical_id}")
    console.print(f"source: {source_record.source}")
    for item in comparisons:
        console.print(
            f"{item['profile']} ({item['target_locale'] or item['target_language']}): "
            f"{item['target'] or '<missing>'}"
        )


@profile_app.command(name="migrate-current")
def profile_migrate_current_cmd(
    project_dir: Path = typer.Argument(..., help="Legacy project directory."),
    profile_name: str = typer.Argument(..., help="Target translation profile name."),
    target: str | None = typer.Option(
        None, "--target", help="Override target language."
    ),
    target_locale: str | None = typer.Option(
        None, "--target-locale", help="Target locale code, e.g. de-DE."
    ),
    actor: str | None = typer.Option(None, "--actor", help="Profile actor label."),
    harness: str | None = typer.Option(
        None, "--harness", help="Profile harness label."
    ),
    model: str | None = typer.Option(None, "--model", help="Profile model label."),
    select: bool = typer.Option(False, "--select", help="Select the migrated profile."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the migration plan only."
    ),
) -> None:
    try:
        payload = migrate_current_project(
            project_dir,
            profile_name,
            target_language=target,
            target_locale=target_locale,
            actor=actor,
            harness=harness,
            model=model,
            select=select,
            dry_run=dry_run,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    if dry_run:
        console.print(
            f"dry-run: would migrate legacy project to profile {profile_name}"
        )
        moves = payload.get("moves", [])
        if isinstance(moves, list):
            for move in moves:
                console.print(f"{move}")
        return
    console.print(f"migrated profile: {payload['profile']}")
    if "migration_manifest" in payload:
        console.print(f"migration manifest: {payload['migration_manifest']}")
    if select:
        console.print(f"selected active profile: {profile_name}")
    console.print(f"next: booktx status {project_dir} --profile {profile_name}")
    console.print(
        f"next: booktx translate next {project_dir} --profile {profile_name} --unit batch --max-words 500 --format block"
    )


@profile_app.command(name="create-pass-through")
def profile_create_pass_through_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile_name: str = typer.Argument(..., help="Pass-through profile name."),
    output_filename: str | None = typer.Option(
        None, "--output-filename", help="Optional output filename override."
    ),
    select: bool = typer.Option(False, "--select", help="Select the created profile."),
) -> None:
    """Create a pass-through profile whose target language equals the source language."""
    try:
        source_project = load_source_project(project_dir)
        target = source_project.source_config.source_language
        project = create_profile(
            project_dir,
            profile_name,
            target_language=target,
            target_locale=target,
            actor="booktx:pass-through",
            harness="booktx",
            model="booktx/pass-through",
            output_filename=output_filename,
            select=select,
            kind="pass-through",
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    console.print(f"created pass-through profile: {project.profile}")
    if select:
        console.print(f"selected active profile: {project.profile}")


# --- context -----------------------------------------------------------------


def _load_project_or_exit(
    project_dir: Path,
    *,
    profile: str | None = None,
    require_profile: bool = False,
) -> Project:
    return _load_runtime_or_exit(
        project_dir,
        profile=profile,
        require_profile=require_profile,
    ).project


def _load_context_or_exit(proj: Project) -> TranslationContext:
    try:
        ctx = load_context(proj)
    except Exception as exc:  # noqa: BLE001 - surface as user-facing CLI error
        _die(f"translation context is invalid: {exc}")
        raise typer.Exit(code=1) from exc
    if ctx is None:
        _die("translation context is missing. Run: booktx context init .")
        raise typer.Exit(code=1)
    return ctx


def _drift_unsafe_message(drift: ContextMarkdownDrift) -> str:
    parts: list[str] = []
    if drift.missing_in_json:
        parts.append(f"missing_in_json: {', '.join(drift.missing_in_json)}")
    if drift.conflicting:
        parts.append(f"conflicting: {', '.join(drift.conflicting)}")
    if drift.parse_errors:
        parts.append(f"parse_errors: {'; '.join(drift.parse_errors)}")
    return (
        "context.md contains chapter notes that are not safely represented "
        "in context.json. " + "; ".join(parts)
    )


def _guard_md_safe_or_die(
    proj: Project, ctx: TranslationContext, *, allow_discard_md_only: bool = False
) -> None:
    try:
        ensure_context_markdown_safe_to_overwrite(
            proj, ctx, allow_discard_md_only=allow_discard_md_only
        )
    except ValueError as exc:
        _die(
            f"{exc} Run `booktx context import-md . --write` first to recover "
            "Markdown-only notes."
        )


def _open_required_questions(ctx: TranslationContext) -> list[ContextQuestion]:
    return [q for q in ctx.questions if q.required and q.status == "open"]


def _resolved_identity(proj: Project) -> TranslationIdentity:
    return resolve_identity(proj)


def _write_identity_defaults(
    proj: Project,
    *,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
) -> TranslationIdentity:
    identity = resolve_identity(proj, actor=actor, harness=harness, model=model)
    write_identity(proj, identity)
    return identity


def _clear_identity_field(proj: Project, field_name: str) -> TranslationIdentity:
    current = load_identity(proj)
    fallback = default_identity()
    identity = TranslationIdentity(
        actor=current.actor if current is not None else fallback.actor,
        harness=current.harness if current is not None else fallback.harness,
        model=current.model if current is not None else fallback.model,
    )
    setattr(identity, field_name, getattr(fallback, field_name))
    if identity == fallback and identity_path(proj).is_file():
        identity_path(proj).unlink()
        return fallback
    write_identity(proj, identity)
    return identity


def _ordered_source_records(proj: Project) -> list[SourceRecordView]:
    return load_source_records(proj)


def _ledger_metadata_for_version(
    proj: Project, version_ref: str | None
) -> dict[str, Any] | None:
    if not version_ref:
        return None
    ledger = load_translation_version_ledger(proj)
    try:
        track, subversion = lookup_version(ledger, version_ref)
    except BooktxError:
        return None
    return {
        "version_ref": subversion.version_ref,
        "version": track.version,
        "subversion": subversion.subversion,
        "actor": track.actor,
        "harness": track.harness,
        "model": track.model,
        "label": track.label,
        "context_sha256": subversion.context_sha256,
        "baseline_sha256": subversion.baseline_sha256,
        "legacy_full_context_sha256": subversion.legacy_full_context_sha256,
        "context_label": subversion.context_label,
        "forced": subversion.forced,
    }


def _store_record_payload(
    proj: Project, record_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    ordered = _ordered_source_records(proj)
    by_id = {record.record_id: record for record in ordered}
    canonical_id = parse_record_ref(record_id).canonical_id
    source_record = by_id.get(canonical_id)
    if source_record is None:
        raise _err("unknown_record_id", f"unknown source record id: {record_id}")
    store = load_translation_store(proj)
    stored = store.records.get(canonical_id)
    versions: list[dict[str, Any]] = []
    active_version = None
    if stored is not None:
        active_version = stored.active_version
        for candidate in stored.versions:
            versions.append(
                {
                    "version": candidate.version,
                    "subversion": candidate.subversion,
                    "version_ref": candidate.version_ref,
                    "target": candidate.target,
                    "status": candidate.status,
                    "created_at": candidate.created_at,
                    "updated_at": candidate.updated_at,
                    "reviewed_at": candidate.reviewed_at,
                    "reviewed_by": candidate.reviewed_by,
                    "review_note": candidate.review_note,
                }
            )
    selected = {
        "id": canonical_id,
        "chunk_id": source_record.chunk_id,
        "source": source_record.source,
        "source_sha256": source_record.source_sha256,
        "active_version": active_version,
    }
    return selected, {"versions": versions, "store": store, "ordered": ordered}


@context_app.command(name="init")
def context_init(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    non_interactive: bool = typer.Option(
        True, "--non-interactive/--interactive", help="Create open questions or prompt."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing context."),
    seed: str | None = typer.Option(
        None,
        "--seed",
        help="Packaged seed template name (e.g. 'shadows-of-apt').",
    ),
    seed_file: Path | None = typer.Option(
        None,
        "--seed-file",
        help="Path to a JSON seed file with extra questions and glossary.",
    ),
) -> None:
    """Create the active profile's context.json and rendered context.md."""
    from booktx.context import load_seed_template

    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    proj = runtime.project
    existing = None if force else load_context(proj)
    if existing is not None:
        _guard_md_safe_or_die(proj, existing)
        write_context_markdown(proj, existing)
        console.print(
            f"context exists: {display_path(context_markdown_path(proj), runtime.mode)}"
        )
        return

    ctx = default_context(proj)
    # Load seed template if specified.
    if seed is not None:
        try:
            extra_questions, extra_glossary = load_seed_template(seed)
        except FileNotFoundError as exc:
            _die(str(exc))
            return
        ctx.questions.extend(extra_questions)
        ctx.glossary.extend(extra_glossary)
    if seed_file is not None:
        import json as _json

        try:
            seed_data = _json.loads(seed_file.read_text("utf-8"))
        except (FileNotFoundError, _json.JSONDecodeError) as exc:
            _die(f"could not read seed file: {exc}")
            return
        from booktx.context import ContextQuestion, GlossaryEntry

        for q in seed_data.get("questions", []):
            ctx.questions.append(ContextQuestion(**q))
        for g in seed_data.get("glossary", []):
            ctx.glossary.append(GlossaryEntry(**g))
    if not non_interactive:
        for q in ctx.questions:
            answer = typer.prompt(q.question, default="", show_default=False)
            if answer.strip():
                q.answer = answer.strip()
                q.status = "answered"
        ctx.ready = not _open_required_questions(ctx)
    _guard_md_safe_or_die(proj, ctx, allow_discard_md_only=force)
    write_context(proj, ctx)
    write_context_markdown(proj, ctx)
    console.print(f"wrote {display_path(context_path(proj), runtime.mode)}")
    console.print(f"wrote {display_path(context_markdown_path(proj), runtime.mode)}")


@context_app.command(name="questions")
def context_questions(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """List context questions."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    ctx = _load_context_or_exit(proj)
    for q in ctx.questions:
        marker = "required" if q.required else "optional"
        answer = f" -> {q.answer}" if q.answer else ""
        console.print(f"{q.id} [{marker}] {q.status} {q.topic}: {q.question}{answer}")


@context_app.command(name="status")
def context_status(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Show translation context readiness."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    proj = runtime.project
    ctx = _load_context_or_exit(proj)
    open_required = _open_required_questions(ctx)
    open_total = [q for q in ctx.questions if q.status == "open"]
    status = "READY" if ctx.ready else "NOT READY"
    console.print(f"Status: {status}")
    console.print(f"open_required={len(open_required)} open_total={len(open_total)}")
    console.print(f"glossary_entries={len(ctx.glossary)}")
    console.print(
        f"context: {display_path(context_markdown_path(proj), runtime.mode)}",
        soft_wrap=True,
    )


@context_app.command(name="render")
def context_render(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    write: bool = typer.Option(
        False,
        "--write",
        help="Write the active profile's context.md.",
    ),
    stdout: bool = typer.Option(
        False, "--stdout", help="Print rendered Markdown without writing."
    ),
    force_discard_md_only: bool = typer.Option(
        False,
        "--force-discard-md-only",
        help="Allow --write to overwrite despite unsafe Markdown-only notes.",
    ),
) -> None:
    """Render context.md from context.json (dry run by default).

    Without flags, reports whether ``context.md`` matches the render and
    whether writing would be unsafe. ``--stdout`` prints rendered Markdown.
    ``--write`` persists, but refuses when drift analysis says the write is
    unsafe unless ``--force-discard-md-only`` is also passed.
    """
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    proj = runtime.project
    ctx = _load_context_or_exit(proj)
    rendered = render_context_markdown(ctx)
    if stdout:
        typer.echo(rendered)
        return
    md_path = context_markdown_path(proj)
    drift = analyze_context_markdown_drift(proj, ctx)
    matches = bool(
        md_path.is_file()
        and md_path.read_text("utf-8").replace("\r\n", "\n")
        == rendered.replace("\r\n", "\n")
    )
    if write:
        if drift.unsafe_to_overwrite and not force_discard_md_only:
            _die(_drift_unsafe_message(drift))
        write_context_markdown(proj, ctx)
        console.print(f"rendered {display_path(md_path, runtime.mode)}")
        return
    if matches:
        console.print(f"{display_path(md_path, runtime.mode)} is up to date")
    else:
        console.print(f"{display_path(md_path, runtime.mode)} is out of date")
        if drift.unsafe_to_overwrite:
            console.print(_drift_unsafe_message(drift))
            console.print(
                "Run `booktx context import-md . --write` first, or pass "
                "`--write --force-discard-md-only` to discard Markdown-only notes."
            )
        else:
            console.print("Run `booktx context render . --write` to update Markdown.")


@context_app.command(name="answer")
def context_answer(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    question_id: str = typer.Argument(..., help="Question id, e.g. Q001."),
    text: str = typer.Option(..., "--text", help="Answer text."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Answer one context question non-interactively."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    ctx = _load_context_or_exit(proj)
    for q in ctx.questions:
        if q.id == question_id:
            q.answer = text
            q.status = "answered" if text.strip() else "open"
            apply_answer_to_context(ctx, question_id, text)
            _guard_md_safe_or_die(proj, ctx)
            write_context(proj, ctx)
            write_context_markdown(proj, ctx)
            console.print(f"answered {question_id}")
            return
    _die(f"unknown question id: {question_id}")


@context_app.command(name="add-term")
def context_add_term(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    source: str = typer.Argument(..., help="Source term."),
    target: str | None = typer.Option(None, "--target", help="Approved target term."),
    forbid: list[str] | None = typer.Option(
        None, "--forbid", help="Forbidden target term (repeatable)."
    ),
    category: str = typer.Option("term", "--category", help="Glossary category."),
    notes: str = typer.Option("", "--notes", help="Glossary notes."),
    enforce: str = typer.Option(
        "warn", "--enforce", help="Enforcement: off, warn, or error."
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Add or update a glossary entry."""
    if enforce not in {"off", "warn", "error"}:
        _die("--enforce must be off, warn, or error")
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    ctx = _load_context_or_exit(proj)
    forbidden = forbid or []
    for entry in ctx.glossary:
        if entry.source == source:
            if target is not None:
                entry.target = target
                entry.status = "approved" if target else entry.status
            for value in forbidden:
                if value not in entry.forbidden_targets:
                    entry.forbidden_targets.append(value)
            entry.category = category or entry.category
            entry.notes = notes or entry.notes
            entry.enforce = enforce  # type: ignore[assignment]
            break
    else:
        ctx.glossary.append(
            GlossaryEntry(
                source=source,
                target=target,
                forbidden_targets=forbidden,
                category=category,
                status="approved" if target else "open",
                notes=notes,
                enforce=enforce,  # type: ignore[arg-type]
            )
        )
    _guard_md_safe_or_die(proj, ctx)
    write_context(proj, ctx)
    write_context_markdown(proj, ctx)
    console.print(f"updated term: {source}")


@context_app.command(name="mark-ready")
def context_mark_ready(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    force: bool = typer.Option(
        False, "--force", help="Mark ready even with open required questions."
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Mark context ready once required questions are answered."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    proj = runtime.project
    ctx = _load_context_or_exit(proj)
    open_required = _open_required_questions(ctx)
    if open_required and not force:
        ids = ", ".join(q.id for q in open_required)
        _die(f"required questions are still open: {ids}")
    ctx.ready = True
    _guard_md_safe_or_die(proj, ctx)
    write_context(proj, ctx)
    write_context_markdown(proj, ctx)
    console.print(
        f"context ready: {display_path(context_markdown_path(proj), runtime.mode)}"
    )


@context_app.command(name="import-md")
def context_import_md(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    write: bool = typer.Option(
        False, "--write", help="Write context.json and regenerate context.md."
    ),
    replace_existing: bool = typer.Option(
        False,
        "--replace-existing",
        help="Replace durable fields for conflicting chapters.",
    ),
    append_existing_lists: bool = typer.Option(
        False,
        "--append-existing-lists",
        help="Append decisions and open issues for conflicting chapters.",
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Import chapter notes from context.md into context.json.

    The recovery path for chapter notes that exist only in rendered
    Markdown. Without ``--write``, prints the chapter ids that would be
    added or changed. Default mode refuses conflicting existing chapters;
    pass ``--replace-existing`` or ``--append-existing-lists`` (mutually
    exclusive) to resolve them.
    """
    if replace_existing and append_existing_lists:
        _die("--replace-existing and --append-existing-lists are mutually exclusive")
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    proj = runtime.project
    ctx = _load_context_or_exit(proj)
    md_path = context_markdown_path(proj)
    if not md_path.is_file():
        _die("context.md is missing; nothing to import")
    try:
        imported = parse_context_markdown_chapter_notes(md_path.read_text("utf-8"))
    except ValueError as exc:
        _die(f"could not parse context.md chapter notes: {exc}")
        return
    hydrate_chapter_contexts_from_chapter_map(proj, imported)
    try:
        changed = merge_chapter_contexts(
            ctx,
            imported,
            replace_existing=replace_existing,
            append_existing_lists=append_existing_lists,
        )
    except ValueError as exc:
        _die(str(exc))
        return
    if write:
        write_context(proj, ctx)
        write_context_markdown(proj, ctx)
        if changed:
            console.print(f"updated chapters: {', '.join(changed)}")
        else:
            console.print("no chapter changes")
        console.print(f"wrote {display_path(context_path(proj), runtime.mode)}")
    else:
        if changed:
            console.print(f"would add or change chapters: {', '.join(changed)}")
        else:
            console.print("no chapter changes")
        console.print("Pass --write to update context.json.")


@context_app.command(name="chapter-note")
def context_chapter_note(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    chapter_id: str = typer.Argument(..., help="Chapter id, e.g. 0006."),
    title: str = typer.Option("", "--title", help="Chapter title."),
    source_summary: str = typer.Option("", "--source-summary", help="Source summary."),
    translation_summary: str = typer.Option(
        "", "--translation-summary", help="Translation summary."
    ),
    decision: list[str] | None = typer.Option(
        None, "--decision", help="Decision added (repeatable)."
    ),
    open_issue: list[str] | None = typer.Option(
        None, "--open-issue", help="Open issue (repeatable)."
    ),
    replace_decisions: bool = typer.Option(
        False, "--replace-decisions", help="Replace the decision list."
    ),
    replace_open_issues: bool = typer.Option(
        False, "--replace-open-issues", help="Replace the open issue list."
    ),
    force_discard_md_only: bool = typer.Option(
        False,
        "--force-discard-md-only",
        help="Overwrite despite unsafe Markdown-only notes.",
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Create or update one chapter note in context.json.

    Durable replacement for manually editing ``context.md`` after each
    completed chapter. Decisions and open issues append by default; pass
    ``--replace-decisions`` or ``--replace-open-issues`` to replace a list.
    """
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    ctx = _load_context_or_exit(proj)
    try:
        ensure_context_markdown_safe_to_overwrite(
            proj, ctx, allow_discard_md_only=force_discard_md_only
        )
    except ValueError as exc:
        _die(
            f"{exc} Pass --force-discard-md-only to overwrite anyway, or run "
            "`booktx context import-md . --write` first."
        )
        return
    note = ChapterContext(
        chapter_id=chapter_id,
        title=title,
        source_summary=source_summary,
        translation_summary=translation_summary,
        decisions_added=list(decision or []),
        open_issues=list(open_issue or []),
    )
    hydrate_chapter_contexts_from_chapter_map(proj, [note])
    upsert_chapter_context(
        ctx,
        note,
        replace_decisions=replace_decisions,
        replace_open_issues=replace_open_issues,
    )
    write_context(proj, ctx)
    write_context_markdown(proj, ctx)
    console.print(f"updated chapter note: {chapter_id}")


# --- init --------------------------------------------------------------------


@app.command()
def init(
    project_dir: Path = typer.Argument(..., help="Directory to create the project in."),
    target: str | None = typer.Option(
        None, "--target", "-t", help="Optional target language code, e.g. de."
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Optional profile name to create when --target is used."
    ),
    source_lang: str = typer.Option(
        "en",
        "--source",
        "--source-lang",
        "-s",
        help="Source language code (default: en).",
    ),
    source: Path | None = typer.Option(
        None,
        "--source-file",
        help="Optional source document to copy into <project>/source/.",
    ),
    chunk_size: int = typer.Option(
        50, "--chunk-size", help="Max records per chunk (default: 50)."
    ),
) -> None:
    """Create a new booktx project layout."""
    try:
        proj = init_project(
            project_dir,
            target_language=target or "",
            profile_name=profile,
            source_language=source_lang,
            source_file=source,
            chunk_size=chunk_size,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return

    if target:
        console.print(f"[green]Initialized source project:[/green] {proj.root}")
        console.print(f"[green]Created profile:[/green] {proj.profile}")
        console.print(f"[green]Selected active profile:[/green] {proj.profile}")
    else:
        console.print(f"[green]Initialized source project:[/green] {proj.root}")
    console.print(f"  source_language: {proj.config.source_language}")
    if proj.config.target_language:
        console.print(f"  target_language: {proj.config.target_language}")
    console.print(f"  format:          {proj.config.format}")
    if proj.config.source_file:
        console.print(f"  source_file:     {proj.config.source_file}")
    else:
        console.print(
            "  [yellow]source/ is empty — drop a .md or .epub file into it.[/yellow]"
        )


# --- inspect -----------------------------------------------------------------


@app.command()
def inspect(
    project_dir: Path = typer.Argument(..., help="Project directory."),
) -> None:
    """Summarise the source document and how many records it would yield."""
    try:
        proj = load_project(project_dir)
        source = find_source_file(proj)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return

    fmt = proj.config.format
    names = _load_names_list(proj)
    record_count, extra = _count_records(
        source, fmt, names, proj.config.source_language
    )

    table = Table(title=f"booktx inspect — {proj.root}", show_header=False)
    table.add_row("source_file", source.name)
    table.add_row("format", fmt)
    table.add_row("source_language", proj.config.source_language)
    table.add_row("target_language", proj.config.target_language)
    table.add_row("estimated_records", str(record_count))
    table.add_row("protected_terms", ", ".join(names) if names else "(none)")
    table.add_row("details", extra)
    console.print(table)


def _load_names_list(proj: Project) -> list[str]:
    from booktx.config import load_names

    return load_names(proj).protected_terms


def _count_records(
    source: Path, fmt: str, names: list[str], source_language: str
) -> tuple[int, str]:
    if fmt == "markdown":
        text = source.read_text("utf-8")
        ext = extract_markdown(text, protected_terms=names)
        spans = ext.spans
        details = f"{len(spans)} prose span(s)"
    elif fmt == "epub":
        extraction = extract_epub(str(source), protected_terms=names)
        spans = extraction.spans
        entries_raw = extraction.text2epub_manifest.get("entries", [])
        entries = entries_raw if isinstance(entries_raw, list) else []
        block_entries = [
            entry
            for entry in entries
            if isinstance(entry, dict) and entry.get("blocks")
        ]
        details = f"{len(block_entries)} spine document(s) with text blocks"
    else:  # pragma: no cover - config validation already guards this
        raise BooktxError("unsupported_format", f"Unsupported format {fmt!r}")

    from booktx.chunking import segment_spans

    records = segment_spans(spans, language=source_language)
    return len(records), details


def _chunk_json_texts(chunks: list[Chunk] | list[TranslatedChunk]) -> dict[str, str]:
    return {
        f"{chunk.chunk_id}.json": chunk.model_dump_json(indent=2) + "\n"
        for chunk in chunks
    }


def _has_accepted_store_records(proj: Project) -> bool:
    path = translation_store_path(proj)
    if not path.is_file():
        return False
    store = load_translation_store(proj)
    return any(
        any(candidate.status == "accepted" for candidate in record.versions)
        for record in store.records.values()
    )


def _same_extract_settings(
    manifest: Manifest,
    *,
    chunk_size: int,
    source_language: str,
    names_sha256: str,
) -> bool:
    return (
        manifest.chunk_size == chunk_size
        and manifest.record_id_scheme == RECORD_ID_SCHEME
        and manifest.segmenter == segmenter_metadata(source_language)
        and manifest.names_sha256 == names_sha256
    )


def _guard_extract_repeatability_and_rechunk(
    proj: Project,
    *,
    current_source_sha256: str,
    chunk_texts: dict[str, str],
    names_sha256: str,
    force_rechunk: bool,
) -> str | None:
    previous_manifest = load_manifest(proj)
    if previous_manifest is None:
        return None

    previous_source_sha256 = previous_manifest.source.sha256
    same_source = bool(previous_source_sha256) and (
        previous_source_sha256 == current_source_sha256
    )

    if (
        same_source
        and previous_manifest.record_id_scheme == RECORD_ID_SCHEME
        and previous_manifest.chunk_size != proj.config.chunk_size
        and _has_accepted_store_records(proj)
        and not force_rechunk
    ):
        _die(
            "chunk_size changed from "
            f"{previous_manifest.chunk_size} to {proj.config.chunk_size}, but this "
            f"project uses record_id_scheme={RECORD_ID_SCHEME}.\n"
            "Changing chunk_size would renumber record ids and orphan existing "
            "translation-store entries.\n"
            "Use the existing chunk_size, or run `booktx extract --force-rechunk` "
            "after backing up or migrating translations."
        )

    if same_source and _same_extract_settings(
        previous_manifest,
        chunk_size=proj.config.chunk_size,
        source_language=proj.config.source_language,
        names_sha256=names_sha256,
    ):
        existing_chunks = {
            path.name: path.read_text("utf-8")
            for path in sorted(proj.chunks(), key=lambda path: path.name)
        }
        if existing_chunks and existing_chunks != chunk_texts:
            _die(
                "repeatability violated: the same source and extraction settings "
                "did not reproduce byte-identical chunk files; refusing to replace "
                "the existing chunks."
            )

    if previous_source_sha256 and previous_source_sha256 != current_source_sha256:
        return (
            "source file changed since the previous extraction; validation may "
            "report stale translations."
        )
    return None


# --- extract -----------------------------------------------------------------


@app.command()
def extract(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    force_rechunk: bool = typer.Option(
        False,
        "--force-rechunk",
        help="Allow a risky chunk-size rechunk when chunk-local ids would be renumbered.",
    ),
) -> None:
    """Extract translatable chunks into ``.booktx/chunks/``.

    Idempotent: ``chunks/`` is rebuilt each run; ``translated/`` is left intact.
    """
    try:
        proj = load_project(project_dir)
        source = find_source_file(proj)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return

    names = _load_names_list(proj)
    fmt = proj.config.format
    if fmt == "markdown":
        text = source.read_text("utf-8")
        ext = extract_markdown(text, protected_terms=names)
        spans = ext.spans
    elif fmt == "epub":
        extraction = extract_epub(str(source), protected_terms=names)
        spans = extraction.spans
    else:  # pragma: no cover
        _die(f"Unsupported format {fmt!r}")
        return

    chunks = spans_to_chunks(
        spans,
        source_language=proj.config.source_language,
        target_language=proj.config.target_language,
        chunk_size=proj.config.chunk_size,
    )
    if fmt == "epub":
        _assert_epub_records_are_clean(chunks)

    # Idempotent rebuild of chunks/ — write into a sibling temp dir and swap
    # it in atomically so an interrupted extract never leaves a half-empty
    # .booktx/chunks/.
    import tempfile

    from booktx.epub_manifest import sha256_path as _sha256
    from booktx.io_utils import write_text_atomic

    current_source_sha256 = (
        extraction.source_sha256 if fmt == "epub" else _sha256(source)
    )
    names_sha256 = protected_terms_sha256(names)
    chunk_texts = _chunk_json_texts(chunks)
    warning_message = _guard_extract_repeatability_and_rechunk(
        proj,
        current_source_sha256=current_source_sha256,
        chunk_texts=chunk_texts,
        names_sha256=names_sha256,
        force_rechunk=force_rechunk,
    )

    proj.booktx_dir.mkdir(parents=True, exist_ok=True)
    tmp_chunks = Path(tempfile.mkdtemp(prefix=".chunks.", dir=proj.booktx_dir))
    try:
        for filename, text in chunk_texts.items():
            write_text_atomic(tmp_chunks / filename, text)
        # Remove the previous chunks dir and move the temp one into place.
        if proj.chunks_dir.exists():
            shutil.rmtree(proj.chunks_dir)
        tmp_chunks.replace(proj.chunks_dir)
    except BaseException:
        shutil.rmtree(tmp_chunks, ignore_errors=True)
        raise

    record_count = sum(len(c.records) for c in chunks)
    if fmt == "epub":
        _save_epub_manifest(proj, source, extraction, len(chunks), record_count)
    elif fmt == "markdown":
        from booktx.config import write_manifest
        from booktx.models import Manifest, ManifestSource

        write_manifest(
            proj,
            Manifest(
                version=1,
                source=ManifestSource(
                    filename=source.name,
                    format="markdown",
                    source_language=proj.config.source_language,
                    target_language=proj.config.target_language,
                    sha256=current_source_sha256,
                ),
                chunk_count=len(chunks),
                record_count=record_count,
                chunk_size=proj.config.chunk_size,
                record_id_scheme=RECORD_ID_SCHEME,
                segmenter=segmenter_metadata(proj.config.source_language),
                names_sha256=names_sha256,
            ),
        )
    console.print(
        f"[green]Extracted[/green] {len(chunks)} chunk(s), "
        f"{record_count} record(s) into {proj.chunks_dir}"
    )
    if warning_message:
        console.print(f"[yellow]warning:[/yellow] {warning_message}", soft_wrap=True)


def _assert_epub_records_are_clean(chunks: list[Chunk]) -> None:
    for chunk in chunks:
        for record in chunk.records:
            if "__TAG_" in record.source or "__SPANTX_" in record.source:
                raise BooktxError(
                    "epub_placeholders_leaked",
                    "new EPUB extraction produced TAG/SPANTX placeholders; "
                    "this is forbidden",
                )


def _save_epub_manifest(
    proj: Project,
    source: Path,
    extraction: EpubExtraction,
    chunk_count: int,
    record_count: int,
) -> None:
    """Record EPUB v2 extraction metadata in manifest.json."""
    import json

    from booktx.config import write_manifest
    from booktx.models import EpubTemplateData, Manifest, ManifestSource

    template = EpubTemplateData(
        pipeline=EPUB_TEMPLATE_PIPELINE,
        epub2text_schema=EPUB2TEXT_SCHEMA,
        text2epub_manifest=extraction.text2epub_manifest,
        spans=extraction.span_refs,
        navigation=extraction.navigation,
    )
    manifest = Manifest(
        version=2,
        source=ManifestSource(
            filename=source.name,
            format="epub",
            source_language=proj.config.source_language,
            target_language=proj.config.target_language,
            sha256=extraction.source_sha256,
        ),
        chunk_count=chunk_count,
        record_count=record_count,
        chunk_size=proj.config.chunk_size,
        record_id_scheme=RECORD_ID_SCHEME,
        segmenter=segmenter_metadata(proj.config.source_language),
        names_sha256=protected_terms_sha256(_load_names_list(proj)),
        template=template.model_dump(mode="json"),
    )
    write_manifest(proj, manifest)
    # names file convenience: keep names.json in sync if user edited it.
    _ = (json, NamesFile)  # touch imports for clarity


def _require_ready_context(
    proj: Project, *, allow_missing_context: bool = False
) -> bool:
    """Return True when context was checked and should be printed."""
    if allow_missing_context:
        return False
    ctx = load_context(proj)
    if ctx is None or not ctx.ready:
        _die("translation context is missing or not ready.\nRun: booktx context init .")
    return True


def _require_chunks(proj: Project) -> list[Path]:
    chunk_paths = proj.chunks()
    if not chunk_paths:
        _die("No source chunks found. Run: booktx extract .")
    return chunk_paths


def _require_no_source_drift(proj: Project) -> None:
    """Fail if the source file changed since the last extraction."""
    from booktx.config import current_source_sha256, extracted_source_sha256

    extracted = extracted_source_sha256(proj)
    if extracted and extracted != current_source_sha256(proj):
        _die(
            "source file has changed since last extraction; "
            "run 'booktx extract' to update chunks before translating"
        )


def _coverage_status(*, total: int, translated: int, has_error: bool) -> str:
    """Backward-compatible alias for :func:`booktx.status.coverage_status`."""
    from booktx.status import coverage_status

    return coverage_status(total=total, translated=translated, has_error=has_error)


def _format_chunk_span(chunk_ids: list[str]) -> str:
    from booktx.rendering import format_chunk_span

    return format_chunk_span(chunk_ids)


def _load_context_status(proj: Project) -> tuple[bool, bool]:
    try:
        ctx = load_context(proj)
    except Exception as exc:  # noqa: BLE001
        _die(f"translation context is invalid: {exc}")
    return (ctx is not None, bool(ctx and ctx.ready))


def _chapter_map_for_workflow(proj: Project) -> ChapterMap:
    """Refresh-and-load helper retained for direct callers outside status.py."""
    source_sha256 = project_source_sha256(proj)
    chapter_map = load_chapter_map(proj)
    if chapter_map is None or chapter_map.source_sha256 != source_sha256:
        chapter_map = detect_chapters(proj)
        write_chapter_map(proj, chapter_map)
    return chapter_map


def _project_status_snapshot(proj: Project) -> StatusBundle:
    """Build the typed status snapshot + runtime index for ``proj``.

    Thin wrapper over :func:`booktx.status.build_status_snapshot`; the CLI
    owns the invalid-context error UX here.
    """
    from booktx.status import build_status_snapshot

    context_exists, context_ready = _load_context_status(proj)
    return build_status_snapshot(
        proj, context_exists=context_exists, context_ready=context_ready
    )


def _selected_chapter(
    bundle: StatusBundle, chapter_id: str | None
) -> ChapterProgress | None:
    from booktx.status import selected_chapter

    chapter = selected_chapter(bundle, chapter_id)
    if chapter is None and chapter_id is not None:
        _die(f"unknown chapter id: {chapter_id}")
    return chapter


def _limit_records_by_words(
    record_ids: list[str], source_by_id: dict[str, Any], max_words: int
) -> list[str]:
    from booktx.tasks import limit_records_by_words

    try:
        return limit_records_by_words(record_ids, source_by_id, max_words)
    except ValueError as exc:
        _die(f"--{str(exc).replace('_', '-')}")
        raise typer.Exit(code=1) from exc


def _select_translation_record_ids(
    bundle: StatusBundle,
    chapter: ChapterProgress,
    *,
    unit: str,
    max_words: int,
) -> tuple[str, list[str]]:
    try:
        return select_translation_record_ids(
            bundle,
            chapter,
            unit=unit,
            max_words=max_words,
        )
    except ValueError as exc:
        _die(f"--{str(exc).replace('_', '-')}")
        raise typer.Exit(code=1) from exc


def _project_relative(path: Path, root: Path) -> str:
    """Backward-compatible alias for :func:`booktx.tasks.project_relative`."""
    from booktx.tasks import project_relative

    return project_relative(path, root)


def _create_translation_task(
    proj: Project,
    bundle: StatusBundle,
    chapter: ChapterProgress,
    *,
    mode: RuntimeMode | None = None,
    unit: str,
    record_ids: list[str],
    requested_max_words: int | None = None,
    todo_id: str | None = None,
) -> TranslationTask:
    """Backward-compatible alias for :func:`booktx.tasks.create_translation_task`."""
    return create_translation_task(
        proj,
        bundle,
        chapter,
        mode=mode,
        unit=unit,
        record_ids=record_ids,
        requested_max_words=requested_max_words,
        todo_id=todo_id,
    )


def _print_status_human(bundle: StatusBundle, chapter: ChapterProgress | None) -> None:
    from booktx.rendering import print_status_human

    print_status_human(bundle, chapter)


def _print_translate_task(
    task: TranslationTask,
    proj: Project,
    *,
    mode: RuntimeMode | None = None,
    as_json: bool,
    output_format: str,
    show_sources: bool = False,
    show_template: bool = False,
) -> None:
    from booktx.rendering import print_translate_task

    print_translate_task(
        task,
        proj,
        mode=mode,
        as_json=as_json,
        output_format=output_format,
        show_sources=show_sources,
        show_template=show_template,
    )


def _load_translation_task_or_exit(proj: Project, task_id: str) -> TranslationTask:
    task = load_translation_task(proj, task_id)
    if task is None:
        _die(f"unknown task id: {task_id} ({translation_task_path(proj, task_id)})")
        raise typer.Exit(code=1)
    return task


def _render_submission_failures(findings: list[Finding]) -> None:
    from booktx.rendering import render_submission_failures

    render_submission_failures(findings)


def _next_chapter(
    proj: Project,
    *,
    print_context: bool,
    mode: RuntimeMode | None = None,
) -> None:
    summary = _project_status_snapshot(proj)
    chapter = summary.snapshot.next
    if chapter is None:
        console.print("All chapter records have accepted translations.")
        raise typer.Exit(code=1)
    if print_context:
        if mode is not None:
            console.print(
                f"context: {display_path(context_markdown_path(proj), mode)}",
                soft_wrap=True,
            )
        else:
            console.print(f"context: {context_markdown_path(proj)}", soft_wrap=True)
    console.print(f"chapter: {chapter.chapter_id}  {chapter.title}".rstrip())
    console.print(f"status: {chapter.status}")
    console.print(
        f"record range: {chapter.record_range.start}..{chapter.record_range.end}"
    )
    console.print(
        f"records: {chapter.records_translated} / "
        f"{chapter.records_total} translated, "
        f"{chapter.records_remaining} remaining"
    )
    console.print(f"chunks: {_format_chunk_span(chapter.chunk_ids)}")
    console.print(f"pending chunks: {_format_chunk_span(chapter.pending_chunk_ids)}")
    console.print(f"source words remaining: {chapter.source_words_remaining:,}")
    console.print(
        "[dim]next command:[/dim] "
        + translate_next_command(proj, mode=mode, chapter_id=chapter.chapter_id)
    )
    raise typer.Exit(code=0)


# --- next --------------------------------------------------------------------


@app.command(name="status")
def status_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    chapter: str | None = typer.Option(
        None, "--chapter", help="Optional chapter id to focus the report."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit stable JSON output."),
) -> None:
    """Report record-aware translation progress."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=False)
    proj = runtime.project
    if proj.layout_version == "profiles" and proj.profile is None:
        overview = build_profiles_overview(load_source_project(proj.root))
        if as_json:
            console.print_json(
                json.dumps(overview.model_dump(mode="json"), ensure_ascii=False)
            )
            return
        _render_profiles_overview_human(overview)
        return
    _require_chunks(proj)
    summary = _project_status_snapshot(proj)
    selected = _selected_chapter(summary, chapter)
    if selected is not None:
        summary.snapshot.chapters = [selected]
        summary.snapshot.next = selected
    if runtime.mode.isolated_output:
        summary.snapshot.project = display_path(
            proj.profile_dir or proj.root, runtime.mode
        )
    if as_json:
        console.print_json(
            json.dumps(summary.snapshot.model_dump(mode="json"), ensure_ascii=False)
        )
        return
    _print_status_human(summary, selected)


@app.command(name="next")
def next_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    allow_missing_context: bool = typer.Option(
        False,
        "--allow-missing-context",
        help="Legacy override: allow next without a ready translation context.",
    ),
    unit: str = typer.Option(
        "chunk", "--unit", help="Translation unit to return: chunk or chapter."
    ),
) -> None:
    """Print the next pending legacy work item and point callers at translate/*."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    proj = runtime.project

    if unit not in {"chunk", "chapter"}:
        _die("--unit must be chunk or chapter")
    _require_chunks(proj)
    print_context = _require_ready_context(
        proj, allow_missing_context=allow_missing_context
    )
    if unit == "chapter":
        _next_chapter(proj, print_context=print_context, mode=runtime.mode)
        return
    if runtime.mode.isolated_output:
        _die(
            "booktx next is not available in profile-root isolated mode; use `booktx translate next .` instead"
        )
    summary = _project_status_snapshot(proj)
    pending_chunks = [
        chunk.chunk_id
        for chunk in summary.index.chunk_summaries
        if chunk.records_remaining > 0
    ]
    if not pending_chunks:
        console.print("All chunk records have accepted translations.")
        raise typer.Exit(code=1)
    if print_context:
        console.print(f"context: {context_markdown_path(proj)}", soft_wrap=True)
    cid = pending_chunks[0]
    chunk_path = proj.chunks_dir / f"{cid}.json"
    records_remaining = next(
        chunk.records_remaining
        for chunk in summary.index.chunk_summaries
        if chunk.chunk_id == cid
    )
    console.print(f"{cid}\t{chunk_path}", soft_wrap=True)
    console.print(f"records remaining: {records_remaining}")
    console.print("[dim]submit with:[/dim]")
    profile_part = f" --profile {proj.profile}" if proj.profile else ""
    console.print(
        f"booktx translate next {project_dir}{profile_part} --unit chunk",
        soft_wrap=True,
    )
    console.print(f"booktx translate insert .{profile_part} --stdin")
    raise typer.Exit(code=0)


# --- chapters ---------------------------------------------------------------


@app.command(name="chapters")
def chapters_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory."),
) -> None:
    """Detect and list chapter ranges."""
    try:
        proj = load_project(project_dir)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    chapter_map = detect_chapters(proj)
    write_chapter_map(proj, chapter_map)
    for chapter in chapter_map.chapters:
        chunks = ", ".join(chapter.chunk_ids)
        title = f"  {chapter.title}" if chapter.title else ""
        console.print(
            f"{chapter.chapter_id}{title}\tchunks: {chunks}\t"
            f"records: {chapter.start_record_id}..{chapter.end_record_id}"
        )


@app.command(name="next-chapter")
def next_chapter_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    allow_missing_context: bool = typer.Option(
        False,
        "--allow-missing-context",
        help="Legacy override: allow next-chapter without ready context.",
    ),
) -> None:
    """Print the next incomplete chapter and all chunks it covers."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    proj = runtime.project
    _require_chunks(proj)
    print_context = _require_ready_context(
        proj, allow_missing_context=allow_missing_context
    )
    _next_chapter(proj, print_context=print_context, mode=runtime.mode)


@translate_app.command(name="next")
def translate_next(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    chapter: str | None = typer.Option(None, "--chapter", help="Optional chapter id."),
    unit: str = typer.Option(
        "paragraph",
        "--unit",
        help="Work-unit selection: paragraph, batch, chunk, or chapter.",
    ),
    max_words: int = typer.Option(
        900,
        "--max-words",
        help="Maximum source words to return for paragraph or batch work units.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
    output_format: str = typer.Option(
        "text",
        "--format",
        help="Human output format: text, tsv, or block.",
    ),
    show_sources: bool = typer.Option(
        False,
        "--show-sources",
        help="Print source records inline (block format only).",
    ),
    show_template: bool = typer.Option(
        False,
        "--show-template",
        help="Print the heredoc submit template inline (block format only).",
    ),
    allow_missing_context: bool = typer.Option(
        False,
        "--allow-missing-context",
        help="Legacy override: allow next without a ready translation context.",
    ),
) -> None:
    """Return the next text to translate and persist a task id."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    proj = runtime.project
    if unit not in {"paragraph", "batch", "chunk", "chapter"}:
        _die("--unit must be paragraph, batch, chunk, or chapter")
    if output_format not in {"text", "tsv", "block"}:
        _die("--format must be text, tsv, or block")
    if as_json and output_format != "text":
        _die("--json cannot be combined with --format")
    _require_chunks(proj)
    _require_no_source_drift(proj)
    _require_ready_context(proj, allow_missing_context=allow_missing_context)
    summary = _project_status_snapshot(proj)
    selected_chapter = _selected_chapter(summary, chapter)
    if selected_chapter is None:
        console.print("All records already have accepted translations.")
        raise typer.Exit(code=1)
    actual_unit, record_ids = _select_translation_record_ids(
        summary,
        selected_chapter,
        unit=unit,
        max_words=max_words,
    )
    if not record_ids:
        console.print("Selected chapter has no remaining records.")
        raise typer.Exit(code=1)
    task = _create_translation_task(
        proj,
        summary,
        selected_chapter,
        mode=runtime.mode,
        unit=actual_unit,
        record_ids=record_ids,
        requested_max_words=max_words,
    )
    _print_translate_task(
        task,
        proj,
        mode=runtime.mode,
        as_json=as_json,
        output_format=output_format,
        show_sources=show_sources,
        show_template=show_template,
    )


@translate_app.command(name="insert")
def translate_insert(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    task_id: str | None = typer.Option(None, "--task-id", help="Optional task id."),
    stdin: bool = typer.Option(False, "--stdin", help="Read the payload from stdin."),
    record_id: str | None = typer.Option(None, "--record-id", help="Single record id."),
    target: str | None = typer.Option(None, "--target", help="Single target text."),
    json_file: Path | None = typer.Option(
        None,
        "--json-file",
        help="Compatibility sugar for --format json --file PATH.",
    ),
    input_file: Path | None = typer.Option(
        None,
        "--file",
        help="Read submission payload from a file using --format.",
    ),
    input_format: str = typer.Option(
        "json",
        "--format",
        help="Input format for --stdin/--file payloads: json, tsv, or block.",
    ),
    allow_missing_context: bool = typer.Option(
        False,
        "--allow-missing-context",
        help="Legacy override: allow insert without a ready translation context.",
    ),
) -> None:
    """Accept translated text through the CLI and write the store atomically."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    proj = runtime.project
    if input_format not in {"json", "tsv", "block"}:
        _die("--format must be json, tsv, or block")
    _require_chunks(proj)
    _require_no_source_drift(proj)
    _require_ready_context(proj, allow_missing_context=allow_missing_context)

    try:
        parsed = resolve_submission(
            record_id=record_id,
            target=target,
            input_format=input_format,
            stdin=stdin,
            json_file=json_file,
            input_file=input_file,
            ingest_hint=_submission_ingest_hint(proj, task_id, mode=runtime.mode),
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return

    submitted_records = parsed.records
    payload_task_id = parsed.task_id

    effective_task_id = task_id or payload_task_id
    task = (
        _load_translation_task_or_exit(proj, effective_task_id)
        if effective_task_id
        else None
    )
    summary = _project_status_snapshot(proj)
    try:
        result = accept_translation_records(
            proj,
            submitted_records,
            bundle=summary,
            task=task,
            submission_translation_version=parsed.translation_version,
            submission_profile=parsed.profile,
            enforce_task_version=True,
        )
    except SubmissionValidationError as exc:
        _render_submission_failures(exc.findings)
        raise typer.Exit(code=1) from None
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    console.print(
        f"accepted: {result.accepted_records} record(s), "
        f"{result.target_words} target word(s)"
    )
    if result.version_ref:
        console.print(f"version: {result.version_ref}")
    if result.chapter_id:
        console.print(f"chapter: {result.chapter_id} {result.chapter_title}".rstrip())
        console.print(
            f"progress: {result.records_translated} / "
            f"{result.records_total} records translated, "
            f"{result.records_remaining} remaining"
        )
        if result.records_remaining == 0:
            console.print(
                f"chapter complete: {result.chapter_id} {result.chapter_title}".rstrip()
            )
            console.print("recommended context update template:")
            console.print(
                context_chapter_note_command(
                    proj,
                    mode=runtime.mode,
                    chapter_id=result.chapter_id,
                    title=result.chapter_title or "<TITLE>",
                ),
                soft_wrap=True,
                markup=False,
            )

    # Rebuild status after insert to get fresh totals.
    fresh = _project_status_snapshot(proj)
    max_words = task.requested_max_words if task and task.requested_max_words else 800
    if task is not None and task.todo_id:
        todo = load_translation_todo(proj, task.todo_id)
        if todo is None:
            console.print(
                f"[yellow]warning:[/yellow] todo {task.todo_id} referenced by task "
                f"{task.task_id} is missing; falling back to generic next hints"
            )
        else:
            todo_status = build_todo_status(
                proj,
                todo,
                fresh,
                fail_on_warnings=False,
            )
            if todo_status.goal_complete:
                console.print(f"todo complete: {todo.todo_id}")
                console.print("next: stop - todo goal complete")
            elif todo_status.next_safe_command is not None:
                console.print(
                    "next: " + todo_status.next_safe_command,
                    soft_wrap=True,
                    markup=False,
                )
            return
    if fresh.snapshot.totals.records_remaining == 0:
        console.print(
            "next: " + build_command(proj, mode=runtime.mode),
            soft_wrap=True,
            markup=False,
        )
    elif result.chapter_id and result.records_remaining > 0:
        # Current chapter still incomplete — stay on it.
        console.print(
            "next: "
            + translate_next_command(
                proj,
                mode=runtime.mode,
                chapter_id=result.chapter_id,
                max_words=max_words,
            ),
            soft_wrap=True,
            markup=False,
        )
    elif result.chapter_id:
        # Chapter just completed — advance to next incomplete chapter.
        console.print(
            "next: "
            + translate_next_command(
                proj,
                mode=runtime.mode,
                max_words=max_words,
            ),
            soft_wrap=True,
            markup=False,
        )


@translate_app.command(name="todo-next")
def translate_todo_next(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    chapters: int = typer.Option(
        3, "--chapters", min=1, help="Number of incomplete chapters to complete."
    ),
    batch_words: int = typer.Option(
        800, "--batch-words", min=1, help="Source-word budget per translate next batch."
    ),
    max_run_words: int | None = typer.Option(
        None,
        "--max-run-words",
        min=1,
        help="Optional source-word cap for this agent run.",
    ),
    start_chapter: str | None = typer.Option(
        None,
        "--start-chapter",
        help="Optional chapter id to start from.",
    ),
    skip_current: bool = typer.Option(
        False,
        "--skip-current",
        help="Start after the current first incomplete chapter.",
    ),
    write: bool = typer.Option(
        False,
        "--write",
        help="Write todo markdown/json under translations/<profile>/todos/.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Create a durable run-control todo for a bounded multi-chapter translation run.

    This writes a todo file (not translations) describing how many chapters to
    complete, the per-task word budget, and the stop conditions.  The agent
    reads the todo and loops ``translate next -> fill -> insert -> validate``
    until done or a stop condition occurs.
    """
    from booktx.agent_todo import build_translation_todo, write_translation_todo

    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    _require_chunks(proj)
    _require_no_source_drift(proj)
    _require_ready_context(proj)
    bundle = _project_status_snapshot(proj)

    try:
        todo = build_translation_todo(
            proj,
            bundle,
            chapters=chapters,
            batch_words=batch_words,
            max_run_words=max_run_words,
            skip_current=skip_current,
            start_chapter=start_chapter,
        )
    except ValueError as exc:
        _die(str(exc))

    json_path: Path | None = None
    md_path: Path | None = None
    if write:
        json_path, md_path = write_translation_todo(proj, todo)
        # Verify the written file is loadable before printing success.
        loaded = load_translation_todo(proj, todo.todo_id)
        if loaded is None:
            _die(f"internal error: wrote todo {todo.todo_id} but could not reload it")

    if as_json:
        payload: dict[str, object] = {
            "version": 1,
            "todo_id": todo.todo_id,
            "profile": todo.profile,
            "target_language": todo.target_language,
            "target_locale": todo.target_locale,
            "chapters_requested": todo.chapters_requested,
            "batch_words": todo.batch_words,
            "max_run_words": todo.max_run_words,
            "include_current": todo.include_current,
            "created_at": todo.created_at,
            "baseline_ref": todo.baseline_ref,
            "baseline_sha256": todo.baseline_sha256,
            "context_sha256": todo.context_sha256,
            "source_sha256": todo.source_sha256,
            "chapters": [
                {
                    "chapter_id": c.chapter_id,
                    "title": c.title,
                    "status": c.status,
                    "records_total": c.records_total,
                    "records_translated_at_start": c.records_translated_at_start,
                    "records_remaining_at_start": c.records_remaining_at_start,
                    "source_words_remaining_at_start": c.source_words_remaining_at_start,
                    "pending_chunk_ids": c.pending_chunk_ids,
                }
                for c in todo.chapters
            ],
        }
        if json_path is not None:
            payload["json_path"] = str(json_path.relative_to(proj.root))
        if md_path is not None:
            payload["markdown_path"] = str(md_path.relative_to(proj.root))
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return

    # Human output
    console.print(f"todo: {todo.todo_id}")
    first = todo.chapters[0] if todo.chapters else None
    if first:
        console.print(
            f"goal: complete {todo.chapters_requested} incomplete chapter(s),"
            f" starting at {first.chapter_id} {first.title}".rstrip()
        )
    else:
        console.print(f"goal: complete {todo.chapters_requested} incomplete chapter(s)")
    console.print(f"batch words: {todo.batch_words}")
    console.print("chapters: " + ", ".join(c.chapter_id for c in todo.chapters))
    if md_path is not None:
        console.print(
            f"markdown: {md_path.relative_to(proj.root).as_posix()}",
            soft_wrap=True,
            markup=False,
        )
    if json_path is not None:
        console.print(
            f"json: {json_path.relative_to(proj.root).as_posix()}",
            soft_wrap=True,
            markup=False,
        )
    console.print(
        "next command: " + translate_todo_status_command(proj, todo_id=todo.todo_id),
        soft_wrap=True,
        markup=False,
    )
    console.print(
        "resume command: "
        + translate_todo_resume_command(
            proj,
            todo_id=todo.todo_id,
            output_format="block",
        ),
        soft_wrap=True,
        markup=False,
    )


def _print_todo_status_human(status: Any) -> None:
    chapters_display = ", ".join(chapter.chapter_id for chapter in status.todo.chapters)
    console.print(f"todo: {status.todo.todo_id}")
    console.print(
        f"goal: complete {status.todo.chapters_requested} chapter(s): {chapters_display}"
    )
    console.print(f"complete: {status.complete_count} / {len(status.chapters)}")
    console.print(f"state: {status.state}")
    console.print(f"source drift: {'yes' if status.source_drifted else 'no'}")
    console.print(f"context drift: {'yes' if status.context_drifted else 'no'}")
    validation = status.validation
    console.print(
        "validation: "
        f"errors={validation.errors} warnings={validation.warnings}"
        f"{' (blocking)' if validation.blocking else ''}"
    )
    if status.blocking_reason:
        console.print(f"reason: {status.blocking_reason}")
    if status.current_chapter is not None:
        current = status.current_chapter
        console.print(f"current: {current.chapter_id} {current.title}".rstrip())
        console.print(
            f"progress: {current.records_translated_now} / {current.records_total} "
            f"records, {current.records_remaining_now} remaining"
        )
    else:
        console.print("current: none")
    if status.next_safe_command is not None:
        console.print(
            "next: " + status.next_safe_command,
            soft_wrap=True,
            markup=False,
        )
    elif status.goal_complete:
        console.print("next: stop - todo goal complete")
    console.print("planned chapters:")
    for chapter in status.chapters:
        console.print(
            f"- {chapter.chapter_id} {chapter.title}: "
            f"{chapter.records_translated_now} / {chapter.records_total} translated, "
            f"{chapter.records_remaining_now} remaining, status={chapter.status_now}"
        )


@translate_app.command(name="todo-status")
def translate_todo_status(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    todo_id: str | None = typer.Option(None, "--todo-id", help="Todo id to inspect."),
    latest: bool = typer.Option(
        False, "--latest", help="Select the latest incomplete todo."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit stable JSON output."),
) -> None:
    """Show live bounded-run todo status and the next safe command."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    _require_chunks(proj)
    bundle = _project_status_snapshot(proj)
    try:
        todo = resolve_translation_todo(proj, bundle, todo_id=todo_id, latest=latest)
        status = build_todo_status(
            proj,
            todo,
            bundle,
            validation_report=validate_project(proj),
            fail_on_warnings=True,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    if as_json:
        console.print_json(json.dumps(status.as_dict(), ensure_ascii=False))
        return
    _print_todo_status_human(status)


@translate_app.command(name="todo-resume")
def translate_todo_resume(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    todo_id: str | None = typer.Option(None, "--todo-id", help="Todo id to resume."),
    latest: bool = typer.Option(
        False, "--latest", help="Resume the latest incomplete todo."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
    output_format: str = typer.Option(
        "block",
        "--format",
        help="Human output format: text, tsv, or block.",
    ),
    show_sources: bool = typer.Option(
        False,
        "--show-sources",
        help="Print source records inline (block format only).",
    ),
    show_template: bool = typer.Option(
        False,
        "--show-template",
        help="Print the heredoc submit template inline (block format only).",
    ),
) -> None:
    """Resume a bounded multi-chapter todo and create the next safe task."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    if output_format not in {"text", "tsv", "block"}:
        _die("--format must be text, tsv, or block")
    if as_json and output_format != "text":
        _die("--json cannot be combined with --format")
    _require_chunks(proj)
    bundle = _project_status_snapshot(proj)
    try:
        task = resume_translation_todo(proj, bundle, todo_id=todo_id, latest=latest)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    _print_translate_task(
        task,
        proj,
        as_json=as_json,
        output_format=output_format,
        show_sources=show_sources,
        show_template=show_template,
    )


@translate_app.command(name="import-legacy")
def translate_import_legacy(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Import valid legacy translated chunk files into the translation store."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    _require_chunks(proj)
    store = load_translation_store(proj)
    resolution = resolve_current_version(
        proj,
        note="Imported valid legacy translated chunks into nested translation store.",
    )
    imported_records = 0
    imported_chunks = 0
    source_chunks = {chunk.chunk_id: chunk for chunk in load_source_chunks(proj)}
    for chunk_id, source_chunk in source_chunks.items():
        if proj.translated_dir is None:
            continue
        path = proj.translated_dir / f"{chunk_id}.json"
        if not path.is_file():
            continue
        findings = validate_chunk_pair(source_chunk, path, load_context(proj))
        if any(finding.severity == Severity.ERROR for finding in findings):
            continue
        translated_chunk, err = strict_load_translated(path)
        if err is not None or translated_chunk is None:
            continue
        imported_chunks += 1
        source_records = {record.id: record for record in source_chunk.records}
        for record in translated_chunk.records:
            source_record = source_records[record.id]
            stored = ensure_store_record(
                store,
                record.id,
                source=source_record.source,
                source_sha256=source_record_sha256(source_record.source),
            )
            if active_candidate(stored) is not None:
                continue
            upsert_translation_version(
                stored,
                resolution.version_ref,
                record.target,
                updated_at=datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
            )
            imported_records += 1
    store.source_sha256 = project_source_sha256(proj)
    write_translation_store(proj, store)
    console.print(
        f"imported: {imported_records} record(s) from {imported_chunks} legacy chunk(s)"
    )


@translate_app.command(name="migrate-store")
def translate_migrate_store(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    write: bool = typer.Option(False, "--write", help="Rewrite the store as v2."),
    actor: str | None = typer.Option(
        None, "--actor", help="Actor for migrated ledger."
    ),
    harness: str | None = typer.Option(
        None, "--harness", help="Harness for migrated ledger."
    ),
    model: str | None = typer.Option(
        None, "--model", help="Model for migrated ledger."
    ),
    context_label: str | None = typer.Option(
        None,
        "--context-label",
        help="Optional label stored on the migrated subversion.",
    ),
    allow_missing_source: bool = typer.Option(
        False,
        "--allow-missing-source",
        help="Write migrated records even when some legacy ids no longer exist in source chunks.",
    ),
) -> None:
    """Inspect or rewrite a legacy translation-store.json into the v2 schema."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    path = translation_store_path(proj)
    if not path.is_file():
        _die("translation-store.json is missing")

    try:
        raw = json.loads(path.read_text("utf-8"))
        if isinstance(raw, dict) and raw.get("version") == 2:
            console.print("translation-store.json is already v2")
            return
        legacy = TranslationStore.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        _die(f"translation-store.json is invalid: {exc}")
        return

    source_records = {record.record_id: record for record in load_source_records(proj)}
    migration = migrate_legacy_store(legacy, source_records=source_records)

    if not write:
        console.print(f"dry-run: would migrate {migration.migrated_records} record(s)")
        if migration.missing_source_ids:
            console.print(
                "missing source records: " + ", ".join(migration.missing_source_ids)
            )
        return

    if actor is not None or harness is not None or model is not None:
        write_identity(
            proj,
            TranslationIdentity(
                actor=actor or "user:unknown",
                harness=harness or "booktx",
                model=model or "human",
            ),
        )

    resolution = resolve_current_version(
        proj,
        actor=actor,
        harness=harness,
        model=model,
        context_label=context_label,
        note="Migrated legacy v1 translation store to v2 nested store.",
    )
    migration = migrate_legacy_store(
        legacy,
        source_records=source_records,
        version_ref=resolution.version_ref,
    )
    if migration.missing_source_ids and not allow_missing_source:
        _die(
            "cannot migrate store with missing source records: "
            + ", ".join(migration.missing_source_ids)
        )
    write_translation_store(proj, migration.store)
    console.print(
        f"migrated: {migration.migrated_records} record(s) to store v2 at "
        f"{_project_relative(path, proj.root)}"
    )
    console.print(f"version: {resolution.version_ref}")
    console.print(
        "ledger: "
        + _project_relative(
            proj.booktx_dir / "translation-version-ledger.json", proj.root
        )
    )
    if actor is not None or harness is not None or model is not None:
        console.print(f"identity: {_project_relative(identity_path(proj), proj.root)}")


@translate_app.command(name="export")
def translate_export(  # noqa: C901
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    version_ref: str | None = typer.Option(
        None, "--version", help="Export one exact version ref such as 1.2."
    ),
    track: int | None = typer.Option(
        None,
        "--track",
        help="Export one major track, optionally with --latest-subversion.",
    ),
    latest_subversion: bool = typer.Option(
        False,
        "--latest-subversion",
        help="When exporting a track, choose the latest accepted subversion per record.",
    ),
    all_versions: bool = typer.Option(
        False,
        "--all-versions",
        help="Export all accepted versions into translated/<version-ref>/ chunk files.",
    ),
) -> None:
    """Export fully accepted store-backed chunks into translated/*.json."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    _require_chunks(proj)
    store = load_translation_store(proj)
    if all_versions and (version_ref is not None or track is not None):
        _die("--all-versions cannot be combined with --version or --track")
        return
    if track is not None and not latest_subversion:
        _die("--track currently requires --latest-subversion")
        return

    from booktx.io_utils import write_json_model_atomic

    def _pick_candidate(
        stored: StoredTranslationRecordV2,
    ) -> TranslationCandidate | None:
        if all_versions:
            return None
        if version_ref is not None:
            candidate = find_candidate(stored, version_ref)
            return (
                candidate
                if candidate is not None and candidate.status == "accepted"
                else None
            )
        if track is not None:
            matches = [
                candidate
                for candidate in stored.versions
                if candidate.version == track and candidate.status == "accepted"
            ]
            if not matches:
                return None
            return max(matches, key=lambda item: item.subversion)
        candidate = active_candidate(stored)
        return (
            candidate
            if candidate is not None and candidate.status == "accepted"
            else None
        )

    exported = 0
    if all_versions:
        version_map: dict[str, dict[str, list[TranslatedRecord]]] = {}
        for chunk in load_source_chunks(proj):
            for record in chunk.records:
                stored = store.records.get(record.id)
                if stored is None:
                    continue
                for candidate in stored.versions:
                    if candidate.status != "accepted":
                        continue
                    version_map.setdefault(candidate.version_ref, {}).setdefault(
                        chunk.chunk_id, []
                    ).append(
                        TranslatedRecord(
                            id=record.id,
                            version=candidate.version_ref,
                            target=candidate.target,
                        )
                    )
        for ref, chunks in version_map.items():
            if proj.translated_dir is None:
                continue
            export_dir = proj.translated_dir / ref
            export_dir.mkdir(parents=True, exist_ok=True)
            for chunk_id, records in chunks.items():
                write_json_model_atomic(
                    export_dir / f"{chunk_id}.json",
                    TranslatedChunk(chunk_id=chunk_id, records=records),
                )
                exported += 1
        console.print(f"exported: {exported} chunk file(s) to {proj.translated_dir}")
        return

    for chunk in load_source_chunks(proj):
        translated_records: list[TranslatedRecord] = []
        for record in chunk.records:
            stored = store.records.get(record.id)
            if stored is None:
                translated_records = []
                break
            picked = _pick_candidate(stored)
            if picked is None:
                translated_records = []
                break
            translated_records.append(
                TranslatedRecord(
                    id=record.id,
                    version=picked.version_ref,
                    target=picked.target,
                )
            )
        if not translated_records:
            continue
        translated_chunk = TranslatedChunk(
            chunk_id=chunk.chunk_id, records=translated_records
        )
        findings = []
        for source_record, translated_record in zip(
            chunk.records, translated_chunk.records, strict=True
        ):
            findings.extend(
                validate_record_pair(
                    source_record, translated_record, chunk.chunk_id, load_context(proj)
                )
            )
        if any(finding.severity == Severity.ERROR for finding in findings):
            continue
        if proj.translated_dir is None:
            continue
        write_json_model_atomic(
            proj.translated_dir / f"{chunk.chunk_id}.json", translated_chunk
        )
        exported += 1
    console.print(f"exported: {exported} chunk(s) to {proj.translated_dir}")


@translate_app.command(name="task-status")
def translate_task_status(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    task_id: str = typer.Option(..., "--task-id", help="Task id to inspect."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Report accepted vs missing progress for one persisted translation task.

    Makes interrupted translation runs diagnosable without inspecting the store
    by hand. Exits 0 only when every task record is accepted and current.
    """
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    _require_chunks(proj)
    task = _load_translation_task_or_exit(proj, task_id)
    store = load_translation_store(proj)

    accepted_ids: list[str] = []
    missing_ids: list[str] = []
    stale_ids: list[str] = []
    for record in task.records:
        stored = store.records.get(record.id)
        if stored is None:
            missing_ids.append(record.id)
            continue
        expected_sha = source_record_sha256(record.source)
        if stored.source_sha256 and stored.source_sha256 != expected_sha:
            stale_ids.append(record.id)
            continue
        candidate = active_candidate(stored)
        if candidate is None or candidate.status != "accepted":
            missing_ids.append(record.id)
            continue
        accepted_ids.append(record.id)

    total = len(task.records)
    accepted = len(accepted_ids)
    not_current = total - accepted
    first_missing = (
        missing_ids[0] if missing_ids else (stale_ids[0] if stale_ids else None)
    )
    complete = not_current == 0

    source_display = _project_relative(
        translation_task_source_block_path(proj, task.task_id), proj.root
    )
    block_ingest_display = _project_relative(
        translation_ingest_block_path(proj, task.task_id), proj.root
    )
    json_ingest_display = _project_relative(
        translation_ingest_path(proj, task.task_id), proj.root
    )
    from booktx.command_hints import translate_insert_command

    submit_hint = translate_insert_command(
        proj,
        task_id=task.task_id,
        file_path=block_ingest_display,
        input_format="block",
    )

    payload = {
        "version": 1,
        "task_id": task.task_id,
        "chapter_id": task.chapter_id,
        "chapter_title": task.chapter_title,
        "records_total": total,
        "records_accepted": accepted,
        "records_missing": len(missing_ids),
        "records_stale": len(stale_ids),
        "missing_ids": missing_ids,
        "stale_ids": stale_ids,
        "first_missing": first_missing,
        "complete": complete,
        "source_block_path": source_display,
        "block_ingest_path": block_ingest_display,
        "json_ingest_path": json_ingest_display,
        "submit_hint": submit_hint,
    }
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        raise typer.Exit(code=0 if complete else 1)

    console.print(f"task: {task.task_id}")
    console.print(f"chapter: {task.chapter_id}  {task.chapter_title}".rstrip())
    console.print(f"records: {accepted} / {total} accepted, {not_current} missing")
    if stale_ids:
        console.print(f"stale: {len(stale_ids)} record(s) need re-translation")
    if first_missing is not None:
        console.print(f"first missing: {first_missing}")
    console.print(f"source file: {source_display}", soft_wrap=True, markup=False)
    console.print(f"ingest file: {block_ingest_display}", soft_wrap=True, markup=False)
    console.print(f"submit: {submit_hint}", soft_wrap=True, markup=False)
    raise typer.Exit(code=0 if complete else 1)


@translate_app.command(name="get-record")
def translation_get_record(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    record_ref: str = typer.Argument(..., help="Record ref such as 74@38."),
    before: int = typer.Option(0, "--before", min=0, help="Neighbor records before."),
    after: int = typer.Option(0, "--after", min=0, help="Neighbor records after."),
    version: str | None = typer.Option(
        None, "--version", help="Show one specific version."
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Inspect one source record with nearby context and available versions."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    selected, details = _store_record_payload(proj, record_ref)
    ordered = details["ordered"]
    ordered_ids = [record.record_id for record in ordered]
    selected_id = selected["id"]
    try:
        index = ordered_ids.index(selected_id)
    except ValueError:
        _die(f"unknown source record id: {selected_id}")
        return

    store = details["store"]

    def _record_payload(source_record: SourceRecordView) -> dict[str, Any]:
        payload = {
            "id": source_record.record_id,
            "chunk_id": source_record.chunk_id,
            "source": source_record.source,
        }
        stored = store.records.get(source_record.record_id)
        if stored is not None:
            payload["active_version"] = stored.active_version
            candidate = (
                find_candidate(stored, version)
                if version is not None
                else active_candidate(stored)
            )
            if candidate is not None:
                payload["target"] = candidate.target
                payload["status"] = candidate.status
                payload["version_ref"] = candidate.version_ref
        return payload

    before_records = [
        _record_payload(record) for record in ordered[max(0, index - before) : index]
    ]
    selected_payload = _record_payload(ordered[index])
    selected_payload["available_targets"] = details["versions"]
    selected_payload["ledger_metadata"] = _ledger_metadata_for_version(
        proj, version or selected_payload.get("active_version")
    )
    after_records = [
        _record_payload(record) for record in ordered[index + 1 : index + 1 + after]
    ]
    payload = {
        "selected_record_ref": selected_id,
        "before": before_records,
        "selected": selected_payload,
        "after": after_records,
        "available_targets": details["versions"],
        "active_version": selected_payload.get("active_version"),
        "ledger_metadata": selected_payload["ledger_metadata"],
    }
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    for item in before_records:
        console.print(f"   {item['id']}  {item['source']}")
    console.print(f">> {selected_id}  {selected_payload['source']}")
    for candidate in details["versions"]:
        console.print(
            f"   {candidate['version_ref']} [{candidate['status']}] {candidate['target']}"
        )
    for item in after_records:
        console.print(f"   {item['id']}  {item['source']}")


@translate_app.command(name="list")
def translation_list(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    range_spec: str | None = typer.Option(
        None, "--range", help="Range spec such as 74@38..74@42."
    ),
    chapter: int | None = typer.Option(None, "--chapter", help="Chapter number."),
    version: str | None = typer.Option(
        None, "--version", help="Show a specific version."
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """List records for a range or chapter in source reading order."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    if (range_spec is None) == (chapter is None):
        _die("use exactly one of --range or --chapter")
        return
    ordered = _ordered_source_records(proj)
    ordered_ids = [record.record_id for record in ordered]
    ctx = load_context(proj)
    bundle = build_status_snapshot(
        proj,
        context_exists=ctx is not None,
        context_ready=bool(ctx and ctx.ready),
    )
    spec = range_spec if range_spec is not None else f"chapter:{chapter}"
    try:
        selected_ids = resolve_record_range(
            spec,
            ordered_record_ids=ordered_ids,
            chapter_record_ids=bundle.index.record_ids_by_chapter,
        )
    except ValueError as exc:
        _die(str(exc))
        return
    store = load_translation_store(proj)
    payload: list[dict[str, Any]] = []
    for record in ordered:
        if record.record_id not in selected_ids:
            continue
        item = {
            "id": record.record_id,
            "chunk_id": record.chunk_id,
            "source": record.source,
        }
        stored = store.records.get(record.record_id)
        if stored is not None:
            if stored.active_version is not None:
                item["active_version"] = stored.active_version
            candidate = (
                find_candidate(stored, version)
                if version is not None
                else active_candidate(stored)
            )
            if candidate is not None:
                item["target"] = candidate.target
                item["status"] = candidate.status
                item["version_ref"] = candidate.version_ref
        payload.append(item)
    if as_json:
        console.print_json(json.dumps({"records": payload}, ensure_ascii=False))
        return
    for item in payload:
        suffix = f" [{item['version_ref']}]" if "version_ref" in item else ""
        console.print(f"{item['id']}{suffix}  {item['source']}")


@translate_app.command(name="compare")
def translation_compare(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    record_ref: str = typer.Argument(..., help="Record ref such as 74@38."),
    versions: str = typer.Option(
        ..., "--versions", help="Comma-separated version refs."
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Compare multiple stored version candidates for one record."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    selected, details = _store_record_payload(proj, record_ref)
    store = details["store"]
    stored = store.records.get(selected["id"])
    if stored is None:
        _die(f"record {selected['id']} has no stored translations")
        return
    requested = [item.strip() for item in versions.split(",") if item.strip()]
    payload = {"record_ref": selected["id"], "comparisons": []}
    for version_ref in requested:
        candidate = find_candidate(stored, version_ref)
        payload["comparisons"].append(
            {
                "version_ref": version_ref,
                "target": candidate.target if candidate is not None else None,
                "status": candidate.status if candidate is not None else None,
            }
        )
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    for item in payload["comparisons"]:
        console.print(f"{item['version_ref']}: {item['target'] or '<missing>'}")


@translate_app.command(name="activate")
def translation_activate(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    record_ref: str = typer.Argument(..., help="Record ref such as 74@38."),
    version_ref: str = typer.Argument(..., help="Version ref to activate."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Activate one stored candidate version for a single record."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    store = load_translation_store(proj)
    record_id = parse_record_ref(record_ref).canonical_id
    stored = store.records.get(record_id)
    if stored is None:
        _die(f"record {record_id} has no stored translations")
        return
    candidate = find_candidate(stored, version_ref)
    if candidate is None:
        _die(f"record {record_id} has no version {version_ref}")
        return
    stored.active_version = candidate.version_ref
    write_translation_store(proj, store)
    console.print(f"{record_id} -> {candidate.version_ref}")


@translate_app.command(name="review")
def translation_review(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    record_ref: str = typer.Argument(..., help="Record ref such as 74@38."),
    activate: str | None = typer.Option(
        None, "--activate", help="Optionally activate a version."
    ),
    note: str | None = typer.Option(None, "--note", help="Review note."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Review one stored candidate and optionally activate it."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    store = load_translation_store(proj)
    record_id = parse_record_ref(record_ref).canonical_id
    stored = store.records.get(record_id)
    if stored is None:
        _die(f"record {record_id} has no stored translations")
        return
    candidate = (
        find_candidate(stored, activate)
        if activate is not None
        else active_candidate(stored)
    )
    if candidate is None:
        _die(f"record {record_id} has no matching review target")
        return
    if activate is not None:
        stored.active_version = candidate.version_ref
    candidate.reviewed_at = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    candidate.reviewed_by = _resolved_identity(proj).actor
    candidate.review_note = note
    write_translation_store(proj, store)
    console.print(f"{record_id} reviewed {candidate.version_ref}")


@translate_app.command(name="set-record")
def translate_set_record(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    task_id: str = typer.Option(..., "--task-id", help="Task id owning the record."),
    record_id: str = typer.Option(..., "--record-id", help="Record id to set."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    stdin: bool = typer.Option(
        False,
        "--stdin",
        help="Read the target text from stdin (default source).",
    ),
    target: str | None = typer.Option(None, "--target", help="Inline target text."),
    allow_missing_context: bool = typer.Option(
        False,
        "--allow-missing-context",
        help="Legacy override: allow set-record without a ready context.",
    ),
) -> None:
    """Commit a single translated record from stdin (or --target).

    Lets an agent safely commit one record at a time so work already written to
    translation-store.json survives interruption. Prefer this over embedding a
    whole chapter section in one shell command when truncation is a concern.
    """
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    _require_chunks(proj)
    _require_ready_context(proj, allow_missing_context=allow_missing_context)
    task = _load_translation_task_or_exit(proj, task_id)
    if record_id not in {record.id for record in task.records}:
        _die(f"record {record_id} is not part of task {task.task_id}")

    if target is not None:
        target_text = target
    elif stdin:
        target_text = sys.stdin.read()
        # Drop a single trailing newline (common shell/heredoc artifact) while
        # preserving all internal multiline text.
        if target_text.endswith("\r\n"):
            target_text = target_text[:-2]
        elif target_text.endswith("\n"):
            target_text = target_text[:-1]
    else:
        _die("provide the target text with --stdin or --target")

    summary = _project_status_snapshot(proj)
    try:
        result = accept_one_record(
            proj,
            record_id,
            target_text,
            bundle=summary,
            task=task,
            submission_profile=task.profile or None,
        )
    except SubmissionValidationError as exc:
        _render_submission_failures(exc.findings)
        raise typer.Exit(code=1) from None
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    console.print(f"accepted: 1 record, {result.target_words} target word(s)")
    if result.chapter_id:
        console.print(f"chapter: {result.chapter_id} {result.chapter_title}".rstrip())
        console.print(
            f"progress: {result.records_translated} / "
            f"{result.records_total} records translated, "
            f"{result.records_remaining} remaining"
        )


# --- validate ----------------------------------------------------------------


@app.command()
def validate(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    all_versions_strict: bool = typer.Option(
        False,
        "--all-versions-strict",
        help="Treat inactive-version validation errors as fatal.",
    ),
    fail_on_warnings: bool = typer.Option(
        False,
        "--fail-on-warnings",
        help="Exit non-zero when validation reports warnings.",
    ),
) -> None:
    """Validate translated chunks against the translation contract."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    proj = runtime.project

    report = validate_project(proj, all_versions_strict=all_versions_strict)
    out = write_report(proj, report)

    if report.findings:
        for f in report.findings:
            color = "red" if f.severity == "error" else "yellow"
            loc = f" [{f.record_id}]" if f.record_id else ""
            console.print(
                f"[{color}]{f.severity}[/{color}] {f.chunk_id}{loc} "
                f"{f.rule}: {f.message}"
            )
    console.print(
        f"chunks_checked={report.chunks_checked} "
        f"passed={report.chunks_passed} "
        f"errors={len(report.errors)} warnings={len(report.warnings)} "
        f"missing={report.chunks_missing_translation}"
    )
    console.print("[dim]report:[/dim] ", end="")
    console.print(display_path(out, runtime.mode), soft_wrap=True, markup=False)
    if not report.passed or (fail_on_warnings and report.warnings):
        raise typer.Exit(code=1)


# --- build -------------------------------------------------------------------


@app.command()
def build(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    require_complete: bool = typer.Option(
        False,
        "--require-complete",
        help="Fail when any record is untranslated or invalid.",
    ),
) -> None:
    """Rebuild the translated document into ``output/``."""
    try:
        runtime = _load_runtime_or_exit(
            project_dir, profile=profile, require_profile=True
        )
        proj = runtime.project
        result = build_project(proj, require_complete=require_complete)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    except BuildError as exc:
        _die(str(exc))
        return

    console.print(
        f"[green]Built[/green] {result.format} -> "
        f"{display_path(result.output_path, runtime.mode)}"
    )
    if result.report:
        changed_entries = result.report.get("changed_entries", [])
        console.print(
            "  changed_entries="
            f"{_changed_entry_count(changed_entries)} "
            f"replacements={result.report.get('replacement_count', 0)} "
            f"unresolved_tokens={result.report.get('unresolved_token_count', 0)}"
        )


@app.command(name="pass-through")
def pass_through_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str = typer.Option(..., "--profile", help="Pass-through profile name."),
    create: bool = typer.Option(
        False, "--create", help="Create the pass-through profile if missing."
    ),
    select: bool = typer.Option(False, "--select", help="Select the created profile."),
    output_filename: str | None = typer.Option(
        None, "--output-filename", help="Output filename for a newly created profile."
    ),
    force: bool = typer.Option(
        True, "--force/--no-force", help="Refresh existing generated translated chunks."
    ),
    prune_stale: bool = typer.Option(
        True,
        "--prune-stale/--keep-stale",
        help="Remove stale generated translated chunks.",
    ),
    clear_store: bool = typer.Option(
        False,
        "--clear-store",
        help="Clear store records that would override generated chunks.",
    ),
    no_build: bool = typer.Option(
        False, "--no-build", help="Only generate and validate translated chunks."
    ),
    allow_warnings: bool = typer.Option(
        False, "--allow-warnings", help="Do not fail on validation warnings."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Generate identity translated chunks, validate coverage, and rebuild output.

    This produces a source-as-target reconstruction fixture. It is not a real
    translation: each record target equals its source text. Compare the output
    against the source with a diff viewer to detect reconstruction drift.
    """
    runtime = _load_runtime_or_exit(project_dir, require_profile=False)
    _reject_if_isolated(runtime)
    try:
        proj = ensure_pass_through_profile(
            runtime.project.root,
            profile,
            create=create,
            select=select,
            output_filename=output_filename,
        )
        result = run_pass_through(
            proj,
            force=force,
            prune_stale=prune_stale,
            clear_store=clear_store,
            build=not no_build,
            allow_warnings=allow_warnings,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    except BuildError as exc:
        _die(str(exc))
        return

    payload = {
        "profile": result.profile,
        "chunks_written": result.chunks_written,
        "records_written": result.records_written,
        "stale_removed": result.stale_removed,
        "translated_dir": str(result.translated_dir),
        "validation": {
            "errors": len(result.validation_report.errors),
            "warnings": len(result.validation_report.warnings),
            "missing": result.validation_report.chunks_missing_translation,
        },
        "output_path": str(result.build_result.output_path)
        if result.build_result
        else None,
        "format": result.build_result.format if result.build_result else None,
    }
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return

    console.print(f"pass-through profile: {result.profile}")
    console.print(f"translated chunks: {result.chunks_written}")
    console.print(f"translated records: {result.records_written}")
    console.print(f"removed stale translated chunks: {result.stale_removed}")
    console.print(
        "validation: passed "
        f"errors={len(result.validation_report.errors)} "
        f"warnings={len(result.validation_report.warnings)} "
        f"missing={result.validation_report.chunks_missing_translation}"
    )
    if result.build_result is not None:
        console.print(
            f"[green]Built[/green] {result.build_result.format} -> "
            f"{result.build_result.output_path}"
        )


def _changed_entry_count(changed_entries: object) -> int | object:
    if isinstance(changed_entries, list):
        return len(changed_entries)
    return changed_entries


# --- top-level callback (version) --------------------------------------------


@app.callback()
def _main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Print booktx version and exit.",
        is_eager=True,
    ),
) -> None:
    """booktx root options."""
    if version:
        console.print(__version__)
        raise typer.Exit


def main() -> None:
    """Console-script entry point (used by pyproject [project.scripts])."""
    # Typer raises typer.Exit for normal command exits; surface its code.
    try:
        app()
    except typer.Exit as exc:
        sys.exit(exc.exit_code)


__all__ = ["app", "main"]
