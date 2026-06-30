"""Shared CLI-layer helpers used by ``booktx/cli.py`` and the per-slice
command modules under ``booktx/commands/``.

This module exists to break the import cycle that would otherwise arise when
command modules need the CLI helpers (console, error-to-exit mapping, project
loading, legacy arg parsing) while ``booktx/cli.py`` imports the command
modules to register them. By factoring the shared helpers into this neutral
module:

- ``booktx/cli.py`` imports them at the top and imports command modules at
  the top (no cycle, no ``E402``).
- ``booktx/commands/*.py`` import them from here (never from ``booktx.cli``).

The boundary guard in ``tests/test_cli_command_boundary.py`` only scans
``booktx/commands/``; this module may import ``booktx.config`` /
``booktx.runtime`` / ``booktx.identity`` freely because it is *not* a command
module. Workflow functions under ``booktx/workflows/`` own the actual
mutations; the helpers here only do CLI concerns: rendering, exit-code
mapping, and runtime/project loading.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console

from booktx.command_hints import (
    translate_next_command,
)
from booktx.config import (
    Project,
    load_translation_store,
    load_translation_task,
    load_translation_version_ledger,
    translation_task_path,
)
from booktx.context import (
    context_markdown_path,
    load_context,
    unapproved_required_questions,
    unresolved_required_questions,
)
from booktx.errors import BooktxError, _err
from booktx.identity import identity_payload
from booktx.models import TranslationIdentity
from booktx.path_display import display_path
from booktx.progress import SourceRecordView, load_source_records
from booktx.record_refs import parse_record_ref
from booktx.rendering import (
    print_status_human,
    print_translate_task,
)
from booktx.runtime import RuntimeContext, RuntimeMode, resolve_runtime

# Additional imports needed by the helpers moved from cli.py (slice 6/7).
from booktx.status import (
    ChapterProgress,
    StatusBundle,
    build_status_snapshot,
    coverage_status,
    selected_chapter,
)
from booktx.tasks import (
    create_translation_task,
    limit_records_by_words,
    project_relative,
    select_translation_record_ids,
)
from booktx.validate import (
    Finding,
    Severity,
    ValidationReport,
)
from booktx.versioning import lookup_version, resolve_identity

if TYPE_CHECKING:
    from collections.abc import Callable

    from booktx.acceptance import SubmittedRecord
    from booktx.editor_indexes import EditorIndexesResult
    from booktx.models import TranslationTask
    from booktx.status import ChapterProgress, ProfilesOverview, StatusBundle
    from booktx.validate import Finding
    # ``Project`` lives in ``booktx.config``; imported under TYPE_CHECKING so
    # this module never imports config at runtime (keeps import order simple).

# Shared console instance for all CLI output.
console = Console()


def _isolated_mode_error() -> str:
    return (
        "command is not available in profile-root isolated mode.\n"
        "Run this from the project root for collaborative/admin workflows."
    )


def _reject_if_isolated(runtime: RuntimeContext) -> None:
    if runtime.mode.isolated_output:
        _die(_isolated_mode_error())


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


def _load_context_status(proj: Project) -> tuple[bool, bool]:
    try:
        ctx = load_context(proj)
    except Exception as exc:  # noqa: BLE001
        _die(f"translation context is invalid: {exc}")
    return (ctx is not None, bool(ctx and ctx.ready))


def _project_status_snapshot(proj: Project) -> StatusBundle:
    """Build the typed status snapshot + runtime index for ``proj``.

    Thin wrapper over :func:`booktx.status.build_status_snapshot`; the CLI
    owns the invalid-context error UX here.
    """

    context_exists, context_ready = _load_context_status(proj)
    return build_status_snapshot(
        proj, context_exists=context_exists, context_ready=context_ready
    )


def _die(message: str, code: int = 1) -> None:
    """Print an error and exit with ``code``."""
    console.print(f"[red]error:[/red] {message}")
    raise typer.Exit(code=code)


def _handle_booktx_error(exc: BooktxError) -> None:
    _die(str(exc))


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


# --- shared CLI-layer guards and rendering helpers -------------------------
# These helpers were factored out of ``booktx/cli.py`` so that the per-slice
# command modules under ``booktx/commands/`` can import them without creating a
# cycle (commands cannot import ``booktx.cli``). They wrap CLI concerns:
# validation-to-exit mapping, console rendering, and runtime-aware output.


def _maybe_auto_export_indexes(
    proj: Project, *, export_index: bool = False, trigger: str = ""
) -> None:
    """Auto-export editor indexes after accepted changes if configured."""
    from booktx.editor_indexes import export_editor_indexes

    cfg = proj.profile_config
    if cfg is None:
        return
    indexes_cfg = cfg.indexes
    if indexes_cfg is None and not export_index:
        return

    should_export = export_index
    if indexes_cfg is not None:
        if trigger == "review" and indexes_cfg.auto_export_after_review:
            should_export = True
        elif trigger == "translation" and indexes_cfg.auto_export_after_insert:
            should_export = True

    if not should_export:
        return

    try:
        result = export_editor_indexes(
            proj,
            write_jsonl=indexes_cfg.write_jsonl if indexes_cfg is not None else False,
        )
        console.print(
            f"indexes: exported {result.translated_count} translated, "
            f"{result.missing_count} missing",
        )
    except Exception as exc:
        # Non-fatal: don't block the main operation because of index export.
        console.print(f"[yellow]warning:[/yellow] index export failed: {exc}")


def _require_ready_context(
    proj: Project, *, allow_missing_context: bool = False
) -> bool:
    """Return True when context was checked and should be printed."""
    if allow_missing_context:
        return False
    ctx = load_context(proj)
    if ctx is None or not ctx.ready:
        _die("translation context is missing or not ready.\nRun: booktx context init .")
        return False
    unresolved = unresolved_required_questions(ctx)
    if unresolved and not ctx.ready_forced:
        ids = ", ".join(q.id for q in unresolved)
        _die(
            f"translation context has unapproved required answers: {ids}\n"
            "Run: booktx context questionnaire . and approve "
            "answers before translating."
        )
    unapproved = unapproved_required_questions(ctx)
    if unapproved and not ctx.ready_forced:
        ids = ", ".join(q.id for q in unapproved)
        _die(
            f"translation context has unapproved required answers: {ids}\n"
            "Run: booktx context questionnaire . and approve "
            "answers before translating."
        )
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


def _selected_chapter(
    bundle: StatusBundle, chapter_id: str | None
) -> ChapterProgress | None:

    chapter = selected_chapter(bundle, chapter_id)
    if chapter is None and chapter_id is not None:
        _die(f"unknown chapter id: {chapter_id}")
    return chapter


def _project_relative(path: Path, root: Path) -> str:
    """Backward-compatible alias for :func:`booktx.tasks.project_relative`."""

    return project_relative(path, root)


def _render_submission_failures(findings: list[Finding]) -> None:
    from booktx.rendering import render_submission_failures

    render_submission_failures(findings)


def _truncate(text: str, limit: int = 120) -> str:
    """Return a single-line excerpt of ``text``, truncated for display."""
    one_line = " ".join(text.split())
    if len(one_line) <= limit:
        return one_line
    return one_line[:limit].rstrip() + "\u2026"


def _render_finding(f: Finding) -> None:
    color = "red" if f.severity == "error" else "yellow"
    if f.record_id:
        loc = f" [{f.record_id}]"
    elif f.record_ids:
        loc = f" records={f.record_ids}"
    else:
        loc = ""
    scope_marker = ""
    if f.candidate_scope == "inactive" and f.candidate_ref:
        kind = f.candidate_kind or "translation"
        scope_marker = f" [{kind} {f.candidate_ref}]"
    console.print(
        f"[{color}]{f.severity}[/{color}] {f.chunk_id}{loc} "
        f"{f.rule}{scope_marker}: {f.message}"
    )
    if f.chapter_id:
        title = f" {f.chapter_title}".rstrip() if f.chapter_title else ""
        console.print(f"  chapter: {f.chapter_id}{title}")
    span_parts = []
    if f.span_index is not None:
        span_parts.append(f"span={f.span_index}")
    if f.block_id:
        span_parts.append(f"block={f.block_id}")
    if span_parts:
        console.print(f"  {' '.join(span_parts)}")
    if f.document_href:
        console.print(f"  href: {f.document_href}")


def _staged_preflight_check(
    proj: Project,
    submitted_records: list[SubmittedRecord],
    submitted_ids: set[str],
    *,
    fail_on_warnings: bool = False,
) -> None:
    """Run EPUB inline-XHTML preflight on staged submitted records.

    Layers submitted records on top of current effective translations and runs
    the preflight (via :mod:`booktx.acceptance_preflight`). If inline-XHTML
    errors (or, when ``fail_on_warnings=True``, warnings) are found, renders
    them and exits non-zero BEFORE the store is written.
    """
    from booktx.acceptance_preflight import run_staged_preflight
    from booktx.validate import Finding

    blocking = run_staged_preflight(
        proj,
        submitted_records,
        submitted_ids,
        fail_on_warnings=fail_on_warnings,
    )
    if not blocking:
        return
    for f in blocking:
        _render_finding(
            Finding(
                chunk_id=f.chunk_id or "epub-preflight",
                severity=f.severity,
                rule=f.rule,
                message=f.message,
                record_id=f.record_id,
                record_ids=list(f.record_ids),
                chapter_id=f.chapter_id,
                chapter_title=f.chapter_title,
                span_index=f.span_index,
                block_id=f.block_id,
                document_href=f.document_href,
                source=f.source,
                target=f.target,
            )
        )
        fix_record = f.record_id or (f.record_ids[0] if f.record_ids else "")
        if fix_record:
            console.print(
                f"  fix: booktx translation revise-record . {fix_record} --stdin",
                soft_wrap=True,
                markup=False,
            )
    raise typer.Exit(code=1)


# --- additional CLI helpers moved from cli.py (slice 6/7) ---------------

def _display_path(path: Path, mode: RuntimeMode | None) -> str:
    if mode is not None:
        return display_path(path, mode)
    return path.as_posix()


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


def _coverage_status(*, total: int, translated: int, has_error: bool) -> str:
    """Backward-compatible alias for :func:`booktx.status.coverage_status`."""

    return coverage_status(total=total, translated=translated, has_error=has_error)


def _format_chunk_span(chunk_ids: list[str]) -> str:
    from booktx.rendering import format_chunk_span

    return format_chunk_span(chunk_ids)


def _render_epub_audit_summary(audit: Any) -> None:
    """Print a recomputed EPUB chapter-audit summary when findings exist."""
    if audit is None or not getattr(audit, "findings", None):
        return
    color = "red" if audit.has_blocking_errors else "yellow"
    label = "error" if audit.has_blocking_errors else "warning"
    console.print(
        f"[{color}]{label}:[/{color}] EPUB chapter audit: "
        f"{audit.error_count} error(s), {audit.warning_count} warning(s) "
        f"(visible TOC vs extracted chapters).",
        soft_wrap=True,
    )
    console.print("[dim]details: booktx chapters . --audit[/dim]", soft_wrap=True)


def _block_on_epub_audit_errors(bundle: StatusBundle) -> None:
    """Refuse new work selection when the recomputed EPUB audit has errors.

    Warnings (preview/truncated EPUBs) stay non-blocking; only ``error`` findings
    such as ``epub_toc_href_extracted_but_unmapped`` block. The audit is always
    recomputed (``StatusBundle.epub_audit``), never read from a persisted report.
    """
    audit = getattr(bundle, "epub_audit", None)
    if audit is None or not audit.has_blocking_errors:
        return
    errors = [f for f in audit.findings if f.severity == "error"]
    preview = "; ".join(f"{f.code}: {f.message}" for f in errors[:3])
    suffix = "" if len(errors) <= 3 else f" ...(+{len(errors) - 3} more)"
    _die(
        f"EPUB chapter audit reports {len(errors)} blocking error(s); refusing to "
        f"select new work until resolved. {preview}{suffix}\n"
        "Inspect: booktx chapters . --audit"
    )


def _limit_records_by_words(
    record_ids: list[str], source_by_id: dict[str, Any], max_words: int
) -> list[str]:

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

    print_status_human(bundle, chapter)
    _render_epub_audit_summary(getattr(bundle, "epub_audit", None))


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


def _next_chapter(
    proj: Project,
    *,
    print_context: bool,
    mode: RuntimeMode | None = None,
) -> None:
    summary = _project_status_snapshot(proj)
    _block_on_epub_audit_errors(summary)
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


def _editor_index_summary(
    result: EditorIndexesResult, display: Callable[[str | None], str | None]
) -> dict[str, Any]:
    return {
        "source_path": display(result.source_path),
        "target_path": display(result.target_path),
        "source_target_path": display(result.source_target_path),
        "source_record_count": result.source_record_count,
        "target_record_count": result.target_record_count,
        "source_target_record_count": result.source_target_record_count,
        "translated_count": result.translated_count,
        "missing_count": result.missing_count,
        "warning_count": result.warning_count,
        "error_count": result.error_count,
        "written": list(result.written),
    }


def _render_validate_findings(report: ValidationReport) -> None:
    if not report.findings:
        return
    for f in report.findings:
        _render_finding(f)


def _epub_output_audit_findings(
    proj: Project,
) -> tuple[list[Finding], dict[str, object]]:
    """Non-writing audit of the expected EPUB output path.

    Returns validation-style findings plus a JSON payload. Errors clearly when
    no output exists or the project is not an EPUB project.
    """
    from booktx.build import _output_path
    from booktx.config import find_source_file
    from booktx.epub_output_policy import (
        PolicyError,
        audit_epub_output_policy,
        resolve_epub_output_policy,
    )

    findings: list[Finding] = []
    if proj.config.format != "epub":
        findings.append(
            Finding(
                chunk_id="epub_output",
                severity=Severity.ERROR,
                rule="not_an_epub_project",
                message="--epub-output is only valid for EPUB projects.",
            )
        )
        return findings, {"findings": [f.as_dict() for f in findings]}

    try:
        source = find_source_file(proj, persist_discovery=False)
    except Exception as exc:  # noqa: BLE001
        findings.append(
            Finding(
                chunk_id="epub_output",
                severity=Severity.ERROR,
                rule="source_not_found",
                message=str(exc),
            )
        )
        return findings, {"findings": [f.as_dict() for f in findings]}

    out_path = _output_path(proj, source, suffix=".epub")
    payload: dict[str, object] = {"output_path": str(out_path)}
    if not out_path.is_file():
        findings.append(
            Finding(
                chunk_id="epub_output",
                severity=Severity.ERROR,
                rule="epub_output_missing",
                message=(
                    f"no built EPUB output found at {out_path}; "
                    "run `booktx build` first."
                ),
            )
        )
        payload["findings"] = [f.as_dict() for f in findings]
        return findings, payload

    try:
        policy = resolve_epub_output_policy(proj)
        report = audit_epub_output_policy(out_path, extraction_hrefs=[], policy=policy)
    except PolicyError as exc:
        findings.append(
            Finding(
                chunk_id="epub_output",
                severity=Severity.ERROR,
                rule="epub_output_audit_failed",
                message=str(exc),
            )
        )
        payload["findings"] = [f.as_dict() for f in findings]
        return findings, payload

    if report.applied:
        findings.append(
            Finding(
                chunk_id="epub_output",
                severity=Severity.INFO,
                rule="epub_output_policy_applied",
                message=(
                    f"EPUB output policy applied: language={report.language!r}, "
                    f"hyphenation={report.hyphenation!r}, "
                    f"patched_xhtml={len(report.patched_xhtml_entries)}, "
                    f"css_injected={len(report.css_injected_entries)}"
                ),
            )
        )
    for w in report.warnings:
        findings.append(
            Finding(
                chunk_id="epub_output",
                severity=Severity.WARN,
                rule="epub_output_css_conflict",
                message=f"{w['entry']}: {w['declaration']}",
                document_href=w.get("entry", ""),
            )
        )
    payload["findings"] = [f.as_dict() for f in findings]
    payload["policy"] = {
        "applied": report.applied,
        "language_policy": report.language_policy,
        "language": report.language,
        "hyphenation": report.hyphenation,
        "patched_xhtml_entries": list(report.patched_xhtml_entries),
        "css_injected_entries": list(report.css_injected_entries),
        "fixed_layout_skipped_entries": list(report.fixed_layout_skipped_entries),
        "warnings": list(report.warnings),
    }
    return findings, payload


def _changed_entry_count(changed_entries: object) -> int | object:
    if isinstance(changed_entries, list):
        return len(changed_entries)
    return changed_entries


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
    if status.validation_scope_chapter is not None:
        scope = status.validation_scope_chapter
        title = ""
        if status.current_chapter is not None:
            title = f" {status.current_chapter.title}".rstrip()
        console.print(f"validation scope: chapter {scope}{title}")
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
    if status.global_note:
        console.print(
            f"note: {status.global_note}",
            soft_wrap=True,
            markup=False,
        )
    console.print("planned chapters:")
    for chapter in status.chapters:
        console.print(
            f"- {chapter.chapter_id} {chapter.title}: "
            f"{chapter.records_translated_now} / {chapter.records_total} translated, "
            f"{chapter.records_remaining_now} remaining, status={chapter.status_now}"
        )




# --- helpers moved from cli.py for translate slice (slice 7) -----------

def _resolved_identity(proj: Project) -> TranslationIdentity:
    return resolve_identity(proj)


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


