"""Typer commands for translation context (Phase 3 slice 5).

Thin command layer for the ``context`` group (init / questions / status /
render / answer / recommend / approve / add-question / questionnaire /
add-term / remove-term / reset-term / mandate-term / audit-term /
mark-ready / export-pack / import-pack / import-md / chapter-note).
Each command parses options, delegates the actual work to
:mod:`booktx.workflows.context` and :mod:`booktx.cli_support`, and
maps :class:`booktx.errors.BooktxError` to a non-zero exit.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from booktx.cli_support import (
    _die,
    _handle_booktx_error,
    _load_project_or_exit,
    _load_runtime_or_exit,
    _project_status_snapshot,
    _reject_if_isolated,
    console,
    resolve_profile_local_path,
)
from booktx.context_organization import ContextOrganizationIssue
from booktx.context_packs import ContextPackImportResult, SeriesContextPack
from booktx.context_sync import ContextSyncPlan
from booktx.errors import BooktxError
from booktx.path_display import display_path
from booktx.runtime import RuntimeContext
from booktx.source_analysis import read_canonical_report
from booktx.source_analysis_context import (
    compatible_prefill_profiles,
    prefill_contexts,
    promote_candidate,
)
from booktx.workflows.context import (
    add_or_update_term_workflow,
    add_question_workflow,
    answer_question_workflow,
    approve_question_workflow,
    audit_term_workflow,
    build_context_status_payload,
    context_doctor_workflow,
    context_pack_import_has_failures,
    context_pack_import_payload,
    context_sync_workflow,
    export_context_pack_workflow,
    import_context_pack_workflow,
    import_md_workflow,
    init_context_workflow,
    list_questions_lines,
    load_context_or_die,
    mandate_term_workflow,
    mark_ready_workflow,
    recommend_question_workflow,
    remove_term_workflow,
    render_context_command,
    render_questionnaire_text,
    reset_term_workflow,
    upsert_chapter_note_workflow,
    write_audit_blocks,
    write_context_doctor_report,
)

context_app = typer.Typer(help="Build, inspect, and render translation context.")


@context_app.command(name="prefill")
def context_prefill(
    project_dir: Path = typer.Argument(..., help="Project root."),
    profile: str | None = typer.Option(None, "--profile", help="Target profile."),
    from_source_analysis: bool = typer.Option(
        False,
        "--from-source-analysis",
        help="Use canonical source-analysis evidence.",
    ),
    all_compatible: bool = typer.Option(
        False, "--all-compatible", help="Prefill all compatible profiles."
    ),
    include_advisory: bool = typer.Option(
        False,
        "--include-advisory",
        help="Also create advisory glossary entries for "
        "low-priority phrase candidates.",
    ),
    write: bool = typer.Option(False, "--write", help="Commit planned changes."),
) -> None:
    """Prefill open context recommendations (dry run by default)."""
    if not from_source_analysis:
        _die("--from-source-analysis is required")
    if (profile is None) == (not all_compatible):
        _die("pass exactly one of --profile or --all-compatible")
    runtime = _load_runtime_or_exit(project_dir, require_profile=False)
    if runtime.mode.isolated_output:
        _die("context prefill is a project-root command")
    report = read_canonical_report(runtime.project)
    if report is None:
        _die("no canonical source analysis; run `booktx source analyze . --write`")
    assert report is not None
    profiles = (
        compatible_prefill_profiles(runtime.project)
        if all_compatible
        else [profile or ""]
    )
    try:
        result = prefill_contexts(
            runtime.project,
            report,
            profiles=profiles,
            write=write,
            include_advisory=include_advisory,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    for item in result.profiles:
        state = "error" if item.error else ("written" if item.written else "planned")
        console.print(
            f"{item.profile}: {state} add={item.added} update={item.updated} "
            f"skip={item.skipped} conflict={item.conflicts}"
        )
        if item.error:
            console.print(f"  error: {item.error}")
    if result.blocked:
        _die("prefill preflight/write failed; inspect profile results above")
    if not write:
        console.print("Dry run. Re-run with --write to apply.")


@context_app.command(name="promote-candidate")
def context_promote_candidate(
    project_dir: Path = typer.Argument(..., help="Project root."),
    candidate_id: str = typer.Argument(..., help="Stable candidate id."),
    profile: str = typer.Option(..., "--profile", help="Target profile."),
    category: str | None = typer.Option(None, "--category", help="Glossary category."),
    target: str | None = typer.Option(
        None, "--target", help="Explicit target decision."
    ),
    forbid: list[str] | None = typer.Option(
        None, "--forbid", help="Explicit forbidden target. Repeatable."
    ),
    require_target: bool = typer.Option(
        False, "--require-target", help="Require an explicit target form."
    ),
    enforce: str = typer.Option("warn", "--enforce", help="off, warn, or error."),
    as_question: bool = typer.Option(
        False, "--as-question", help="Promote to a recommended question."
    ),
    promoted_by: str = typer.Option(
        "cli", "--promoted-by", help="Promotion provenance."
    ),
    write: bool = typer.Option(False, "--write", help="Commit the promotion."),
) -> None:
    """Promote one source-analysis candidate (dry run by default)."""
    if enforce not in {"off", "warn", "error"}:
        _die("--enforce must be off, warn, or error")
    if as_question and (target or forbid or require_target):
        _die("--as-question conflicts with glossary binding options")
    runtime = _load_runtime_or_exit(project_dir, require_profile=False)
    if runtime.mode.isolated_output:
        _die("context promote-candidate is a project-root command")
    report = read_canonical_report(runtime.project)
    if report is None:
        _die("no canonical source analysis; run `booktx source analyze . --write`")
    assert report is not None
    try:
        context_ref, _ = promote_candidate(
            runtime.project,
            report,
            profile=profile,
            candidate_id=candidate_id,
            category=category,
            target=target,
            forbidden_targets=forbid or [],
            require_target=require_target,
            enforce=enforce,  # type: ignore[arg-type]
            as_question=as_question,
            promoted_by=promoted_by,
            write=write,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    console.print(
        f"{'promoted' if write else 'would promote'} {candidate_id} "
        f"to {profile}:{context_ref}"
    )


def _validate_origin(origin: str) -> None:
    if origin not in {"core", "seed", "agent_review", "user", "legacy"}:
        _die("--origin must be core, seed, agent_review, user, or legacy")


def _render_pack_finding(finding: object) -> None:
    action = getattr(finding, "action", None)
    message = getattr(finding, "message", "")
    if action == "warning":
        console.print(f"[yellow]warning:[/yellow] {message}")
        return
    if action in {"conflict", "error"}:
        console.print(f"[red]{action}[/red] {message}")
        return
    console.print(f"{action} {getattr(finding, 'section', '')}: {message}")


def _render_pack_import_human(
    pack: SeriesContextPack,
    result: ContextPackImportResult,
    runtime: RuntimeContext,
    *,
    write: bool,
) -> None:
    console.print(
        f"Series context pack: {pack.series_id} "
        f"({pack.source_language} -> {pack.target_language})"
    )
    console.print("Write." if write else "Dry run.")
    console.print("")
    for finding in result.findings:
        _render_pack_finding(finding)
    console.print("")
    console.print(
        f"summary: add={result.added} update={result.updated} "
        f"skip={result.skipped} conflict={result.conflicts} "
        f"error={result.errors} warning={result.warnings}"
    )
    if write and result.changed:
        console.print(
            "context updated; inspect with `booktx context status .` "
            "and run `booktx context mark-ready .` after approval"
        )
    elif write:
        console.print("No files changed.")
    else:
        console.print("No files written.")


def _render_context_sync_human(plan: ContextSyncPlan) -> None:
    sections = ",".join(getattr(plan, "sections", [])) or "glossary"
    glossary_terms = getattr(plan, "glossary_terms", [])
    term_text = ", ".join(glossary_terms) if glossary_terms else "all"
    console.print(
        f"context sync: source={plan.source.profile} "
        f"sections={sections} terms={term_text}"
    )
    for target in getattr(plan, "targets", []):
        if not getattr(target, "eligible", True):
            console.print(
                f"target {target.profile}: skipped, "
                f"{target.skipped_reason or 'not eligible'}"
            )
            continue
        status = "blocked"
        if not (target.errors or target.conflicts):
            status = "changed" if target.changed else "unchanged"
        console.print(
            f"target {target.profile}: {status}, add={target.added} "
            f"update={target.updated} skip={target.skipped} "
            f"conflict={target.conflicts} warning={target.warnings} "
            f"error={target.errors}"
        )
        for finding in target.findings:
            console.print(f"  - {finding.action}: {finding.message}")
    if getattr(plan, "blocked", False):
        console.print("")
        console.print("Blocked by conflicts or errors. Nothing written.")
        console.print(
            "Re-run with --conflict keep-local or --conflict replace after review."
        )
        return
    if getattr(plan, "write", False):
        profiles = getattr(plan, "would_write_profiles", [])
        if profiles:
            console.print("")
            console.print("Wrote target contexts: " + ", ".join(profiles))
        else:
            console.print("")
            console.print("No target contexts changed.")
        return
    console.print("")
    console.print("Dry run. Re-run with --write to apply the planned sync.")


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
        help="Packaged seed template name (e.g. 'shadows_of_apt').",
    ),
    seed_file: Path | None = typer.Option(
        None,
        "--seed-file",
        help="Path to a JSON seed file with extra questions and glossary.",
    ),
) -> None:
    """Create the active profile's context.json and rendered context.md."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    try:
        message = init_context_workflow(
            runtime.project,
            force=force,
            non_interactive=non_interactive,
            seed=seed,
            seed_file=seed_file,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    for line in message.splitlines():
        console.print(line)


@context_app.command(name="questions")
def context_questions(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """List context questions."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    try:
        ctx = load_context_or_die(proj)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    for line in list_questions_lines(ctx):
        console.print(line)


@context_app.command(name="status")
def context_status(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Show translation context readiness."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    try:
        ctx = load_context_or_die(runtime.project)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    payload = build_context_status_payload(runtime.project, ctx)
    console.print(f"Status: {payload['status']}")
    console.print(
        f"open_required={payload['open_required']} open_total={payload['open_total']}"
    )
    console.print(f"recommended_required={payload['recommended_required']}")
    console.print(f"unapproved_required={payload['unapproved_required']}")
    console.print(f"answered_required={payload['answered_required']}")
    if payload["legacy_answered_required"]:
        console.print(f"legacy_answered_required={payload['legacy_answered_required']}")
    console.print(f"glossary_entries={payload['glossary_entries']}")
    console.print(
        f"context: {display_path(payload['context_path'], runtime.mode)}",
        soft_wrap=True,
    )


def _context_doctor_payload(
    issues: list[ContextOrganizationIssue],
) -> dict[str, object]:
    counts = {"error": 0, "warning": 0, "info": 0}
    for issue in issues:
        severity = issue.severity
        counts[severity] += 1
    return {
        "summary": {**counts, "total": len(issues)},
        "issues": [issue.model_dump() for issue in issues],
    }


def _print_context_doctor_human(issues: list[ContextOrganizationIssue]) -> None:
    counts = {"error": 0, "warning": 0, "info": 0}
    for issue in issues:
        counts[issue.severity] += 1
    console.print(
        f"context organization: errors={counts['error']} "
        f"warnings={counts['warning']} info={counts['info']} "
        f"total={len(issues)}"
    )
    for issue in issues:
        profile = f" [{issue.profile}]" if issue.profile else ""
        console.print(
            f"- {issue.severity}: {issue.code}{profile} at {issue.location}: "
            f"{issue.message}"
        )
        if issue.suggested_action:
            console.print(f"  action: {issue.suggested_action}")


def _doctor_report_path(
    runtime: RuntimeContext, path: Path | None, compare: bool
) -> Path:
    if path is not None:
        if runtime.mode.isolated_output:
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(
                    "isolated profile-root report paths must be profile-local "
                    "relative paths"
                )
            if runtime.mode.profile_root is None:
                raise ValueError("isolated profile-root is unavailable")
            return runtime.mode.profile_root / path
        if path.is_absolute():
            return path
        return runtime.mode.project_root / path
    if compare:
        return (
            runtime.mode.project_root
            / ".booktx"
            / "reports"
            / "context-organization-report.md"
        )
    reports_dir = runtime.project.reports_dir or (
        runtime.project.booktx_dir / "reports"
    )
    return reports_dir / "context-organization-report.md"


@context_app.command(name="doctor")
def context_doctor(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Print machine-readable JSON."
    ),
    compare_profiles: bool = typer.Option(
        False, "--compare-profiles", help="Compare sibling profile contexts."
    ),
    write_report: Path | None = typer.Option(
        None, "--write-report", help="Write a Markdown report."
    ),
) -> None:
    """Audit context organization without mutating context state."""
    runtime = _load_runtime_or_exit(
        project_dir, profile=profile, require_profile=not compare_profiles
    )
    if compare_profiles and runtime.mode.isolated_output:
        _die("--compare-profiles is not available in isolated profile-root mode")
    try:
        issues = context_doctor_workflow(runtime, compare_profiles=compare_profiles)
        if write_report is not None:
            report_path = _doctor_report_path(runtime, write_report, compare_profiles)
            write_context_doctor_report(report_path, issues)
            if not json_output:
                console.print(
                    f"wrote {display_path(report_path, runtime.mode)}",
                    soft_wrap=True,
                )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    except ValueError as exc:
        _die(str(exc))
    if json_output:
        typer.echo(
            json.dumps(_context_doctor_payload(issues), indent=2, sort_keys=True)
        )
        return
    _print_context_doctor_human(issues)


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
    view: str = typer.Option(
        "full",
        "--view",
        help="Render view: full, effective, or provenance.",
    ),
) -> None:
    """Render context.md from context.json (dry run by default)."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    try:
        ctx = load_context_or_die(runtime.project)
        result = render_context_command(
            runtime.project,
            ctx,
            write=write,
            stdout=stdout,
            force_discard_md_only=force_discard_md_only,
            view=view,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    if result["kind"] == "stdout":
        typer.echo(result["rendered"])
        return
    md_path = result["path"]
    if result["kind"] == "wrote":
        console.print(f"rendered {display_path(md_path, runtime.mode)}")
        return
    if result["matches"]:
        console.print(f"{display_path(md_path, runtime.mode)} is up to date")
        return
    console.print(f"{display_path(md_path, runtime.mode)} is out of date")
    if result["drift_unsafe"]:
        console.print(result["drift_message"])
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
    """Legacy command to answer one context question non-interactively."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    try:
        ctx = load_context_or_die(proj)
        message = answer_question_workflow(
            proj, ctx, question_id=question_id, text=text
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    console.print(message)


@context_app.command(name="recommend")
def context_recommend(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    question_id: str = typer.Argument(..., help="Question id, e.g. Q001."),
    text: str = typer.Option(..., "--text", help="Recommended answer text."),
    reason: str = typer.Option("", "--reason", help="Recommendation rationale."),
    source: str = typer.Option(
        "", "--source", help="Source evidence for the recommendation."
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Store an agent recommendation without answering the question."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    try:
        ctx = load_context_or_die(proj)
        message = recommend_question_workflow(
            proj, ctx, question_id=question_id, text=text, reason=reason, source=source
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    console.print(message)


@context_app.command(name="approve")
def context_approve(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    question_id: str = typer.Argument(..., help="Question id, e.g. Q001."),
    text: str | None = typer.Option(None, "--text", help="Approved answer text."),
    use_recommendation: bool = typer.Option(
        False, "--use-recommendation", help="Approve the stored recommendation."
    ),
    approved_by: str = typer.Option(
        "user:unspecified", "--approved-by", help="User approval source."
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Commit a user-approved context answer."""
    if (text is None) == (not use_recommendation):
        _die("pass exactly one of --text or --use-recommendation")
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    try:
        ctx = load_context_or_die(proj)
        message = approve_question_workflow(
            proj,
            ctx,
            question_id=question_id,
            text=text,
            use_recommendation=use_recommendation,
            approved_by=approved_by,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    console.print(message)


@context_app.command(name="add-question")
def context_add_question(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    topic: str = typer.Option(..., "--topic", help="Question topic."),
    question: str = typer.Option(..., "--question", help="Question text."),
    required: bool = typer.Option(
        False, "--required", help="Block readiness until approved."
    ),
    origin: str = typer.Option("agent_review", "--origin", help="Question origin."),
    recommendation: str | None = typer.Option(
        None, "--recommendation", help="Recommended answer."
    ),
    reason: str = typer.Option("", "--reason", help="Recommendation rationale."),
    source: str = typer.Option("", "--source", help="Recommendation source."),
    question_id: str | None = typer.Option(None, "--id", help="Explicit question id."),
    allow_duplicate: bool = typer.Option(
        False, "--allow-duplicate", help="Allow duplicate topic/question."
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Add a book-specific context question."""
    _validate_origin(origin)
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    try:
        ctx = load_context_or_die(proj)
        message = add_question_workflow(
            proj,
            ctx,
            topic=topic,
            question=question,
            required=required,
            origin=origin,
            recommendation=recommendation,
            reason=reason,
            source=source,
            question_id=question_id,
            allow_duplicate=allow_duplicate,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    console.print(message)


@context_app.command(name="questionnaire")
def context_questionnaire(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    stdout: bool = typer.Option(True, "--stdout", help="Print questionnaire Markdown."),
    write: Path | None = typer.Option(
        None, "--write", help="Write questionnaire Markdown."
    ),
) -> None:
    """Print a user-facing approval form."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    try:
        ctx = load_context_or_die(runtime.project)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    rendered = render_questionnaire_text(ctx)
    if write is not None:
        write.write_text(rendered, "utf-8")
        console.print(f"wrote {display_path(write, runtime.mode)}")
    if stdout or write is None:
        typer.echo(rendered)


@context_app.command(name="add-term")
def context_add_term(  # noqa: C901 - long form mirrors original
    project_dir: Path = typer.Argument(..., help="Project directory."),
    source: str = typer.Argument(..., help="Source term."),
    target: str | None = typer.Option(None, "--target", help="Approved target term."),
    forbid: list[str] | None = typer.Option(
        None,
        "--forbid",
        help="Replace the full forbidden-target list with these values. Repeatable.",
    ),
    append_forbid: list[str] | None = typer.Option(
        None, "--append-forbid", help="Append forbidden targets explicitly. Repeatable."
    ),
    clear_forbidden: bool = typer.Option(
        False, "--clear-forbidden", help="Clear all forbidden targets."
    ),
    category: str | None = typer.Option(None, "--category", help="Glossary category."),
    notes: str | None = typer.Option(None, "--notes", help="Glossary notes."),
    enforce: str | None = typer.Option(
        None, "--enforce", help="Enforcement: off, warn, or error."
    ),
    source_variant: list[str] | None = typer.Option(
        None,
        "--source-variant",
        help="Replace source variants (e.g. plurals). Repeatable.",
    ),
    target_variant: list[str] | None = typer.Option(
        None,
        "--target-variant",
        help="Replace approved target variants (e.g. inflections). Repeatable.",
    ),
    require_target: bool = typer.Option(
        False,
        "--require-target",
        help="Require an approved target form when the source term occurs.",
    ),
    allow_disable_enforcement: bool = typer.Option(
        False,
        "--allow-disable-enforcement",
        help="Allow --enforce off on a mandatory rule (intentional disable).",
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Add or update a glossary entry."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    try:
        ctx = load_context_or_die(proj)
        message = add_or_update_term_workflow(
            proj,
            ctx,
            source=source,
            target=target,
            forbid=forbid,
            append_forbid=append_forbid,
            clear_forbidden=clear_forbidden,
            category=category,
            notes=notes,
            enforce=enforce,
            source_variant=source_variant,
            target_variant=target_variant,
            require_target=require_target,
            allow_disable_enforcement=allow_disable_enforcement,
        )
        entry = next((item for item in ctx.glossary if item.source == source), None)
        if (
            entry is not None
            and target is not None
            and entry.enforce in {"warn", "error"}
        ):
            from booktx.glossary_match import entry_is_binding

            if not entry_is_binding(entry):
                console.print(
                    "[yellow]warning:[/yellow] this glossary entry is advisory only;"
                    " approved target is not required."
                )
                console.print(
                    "Use --require-target or `booktx context mandate-term` for a"
                    " binding user decision."
                )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    console.print(message)


@context_app.command(name="remove-term")
def context_remove_term(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    source: str = typer.Argument(..., help="Source term to remove."),
    missing_ok: bool = typer.Option(
        False, "--missing-ok", help="Exit zero when the term is absent."
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Delete a glossary entry by source term."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    try:
        ctx = load_context_or_die(proj)
        message = remove_term_workflow(proj, ctx, source=source, missing_ok=missing_ok)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    console.print(message)


@context_app.command(name="reset-term")
def context_reset_term(  # noqa: C901 - long form mirrors original
    project_dir: Path = typer.Argument(..., help="Project directory."),
    source: str = typer.Argument(..., help="Source term."),
    target: str | None = typer.Option(None, "--target", help="Approved target term."),
    forbid: list[str] | None = typer.Option(
        None, "--forbid", help="Forbidden target term (repeatable)."
    ),
    category: str | None = typer.Option(None, "--category", help="Glossary category."),
    notes: str | None = typer.Option(None, "--notes", help="Glossary notes."),
    enforce: str | None = typer.Option(
        None, "--enforce", help="Enforcement: off, warn, or error."
    ),
    source_variant: list[str] | None = typer.Option(
        None,
        "--source-variant",
        help="Replace source variants (e.g. plurals). Repeatable.",
    ),
    target_variant: list[str] | None = typer.Option(
        None,
        "--target-variant",
        help="Replace approved target variants (e.g. inflections). Repeatable.",
    ),
    require_target: bool = typer.Option(
        False,
        "--require-target",
        help="Require an approved target form when the source term occurs.",
    ),
    allow_disable_enforcement: bool = typer.Option(
        False,
        "--allow-disable-enforcement",
        help="Allow --enforce off on a mandatory rule (intentional disable).",
    ),
    create: bool = typer.Option(
        False, "--create", help="Create the entry if it does not exist."
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Replace one glossary entry atomically with supplied values."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    try:
        ctx = load_context_or_die(proj)
        message = reset_term_workflow(
            proj,
            ctx,
            source=source,
            target=target,
            forbid=forbid,
            category=category,
            notes=notes,
            enforce=enforce,
            source_variant=source_variant,
            target_variant=target_variant,
            require_target=require_target,
            allow_disable_enforcement=allow_disable_enforcement,
            create=create,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    console.print(message)


@context_app.command(name="mandate-term")
def context_mandate_term(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    source: str = typer.Argument(..., help="Source term."),
    target: str | None = typer.Option(None, "--target", help="Approved target term."),
    source_variant: list[str] | None = typer.Option(
        None,
        "--source-variant",
        help="Source variants (e.g. plurals). Repeatable.",
    ),
    target_variant: list[str] | None = typer.Option(
        None,
        "--target-variant",
        help="Approved target variants (e.g. inflections). Repeatable.",
    ),
    forbid: list[str] | None = typer.Option(
        None, "--forbid", help="Forbidden target term (repeatable)."
    ),
    category: str | None = typer.Option(None, "--category", help="Glossary category."),
    notes: str | None = typer.Option(None, "--notes", help="Glossary notes."),
    enforce: str = typer.Option(
        "error",
        "--enforce",
        help="Enforcement level (defaults to error; cannot be off).",
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Record a binding user terminology decision."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    try:
        ctx = load_context_or_die(proj)
        message = mandate_term_workflow(
            proj,
            ctx,
            source=source,
            target=target,
            source_variant=source_variant,
            target_variant=target_variant,
            forbid=forbid,
            category=category,
            notes=notes,
            enforce=enforce,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    console.print(message)


@context_app.command(name="audit-term")
def context_audit_term(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    source: str = typer.Argument(..., help="Source term to audit."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    chapter: str | None = typer.Option(
        None, "--chapter", help="Scope to a chapter id."
    ),
    include_inactive: bool = typer.Option(
        False,
        "--include-inactive",
        help="Also count separately-labeled inactive historical violations.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
    jsonl: bool = typer.Option(
        False, "--jsonl", help="Emit one JSON object per violating record."
    ),
    write_block: Path | None = typer.Option(
        None,
        "--write-block",
        help="Write an ingest block + companion source block for violating records.",
    ),
) -> None:
    """Audit effective records for one glossary source term."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    try:
        ctx = load_context_or_die(runtime.project)
        bundle = _project_status_snapshot(runtime.project)
        result = audit_term_workflow(
            runtime.project,
            ctx,
            source=source,
            chapter=chapter,
            include_inactive=include_inactive,
            bundle=bundle,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    if as_json:
        console.print_json(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    elif jsonl:
        for rec in result.records:
            if not rec.violates:
                continue
            payload = {
                "source_term": result.source_term,
                "record_id": rec.record_id,
                "candidate_ref": rec.candidate_ref,
                "forbidden_found": list(rec.forbidden_found),
                "missing_approved": rec.missing_approved,
            }
            console.print_json(json.dumps(payload, ensure_ascii=False))
    else:
        approved = " / ".join(result.approved_targets) or "(none)"
        console.print(f"term: {result.source_term} -> {approved}")
        console.print(f"records with source term: {result.records_with_source_term}")
        console.print(f"effective target clean: {result.effective_clean}")
        console.print(
            f"forbidden target violations: {result.forbidden_violation_records}"
        )
        console.print(f"missing approved target: {result.missing_approved_records}")
        if include_inactive:
            console.print(
                f"inactive historical violations: "
                f"{result.inactive_violation_records} (not blocking current output)"
            )
        for rec in result.records:
            if not rec.violates:
                continue
            parts: list[str] = []
            if rec.forbidden_found:
                parts.append(f"forbidden={','.join(rec.forbidden_found)}")
            if rec.missing_approved:
                parts.append("missing approved target")
            console.print(f"  {rec.record_id}: {'; '.join(parts)}")
    if write_block is not None:
        try:
            block_path = resolve_profile_local_path(
                runtime.project, write_block, purpose="--write-block"
            )
            ingest_path, source_path = write_audit_blocks(result, block_path)
        except BooktxError as exc:
            _handle_booktx_error(exc)
            return
        console.print(f"ingest block: {ingest_path}")
        console.print(f"source block: {source_path}")


@context_app.command(name="mark-ready")
def context_mark_ready(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    force: bool = typer.Option(
        False, "--force", help="Mark ready even with unresolved required questions."
    ),
    reason: str = typer.Option("", "--reason", help="Reason required with --force."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
) -> None:
    """Mark context ready once required questions are answered."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    try:
        ctx = load_context_or_die(runtime.project)
        message = mark_ready_workflow(runtime.project, ctx, force=force, reason=reason)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    console.print(message)


@context_app.command(name="export-pack")
def context_export_pack(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    series_id: str = typer.Option(..., "--series-id", help="Series identifier."),
    title: str = typer.Option("", "--title", help="Optional pack title."),
    output: Path = typer.Option(..., "--output", help="Output pack file path."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    questions: str = typer.Option(
        "approved",
        "--questions",
        help="Question inclusion: none or approved (default).",
    ),
    no_style: bool = typer.Option(False, "--no-style", help="Exclude style."),
    no_global_rules: bool = typer.Option(
        False, "--no-global-rules", help="Exclude global rules."
    ),
    no_glossary: bool = typer.Option(False, "--no-glossary", help="Exclude glossary."),
    allow_not_ready: bool = typer.Option(
        False,
        "--allow-not-ready",
        help="Export a draft or forced-ready context (with a warning).",
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing output file."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Export a series-wide context pack from the selected profile."""
    if questions not in {"none", "approved"}:
        _die("--questions must be none or approved")
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    try:
        summary = export_context_pack_workflow(
            runtime.project,
            runtime,
            series_id=series_id,
            title=title,
            output=output,
            questions=questions,
            no_style=no_style,
            no_global_rules=no_global_rules,
            no_glossary=no_glossary,
            allow_not_ready=allow_not_ready,
            force=force,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    if summary["allow_not_ready"]:
        console.print("[yellow]warning:[/yellow] exported a draft/forced-ready context")
    if as_json:
        json_payload = {**summary, "path": summary["path"].as_posix()}
        console.print_json(json.dumps(json_payload, ensure_ascii=False))
        return
    console.print(
        f"wrote series context pack: {display_path(summary['path'], runtime.mode)}"
    )
    console.print(
        f"series_id={summary['series_id']} source={summary['source']} "
        f"target={summary['target']} glossary={summary['glossary']} "
        f"questions={summary['questions']}"
    )


@context_app.command(name="import-pack")
def context_import_pack(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    file: Path = typer.Option(..., "--file", help="Input pack file path."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    write: bool = typer.Option(
        False, "--write", help="Commit the planned import (dry run by default)."
    ),
    init_missing_context: bool = typer.Option(
        False,
        "--init-missing-context",
        help="Create a fresh context if none exists.",
    ),
    conflict: str = typer.Option(
        "fail",
        "--conflict",
        help="Conflict mode: fail, keep-local, replace.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Import a series-wide context pack into the selected profile."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    try:
        pack, result, wrote = import_context_pack_workflow(
            runtime,
            file=file,
            write=write,
            init_missing_context=init_missing_context,
            conflict=conflict,
        )
    except BooktxError as exc:
        if as_json:
            typer.echo(json.dumps({"error": exc.code, "message": str(exc)}))
        else:
            _handle_booktx_error(exc)
        raise typer.Exit(code=1) from exc

    if as_json:
        payload = context_pack_import_payload(pack, result, wrote=wrote)
        typer.echo(json.dumps(payload, ensure_ascii=False))
    else:
        _render_pack_import_human(pack, result, runtime, write=wrote)
    if context_pack_import_has_failures(result):
        raise typer.Exit(code=1)


@context_app.command(name="sync")
def context_sync(
    project_dir: Path = typer.Argument(..., help="Project directory."),
    source_profile: str = typer.Option(..., "--from", help="Source profile name."),
    target_profiles: list[str] | None = typer.Option(
        None, "--to", help="Explicit target profile(s)."
    ),
    all_compatible: bool = typer.Option(
        False,
        "--all-compatible",
        help="Target all compatible sibling profiles automatically.",
    ),
    section: list[str] | None = typer.Option(
        None,
        "--section",
        help="Section(s) to sync: glossary, style, global-rules, questions.",
    ),
    term: list[str] | None = typer.Option(
        None,
        "--term",
        help="Glossary source term(s) to sync when glossary is selected.",
    ),
    question_id: list[str] | None = typer.Option(
        None,
        "--question-id",
        help="Reusable question id(s) to sync when questions are selected.",
    ),
    conflict: str = typer.Option(
        "fail",
        "--conflict",
        help="Conflict mode: fail, keep-local, replace.",
    ),
    same_locale: bool = typer.Option(
        False,
        "--same-locale",
        help="Require the target locale to match the source profile locale.",
    ),
    include_pass_through: bool = typer.Option(
        False,
        "--include-pass-through",
        help="Allow pass-through targets when explicitly requested or discovered.",
    ),
    include_selection: bool = typer.Option(
        False,
        "--include-selection",
        help="Include selection profiles in --all-compatible discovery.",
    ),
    init_missing_context: bool = typer.Option(
        False,
        "--init-missing-context",
        help="Create a default target context when one is missing.",
    ),
    allow_not_ready: bool = typer.Option(
        False,
        "--allow-not-ready",
        help="Allow syncing from a source profile whose context is not ready.",
    ),
    write: bool = typer.Option(
        False, "--write", help="Apply the sync after a successful full preflight."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Plan or apply controlled same-book context sync across sibling profiles."""

    runtime = _load_runtime_or_exit(project_dir, require_profile=False)
    _reject_if_isolated(runtime)
    try:
        plan = context_sync_workflow(
            runtime,
            source_profile=source_profile,
            target_profiles=target_profiles,
            all_compatible=all_compatible,
            sections=set(section or ["glossary"]),
            terms=list(term or []),
            question_ids=list(question_id or []),
            conflict=conflict,
            same_locale=same_locale,
            include_pass_through=include_pass_through,
            include_selection=include_selection,
            allow_not_ready=allow_not_ready,
            init_missing_context=init_missing_context,
            write=write,
        )
    except BooktxError as exc:
        if as_json:
            typer.echo(json.dumps({"error": exc.code, "message": str(exc)}))
        else:
            _handle_booktx_error(exc)
        raise typer.Exit(code=1) from exc

    if as_json:
        typer.echo(json.dumps(plan.model_dump(mode="json"), ensure_ascii=False))
    else:
        _render_context_sync_human(plan)
    if plan.blocked:
        raise typer.Exit(code=1)


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
    """Import chapter notes from context.md into context.json."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=True)
    try:
        ctx = load_context_or_die(runtime.project)
        result = import_md_workflow(
            runtime.project,
            ctx,
            write=write,
            replace_existing=replace_existing,
            append_existing_lists=append_existing_lists,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    changed = result["changed"]
    if result["wrote"]:
        if changed:
            console.print(f"updated chapters: {', '.join(changed)}")
        else:
            console.print("no chapter changes")
        console.print(f"wrote {display_path(result['context_path'], runtime.mode)}")
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
    replace_all: bool = typer.Option(
        False, "--replace-all", help="Replace the entire chapter note atomically."
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
    """Create or update one chapter note in context.json."""
    proj = _load_project_or_exit(project_dir, profile=profile, require_profile=True)
    try:
        ctx = load_context_or_die(proj)
        message = upsert_chapter_note_workflow(
            proj,
            ctx,
            chapter_id=chapter_id,
            title=title,
            source_summary=source_summary,
            translation_summary=translation_summary,
            decision=decision,
            open_issue=open_issue,
            replace_decisions=replace_decisions,
            replace_open_issues=replace_open_issues,
            replace_all=replace_all,
            force_discard_md_only=force_discard_md_only,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    console.print(message)
