"""Domain workflow functions for translation context (Phase 3 slice 5).

Wraps the context, glossary, and context_pack service layers so the Typer
command layer never imports ``booktx.config`` or ``booktx.context``
mutations directly. User-facing error cases raise
:class:`booktx.errors.BooktxError`; CLI-only rendering and exit-code
mapping live in :mod:`booktx.commands.context`.

The split mirrors the existing slice 1-4 pattern: workflows own the
mutations and domain decisions, commands are thin Typer wrappers.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from booktx.config import _err
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
    next_question_id,
    parse_context_markdown_chapter_notes,
    render_context_markdown,
    unapproved_required_questions,
    unresolved_required_questions,
    upsert_chapter_context,
    write_context,
    write_context_markdown,
)
from booktx.context_packs import (
    ContextPackError,
    ContextPackImportResult,
    SeriesContextPack,
    export_context_pack,
    import_context_pack,
    plan_context_pack_import,
    read_context_pack,
    write_context_pack,
)
from booktx.context_sync import ContextSyncError, ContextSyncPlan
from booktx.context_sync import (
    apply_context_sync as apply_context_sync_service,
)
from booktx.context_sync import (
    plan_context_sync as plan_context_sync_service,
)
from booktx.errors import BooktxError
from booktx.glossary_audit import (
    audit_glossary_term,
    render_ingest_block,
    render_source_block,
)
from booktx.runtime import RuntimeContext

if TYPE_CHECKING:
    from booktx.config import Project
    from booktx.glossary_audit import GlossaryAuditResult


# --- pure domain helpers ----------------------------------------------------


def _dedupe_nonempty(values: list[str] | None) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in values or []:
        value = raw.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _term_equal(a: str, b: str, *, case_sensitive: bool) -> bool:
    return a == b if case_sensitive else a.casefold() == b.casefold()


def _clean_forbidden_targets(
    values: list[str],
    *,
    approved_target: str | None,
    case_sensitive: bool,
) -> list[str]:
    cleaned = _dedupe_nonempty(values)
    if approved_target:
        cleaned = [
            value
            for value in cleaned
            if not _term_equal(value, approved_target, case_sensitive=case_sensitive)
        ]
    return cleaned


def _clean_variant_list(values: list[str] | None) -> list[str]:
    """Trim and dedupe source/target variant lists."""
    return _dedupe_nonempty(values)


def _die_disable_enforcement_guard(
    *, require_target: bool, forbidden_targets: list[str], allow: bool
) -> None:
    """Refuse ``--enforce off`` on a mandatory glossary rule unless allowed."""
    if allow:
        return
    if require_target or forbidden_targets:
        raise _err(
            "disable_enforcement_refused",
            "refusing to disable a mandatory glossary rule; "
            "use --allow-disable-enforcement if this is intentional",
        )


def _approval_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _open_required_questions(ctx: TranslationContext) -> list[ContextQuestion]:
    return unresolved_required_questions(ctx)


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
    """Raise :class:`BooktxError` when ``context.md`` overwrite would be unsafe."""
    try:
        ensure_context_markdown_safe_to_overwrite(
            proj, ctx, allow_discard_md_only=allow_discard_md_only
        )
    except ValueError as exc:
        raise BooktxError(
            "context_markdown_drift",
            f"{exc} Run `booktx context import-md . --write` first to recover "
            "Markdown-only notes.",
        ) from exc


def _resolve_pack_path(
    path_str: str,
    runtime: RuntimeContext,
    *,
    must_exist: bool,
) -> Path:
    """Resolve a pack input/output path for the current runtime mode."""
    raw = Path(path_str).expanduser()
    if not runtime.mode.isolated_output:
        if must_exist and not raw.is_file():
            raise _err("pack_not_found", f"pack file not found: {path_str}")
        return raw
    profile_root = runtime.mode.profile_root
    if profile_root is None:
        raise _err(
            "pack_isolated_root",
            "profile root is not available in isolated mode",
        )
    if raw.is_absolute():
        raise _err(
            "pack_absolute_in_isolated",
            "absolute pack paths are not allowed in profile-root isolated "
            "mode; use a path relative to the profile root",
        )
    if any(part == ".." for part in raw.parts):
        raise _err(
            "pack_parent_escape_in_isolated",
            "parent-directory escapes are not allowed in profile-root isolated mode",
        )
    candidate = profile_root / raw
    try:
        resolved_parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise _err(
            "pack_unreachable", f"pack path is not reachable: {path_str}: {exc}"
        ) from exc
    profile_resolved = profile_root.resolve(strict=True)
    try:
        resolved_parent.relative_to(profile_resolved)
    except ValueError as exc:
        raise _err("pack_escape_root", "pack path escapes the profile root") from exc
    if must_exist and not candidate.is_file():
        raise _err("pack_not_found", f"pack file not found: {path_str}")
    if candidate.is_symlink():
        try:
            link_target = candidate.resolve(strict=False)
            link_target.relative_to(profile_resolved)
        except ValueError as exc:
            raise _err(
                "pack_symlink_escape", "pack path escapes the profile root via symlink"
            ) from exc
        except OSError as exc:
            raise _err(
                "pack_unreachable", f"pack path is not reachable: {path_str}: {exc}"
            ) from exc
    return candidate


# --- workflow functions ------------------------------------------------------


def load_context_or_die(proj: Project) -> TranslationContext:
    """Load the active context, raising BooktxError when missing or invalid."""
    try:
        ctx = load_context(proj)
    except Exception as exc:  # noqa: BLE001 - surface as user-facing CLI error
        raise BooktxError(
            "context_invalid", f"translation context is invalid: {exc}"
        ) from exc
    if ctx is None:
        raise BooktxError(
            "context_missing",
            "translation context is missing. Run: booktx context init .",
        )
    return ctx


def init_context_workflow(
    project: Project,
    *,
    force: bool,
    non_interactive: bool,
    seed: str | None,
    seed_file: Path | None,
) -> str:
    """Create the active profile's context.json and rendered context.md."""
    existing = None if force else load_context(project)
    if existing is not None:
        _guard_md_safe_or_die(project, existing)
        write_context_markdown(project, existing)
        return f"context exists: {context_markdown_path(project).as_posix()}"

    ctx = default_context(project)
    if seed is not None:
        from booktx.context import load_seed_template

        try:
            extra_questions, extra_glossary = load_seed_template(seed)
        except FileNotFoundError as exc:
            raise BooktxError("seed_not_found", str(exc)) from exc
        ctx.questions.extend(extra_questions)
        ctx.glossary.extend(extra_glossary)
    if seed_file is not None:
        try:
            seed_data = json.loads(seed_file.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise BooktxError(
                "seed_file_invalid", f"could not read seed file: {exc}"
            ) from exc
        for q in seed_data.get("questions", []):
            ctx.questions.append(ContextQuestion(**q))
        for g in seed_data.get("glossary", []):
            ctx.glossary.append(GlossaryEntry(**g))

    if not non_interactive:
        for q in ctx.questions:
            answer = _interactive_prompt(q.question)
            if answer.strip():
                q.answer = answer.strip()
                q.status = "answered"
                q.answer_source = "user"
                q.approved_by = "cli:interactive"
                q.approved_at = _approval_timestamp()
                apply_answer_to_context(ctx, q.id, q.answer)
        ctx.ready = not _open_required_questions(ctx)

    _guard_md_safe_or_die(project, ctx, allow_discard_md_only=force)
    write_context(project, ctx)
    write_context_markdown(project, ctx)
    return (
        f"wrote {context_path(project).as_posix()}\n"
        f"wrote {context_markdown_path(project).as_posix()}"
    )


def _interactive_prompt(question: str) -> str:
    """Read a single line from stdin for interactive ``context init``."""
    sys.stdout.write(f"{question}: ")
    sys.stdout.flush()
    return sys.stdin.readline().rstrip("\n")


def list_questions_lines(ctx: TranslationContext) -> list[str]:
    """Return the human-rendered question list for ``context questions``."""
    lines: list[str] = []
    for q in ctx.questions:
        marker = "required" if q.required else "optional"
        answer = f" -> {q.answer}" if q.answer else ""
        lines.append(f"{q.id} [{marker}] {q.status} {q.topic}: {q.question}{answer}")
    return lines


def build_context_status_payload(
    proj: Project, ctx: TranslationContext
) -> dict[str, Any]:
    """Return the structured status payload used by ``context status``."""
    open_required = _open_required_questions(ctx)
    open_total = [q for q in ctx.questions if q.status == "open"]
    recommended_required = [
        q for q in ctx.questions if q.required and q.status == "recommended"
    ]
    unapproved_required = unapproved_required_questions(ctx)
    answered_required = [
        q for q in ctx.questions if q.required and q.status == "answered"
    ]
    legacy_answered_required = [
        q for q in answered_required if q.answer_source == "legacy"
    ]
    return {
        "status": "READY" if ctx.ready else "NOT READY",
        "open_required": len(open_required),
        "open_total": len(open_total),
        "recommended_required": len(recommended_required),
        "unapproved_required": len(unapproved_required),
        "answered_required": len(answered_required),
        "legacy_answered_required": len(legacy_answered_required),
        "glossary_entries": len(ctx.glossary),
        "context_path": context_markdown_path(proj),
    }


def render_context_command(
    proj: Project,
    ctx: TranslationContext,
    *,
    write: bool,
    stdout: bool,
    force_discard_md_only: bool,
    view: str = "full",
) -> dict[str, Any]:
    """Implement the ``context render`` command; return a render result."""
    if view not in {"full", "effective", "provenance"}:
        raise _err(
            "context_render_view",
            "--view must be one of full, effective, or provenance",
        )
    rendered = render_context_markdown(ctx, view=view)  # type: ignore[arg-type]
    if stdout:
        return {"kind": "stdout", "rendered": rendered}
    md_path = context_markdown_path(proj)
    drift = analyze_context_markdown_drift(proj, ctx)
    matches = bool(
        md_path.is_file()
        and md_path.read_text("utf-8").replace("\r\n", "\n")
        == rendered.replace("\r\n", "\n")
    )
    if write:
        if drift.unsafe_to_overwrite and not force_discard_md_only:
            raise _err("context_markdown_drift", _drift_unsafe_message(drift))
        from booktx.io_utils import write_text_atomic

        write_text_atomic(md_path, rendered)
        return {
            "kind": "wrote",
            "path": md_path,
        }
    return {
        "kind": "dry_run",
        "matches": matches,
        "path": md_path,
        "drift_unsafe": drift.unsafe_to_overwrite,
        "drift_message": (
            _drift_unsafe_message(drift) if drift.unsafe_to_overwrite else ""
        ),
    }


def answer_question_workflow(
    proj: Project,
    ctx: TranslationContext,
    *,
    question_id: str,
    text: str,
) -> str:
    """Legacy ``context answer`` workflow: apply text as the question's answer."""
    for q in ctx.questions:
        if q.id == question_id:
            q.answer = text
            q.status = "answered" if text.strip() else "open"
            q.answer_source = "legacy" if text.strip() else None
            q.approved_by = "legacy:context-answer" if text.strip() else ""
            q.approved_at = _approval_timestamp() if text.strip() else ""
            apply_answer_to_context(ctx, question_id, text)
            _guard_md_safe_or_die(proj, ctx)
            write_context(proj, ctx)
            write_context_markdown(proj, ctx)
            return f"answered {question_id}"
    raise _err("unknown_question_id", f"unknown question id: {question_id}")


def recommend_question_workflow(
    proj: Project,
    ctx: TranslationContext,
    *,
    question_id: str,
    text: str,
    reason: str,
    source: str,
) -> str:
    """Store an agent recommendation without answering the question."""
    for q in ctx.questions:
        if q.id == question_id:
            q.recommendation = text
            q.recommendation_reason = reason
            q.recommendation_source = source
            if q.status == "open":
                q.status = "recommended"
            _guard_md_safe_or_die(proj, ctx)
            write_context(proj, ctx)
            write_context_markdown(proj, ctx)
            return f"recommended {question_id}"
    raise _err("unknown_question_id", f"unknown question id: {question_id}")


def approve_question_workflow(
    proj: Project,
    ctx: TranslationContext,
    *,
    question_id: str,
    text: str | None,
    use_recommendation: bool,
    approved_by: str,
) -> str:
    """Commit a user-approved context answer."""
    if (text is None) == (not use_recommendation):
        raise _err(
            "approve_mode",
            "pass exactly one of --text or --use-recommendation",
        )
    for q in ctx.questions:
        if q.id == question_id:
            answer = q.recommendation if use_recommendation else text
            if not answer or not answer.strip():
                raise _err("approve_empty", "approved answer is empty")
            approved_answer = answer.strip()
            q.answer = approved_answer
            q.status = "answered"
            q.answer_source = "user"
            q.approved_by = approved_by
            q.approved_at = _approval_timestamp()
            apply_answer_to_context(ctx, question_id, approved_answer)
            _guard_md_safe_or_die(proj, ctx)
            write_context(proj, ctx)
            write_context_markdown(proj, ctx)
            return f"approved {question_id}"
    raise _err("unknown_question_id", f"unknown question id: {question_id}")


def add_question_workflow(
    proj: Project,
    ctx: TranslationContext,
    *,
    topic: str,
    question: str,
    required: bool,
    origin: str,
    recommendation: str | None,
    reason: str,
    source: str,
    question_id: str | None,
    allow_duplicate: bool,
) -> str:
    """Add a book-specific context question."""
    if origin not in {"core", "seed", "agent_review", "user", "legacy"}:
        raise _err(
            "add_question_origin",
            "--origin must be core, seed, agent_review, user, or legacy",
        )
    if not allow_duplicate:
        for q in ctx.questions:
            if q.topic == topic and q.question == question:
                raise _err("add_question_duplicate", f"duplicate question: {q.id}")
    new_q = ContextQuestion(
        id=question_id or next_question_id(ctx),
        topic=topic,
        question=question,
        required=required,
        origin=origin,  # type: ignore[arg-type]
        recommendation=recommendation,
        recommendation_reason=reason,
        recommendation_source=source,
        status="recommended" if recommendation else "open",
    )
    ctx.questions.append(new_q)
    _guard_md_safe_or_die(proj, ctx)
    write_context(proj, ctx)
    write_context_markdown(proj, ctx)
    return f"added question {new_q.id}"


def render_questionnaire_text(ctx: TranslationContext) -> str:
    """Render the questionnaire Markdown used by ``context questionnaire``."""
    lines = [
        "# Translation Context Approval",
        "",
        "Please approve, edit, or reject the following context answers.",
        "",
        "## Required questions",
        "",
    ]
    for q in ctx.questions:
        if not q.required:
            continue
        lines.extend(
            [
                f"### {q.id} {q.topic}",
                f"Question: {q.question}",
                f"Recommendation: {q.recommendation or ''}",
                f"Reason: {q.recommendation_reason}",
                "",
                "Your answer:",
                "",
            ]
        )
    optional = [q for q in ctx.questions if not q.required]
    if optional:
        lines.extend(["## Optional questions", ""])
        for q in optional:
            lines.extend(
                [
                    f"### {q.id} {q.topic}",
                    f"Question: {q.question}",
                    f"Recommendation: {q.recommendation or ''}",
                    f"Reason: {q.recommendation_reason}",
                    "",
                    "Your answer:",
                    "",
                ]
            )
    return "\n".join(lines)


def _term_message(prefix: str, entry: GlossaryEntry) -> str:
    from booktx.glossary_match import entry_is_binding

    kind = "binding" if entry_is_binding(entry) else "advisory"
    return f"{prefix} {kind} term: {entry.source}"


def add_or_update_term_workflow(  # noqa: C901 - long form mirrors original
    proj: Project,
    ctx: TranslationContext,
    *,
    source: str,
    target: str | None,
    forbid: list[str] | None,
    append_forbid: list[str] | None,
    clear_forbidden: bool,
    category: str | None,
    notes: str | None,
    enforce: str | None,
    source_variant: list[str] | None,
    target_variant: list[str] | None,
    require_target: bool,
    allow_disable_enforcement: bool,
) -> str:
    """Add or update one glossary entry."""
    forbid_supplied = forbid is not None
    append_supplied = append_forbid is not None
    if forbid_supplied and append_supplied:
        raise _err(
            "term_forbid_conflict",
            "--forbid and --append-forbid are mutually exclusive",
        )
    if clear_forbidden and (forbid_supplied or append_supplied):
        raise _err(
            "term_clear_forbidden_conflict",
            "--clear-forbidden conflicts with --forbid and --append-forbid",
        )
    if enforce is not None and enforce not in {"off", "warn", "error"}:
        raise _err("term_enforce", "--enforce must be off, warn, or error")

    existing: GlossaryEntry | None = None
    for entry in ctx.glossary:
        if entry.source == source:
            existing = entry
            break

    if existing is not None:
        if target is not None:
            existing.target = target
            existing.status = "approved" if target else existing.status
        if clear_forbidden:
            existing.forbidden_targets = _clean_forbidden_targets(
                [],
                approved_target=existing.target,
                case_sensitive=existing.case_sensitive,
            )
        elif forbid_supplied:
            existing.forbidden_targets = _clean_forbidden_targets(
                forbid or [],
                approved_target=existing.target,
                case_sensitive=existing.case_sensitive,
            )
        elif append_supplied:
            combined = list(existing.forbidden_targets) + (append_forbid or [])
            existing.forbidden_targets = _clean_forbidden_targets(
                combined,
                approved_target=existing.target,
                case_sensitive=existing.case_sensitive,
            )
        if category is not None:
            existing.category = category
        if notes is not None:
            existing.notes = notes
        if source_variant is not None:
            existing.source_variants = _clean_variant_list(source_variant)
        if target_variant is not None:
            existing.target_variants = _clean_variant_list(target_variant)
        if require_target:
            existing.require_target = True
        if enforce is not None:
            existing.enforce = enforce  # type: ignore[assignment]
        if existing.enforce == "off":
            _die_disable_enforcement_guard(
                require_target=existing.require_target,
                forbidden_targets=existing.forbidden_targets,
                allow=allow_disable_enforcement,
            )
    else:
        applied_category = category if category is not None else "term"
        applied_enforce = enforce if enforce is not None else "warn"
        applied_notes = notes if notes is not None else ""
        if clear_forbidden:
            raw_forbidden: list[str] = []
        elif forbid_supplied:
            raw_forbidden = forbid or []
        elif append_supplied:
            raw_forbidden = append_forbid or []
        else:
            raw_forbidden = []
        cleaned_forbidden = _clean_forbidden_targets(
            raw_forbidden,
            approved_target=target,
            case_sensitive=False,
        )
        ctx.glossary.append(
            GlossaryEntry(
                source=source,
                source_variants=_clean_variant_list(source_variant),
                target=target,
                target_variants=_clean_variant_list(target_variant),
                require_target=require_target,
                forbidden_targets=cleaned_forbidden,
                category=applied_category,
                status="approved" if target else "open",
                notes=applied_notes,
                enforce=applied_enforce,  # type: ignore[arg-type]
            )
        )
        if applied_enforce == "off":
            _die_disable_enforcement_guard(
                require_target=require_target,
                forbidden_targets=cleaned_forbidden,
                allow=allow_disable_enforcement,
            )

    _guard_md_safe_or_die(proj, ctx)
    write_context(proj, ctx)
    write_context_markdown(proj, ctx)
    entry = next(e for e in ctx.glossary if e.source == source)
    return _term_message("updated", entry)


def remove_term_workflow(
    proj: Project,
    ctx: TranslationContext,
    *,
    source: str,
    missing_ok: bool,
) -> str:
    """Delete a glossary entry by source term."""
    before = len(ctx.glossary)
    ctx.glossary = [entry for entry in ctx.glossary if entry.source != source]
    after = len(ctx.glossary)
    removed = before - after
    if removed == 0 and not missing_ok:
        raise _err("term_missing", f"no glossary entry for source: {source}")
    _guard_md_safe_or_die(proj, ctx)
    write_context(proj, ctx)
    write_context_markdown(proj, ctx)
    return f"removed {removed} term(s): {source}"


def reset_term_workflow(  # noqa: C901 - long form mirrors original
    proj: Project,
    ctx: TranslationContext,
    *,
    source: str,
    target: str | None,
    forbid: list[str] | None,
    category: str | None,
    notes: str | None,
    enforce: str | None,
    source_variant: list[str] | None,
    target_variant: list[str] | None,
    require_target: bool,
    allow_disable_enforcement: bool,
    create: bool,
) -> str:
    """Replace one glossary entry atomically; optionally create it when missing."""
    if enforce is not None and enforce not in {"off", "warn", "error"}:
        raise _err("term_enforce", "--enforce must be off, warn, or error")
    existing: GlossaryEntry | None = None
    for entry in ctx.glossary:
        if entry.source == source:
            existing = entry
            break
    if existing is None:
        if not create:
            raise _err(
                "term_missing",
                f"no glossary entry for source: {source}. Pass --create to add it.",
            )
        applied_category = category if category is not None else "term"
        applied_enforce = enforce if enforce is not None else "warn"
        applied_notes = notes if notes is not None else ""
        cleaned_forbidden = _clean_forbidden_targets(
            forbid or [],
            approved_target=target,
            case_sensitive=False,
        )
        if applied_enforce == "off":
            _die_disable_enforcement_guard(
                require_target=require_target,
                forbidden_targets=cleaned_forbidden,
                allow=allow_disable_enforcement,
            )
        ctx.glossary.append(
            GlossaryEntry(
                source=source,
                source_variants=_clean_variant_list(source_variant),
                target=target,
                target_variants=_clean_variant_list(target_variant),
                require_target=require_target,
                forbidden_targets=cleaned_forbidden,
                category=applied_category,
                status="approved" if target else "open",
                notes=applied_notes,
                enforce=applied_enforce,  # type: ignore[arg-type]
            )
        )
        created = ctx.glossary[-1]
        _guard_md_safe_or_die(proj, ctx)
        write_context(proj, ctx)
        write_context_markdown(proj, ctx)
        return _term_message("created", created)

    if target is not None:
        existing.target = target
        existing.status = "approved" if target else "open"
    if forbid is not None:
        existing.forbidden_targets = _clean_forbidden_targets(
            forbid,
            approved_target=existing.target,
            case_sensitive=existing.case_sensitive,
        )
    if category is not None:
        existing.category = category
    if notes is not None:
        existing.notes = notes
    if source_variant is not None:
        existing.source_variants = _clean_variant_list(source_variant)
    if target_variant is not None:
        existing.target_variants = _clean_variant_list(target_variant)
    if require_target:
        existing.require_target = True
    if enforce is not None:
        existing.enforce = enforce  # type: ignore[assignment]
    if existing.enforce == "off":
        _die_disable_enforcement_guard(
            require_target=existing.require_target,
            forbidden_targets=existing.forbidden_targets,
            allow=allow_disable_enforcement,
        )

    _guard_md_safe_or_die(proj, ctx)
    write_context(proj, ctx)
    write_context_markdown(proj, ctx)
    return _term_message("reset", existing)


def mandate_term_workflow(
    proj: Project,
    ctx: TranslationContext,
    *,
    source: str,
    target: str | None,
    source_variant: list[str] | None,
    target_variant: list[str] | None,
    forbid: list[str] | None,
    category: str | None,
    notes: str | None,
    enforce: str,
) -> str:
    """Record a binding user terminology decision (always enforced)."""
    if enforce == "off":
        raise _err(
            "mandate_term_off",
            "mandate-term cannot disable enforcement; use reset-term instead",
        )
    if enforce not in {"warn", "error"}:
        raise _err("term_enforce", "--enforce must be warn or error")
    applied_category = category if category is not None else "term"
    applied_notes = notes if notes is not None else ""
    cleaned_forbidden = _clean_forbidden_targets(
        forbid or [],
        approved_target=target,
        case_sensitive=False,
    )
    replacement = GlossaryEntry(
        source=source,
        source_variants=_clean_variant_list(source_variant),
        target=target,
        target_variants=_clean_variant_list(target_variant),
        require_target=True,
        forbidden_targets=cleaned_forbidden,
        category=applied_category,
        status="approved" if target else "open",
        notes=applied_notes,
        enforce=enforce,  # type: ignore[arg-type]
    )
    ctx.glossary = [e for e in ctx.glossary if e.source != source]
    ctx.glossary.append(replacement)
    _guard_md_safe_or_die(proj, ctx)
    write_context(proj, ctx)
    write_context_markdown(proj, ctx)
    return f"mandated term: {source}"


def audit_term_workflow(
    proj: Project,
    ctx: TranslationContext,
    *,
    source: str,
    chapter: str | None,
    include_inactive: bool,
    bundle: Any,
) -> GlossaryAuditResult:
    """Audit effective records for one glossary source term."""
    result = audit_glossary_term(
        proj,
        bundle,
        source_term=source,
        include_inactive=include_inactive,
        chapter_id=chapter,
    )
    if result is None:
        raise _err("term_missing", f"no glossary entry for source: {source}")
    return result


def write_audit_blocks(
    result: GlossaryAuditResult, write_block: Path
) -> tuple[str, str]:
    """Write the ingest + companion source blocks; return their display paths."""
    write_block.parent.mkdir(parents=True, exist_ok=True)
    ingest = render_ingest_block(result)
    source_block = render_source_block(result)
    write_block.write_text(ingest, encoding="utf-8")
    name = write_block.name
    if name.endswith(".block.txt"):
        companion_name = name[: -len(".block.txt")] + ".source.block.txt"
    else:
        companion_name = write_block.stem + ".source.block.txt"
    companion = write_block.with_name(companion_name)
    companion.write_text(source_block, encoding="utf-8")
    return str(write_block), str(companion)


def mark_ready_workflow(
    proj: Project,
    ctx: TranslationContext,
    *,
    force: bool,
    reason: str,
) -> str:
    """Mark context ready once required questions are answered."""
    open_required = _open_required_questions(ctx)
    unapproved = unapproved_required_questions(ctx)
    if force and not reason.strip():
        raise _err("mark_ready_force_reason", "--force requires --reason")
    if not force:
        if open_required:
            ids = ", ".join(q.id for q in open_required)
            raise _err(
                "mark_ready_open",
                f"required questions are unresolved or unapproved: {ids}",
            )
        if unapproved:
            ids = ", ".join(q.id for q in unapproved)
            raise _err(
                "mark_ready_unapproved",
                f"required questions have unapproved answers: {ids}",
            )
    ctx.ready = True
    ctx.ready_forced = force
    ctx.ready_reason = reason
    ctx.ready_by = "cli:mark-ready"
    ctx.ready_at = _approval_timestamp()
    _guard_md_safe_or_die(proj, ctx)
    write_context(proj, ctx)
    write_context_markdown(proj, ctx)
    return f"context ready: {context_markdown_path(proj).as_posix()}"


def export_context_pack_workflow(
    proj: Project,
    runtime: RuntimeContext,
    *,
    series_id: str,
    title: str,
    output: Path,
    questions: str,
    no_style: bool,
    no_global_rules: bool,
    no_glossary: bool,
    allow_not_ready: bool,
    force: bool = False,
) -> dict[str, Any]:
    """Export a series-wide context pack; return the summary payload."""
    if questions not in {"none", "approved"}:
        raise _err("export_pack_questions", "--questions must be none or approved")
    out_path = _resolve_pack_path(str(output), runtime, must_exist=False)
    if out_path.exists() and not force:
        raise _err(
            "export_pack_exists",
            f"output file already exists: {out_path.as_posix()}; "
            "pass --force to overwrite",
        )
    try:
        pack = export_context_pack(
            proj,
            series_id=series_id,
            title=title,
            include_style=not no_style,
            include_global_rules=not no_global_rules,
            include_glossary=not no_glossary,
            include_questions=questions,  # type: ignore[arg-type]
            allow_not_ready=allow_not_ready,
        )
    except ContextPackError as exc:
        raise BooktxError(exc.code, str(exc)) from exc
    write_context_pack(out_path, pack)
    return {
        "series_id": pack.series_id,
        "source": pack.source_language,
        "target": pack.target_language,
        "glossary": len(pack.glossary),
        "questions": len(pack.questions),
        "path": out_path,
        "format": pack.format,
        "version": pack.version,
        "allow_not_ready": allow_not_ready,
    }


def import_context_pack_workflow(
    runtime: RuntimeContext,
    *,
    file: Path,
    write: bool,
    init_missing_context: bool,
    conflict: str,
) -> tuple[SeriesContextPack, ContextPackImportResult, bool]:
    """Plan (or commit) a context-pack import; return pack, result, and wrote flag."""
    if conflict not in {"fail", "keep-local", "replace"}:
        raise _err(
            "import_pack_conflict", "--conflict must be fail, keep-local, or replace"
        )
    pack_path = _resolve_pack_path(str(file), runtime, must_exist=True)
    try:
        pack = read_context_pack(pack_path)
    except ContextPackError as exc:
        raise BooktxError(exc.code, str(exc)) from exc
    try:
        if write:
            _planned_ctx, result = import_context_pack(
                runtime.project,
                pack,
                conflict=conflict,  # type: ignore[arg-type]
                init_missing_context=init_missing_context,
            )
            wrote = True
        else:
            _planned_ctx, result = plan_context_pack_import(
                runtime.project,
                pack,
                conflict=conflict,  # type: ignore[arg-type]
                init_missing_context=init_missing_context,
            )
            wrote = False
    except ContextPackError as exc:
        raise BooktxError(exc.code, str(exc)) from exc
    return pack, result, wrote


def context_pack_import_payload(
    pack: SeriesContextPack, result: ContextPackImportResult, *, wrote: bool
) -> dict[str, object]:
    """Build the JSON payload for ``context import-pack --json``."""
    return {
        "series_id": pack.series_id,
        "source_language": pack.source_language,
        "target_language": pack.target_language,
        "changed": result.changed,
        "dry_run": not wrote,
        "summary": {
            "add": result.added,
            "update": result.updated,
            "skip": result.skipped,
            "conflict": result.conflicts,
            "error": result.errors,
            "warning": result.warnings,
        },
        "findings": [
            {
                "section": f.section,
                "key": f.key,
                "action": f.action,
                "message": f.message,
            }
            for f in result.findings
        ],
    }


def context_pack_import_has_failures(result: ContextPackImportResult) -> bool:
    return bool(result.errors or result.conflicts)


def context_sync_workflow(
    runtime: RuntimeContext,
    *,
    source_profile: str,
    target_profiles: list[str] | None,
    all_compatible: bool,
    sections: set[str],
    terms: list[str],
    question_ids: list[str],
    conflict: str,
    same_locale: bool,
    include_pass_through: bool,
    include_selection: bool,
    allow_not_ready: bool,
    init_missing_context: bool,
    write: bool,
) -> ContextSyncPlan:
    """Plan and optionally apply a multi-profile context sync."""

    if conflict not in {"fail", "keep-local", "replace"}:
        raise _err(
            "sync_conflict",
            "--conflict must be fail, keep-local, or replace",
        )
    try:
        plan = plan_context_sync_service(
            runtime.project.root,
            source_profile=source_profile,
            target_profiles=target_profiles,
            all_compatible=all_compatible,
            sections=sections,
            terms=terms,
            question_ids=question_ids,
            conflict=conflict,  # type: ignore[arg-type]
            same_locale=same_locale,
            include_pass_through=include_pass_through,
            include_selection=include_selection,
            allow_not_ready=allow_not_ready,
            init_missing_context=init_missing_context,
        )
        if write and not plan.blocked:
            return apply_context_sync_service(plan, runtime.project.root)
        return plan.model_copy(update={"write": write})
    except ContextSyncError as exc:
        raise BooktxError(exc.code, str(exc)) from exc


def import_md_workflow(
    proj: Project,
    ctx: TranslationContext,
    *,
    write: bool,
    replace_existing: bool,
    append_existing_lists: bool,
) -> dict[str, Any]:
    """Import chapter notes from context.md into context.json."""
    if replace_existing and append_existing_lists:
        raise _err(
            "import_md_flags",
            "--replace-existing and --append-existing-lists are mutually exclusive",
        )
    md_path = context_markdown_path(proj)
    if not md_path.is_file():
        raise _err("import_md_missing", "context.md is missing; nothing to import")
    try:
        imported = parse_context_markdown_chapter_notes(md_path.read_text("utf-8"))
    except ValueError as exc:
        raise _err(
            "import_md_parse", f"could not parse context.md chapter notes: {exc}"
        ) from exc
    hydrate_chapter_contexts_from_chapter_map(proj, imported)
    try:
        changed = merge_chapter_contexts(
            ctx,
            imported,
            replace_existing=replace_existing,
            append_existing_lists=append_existing_lists,
        )
    except ValueError as exc:
        raise _err("import_md_merge", str(exc)) from exc
    if write:
        write_context(proj, ctx)
        write_context_markdown(proj, ctx)
    return {
        "changed": list(changed),
        "wrote": write,
        "context_path": context_path(proj),
    }


def upsert_chapter_note_workflow(
    proj: Project,
    ctx: TranslationContext,
    *,
    chapter_id: str,
    title: str,
    source_summary: str,
    translation_summary: str,
    decision: list[str] | None,
    open_issue: list[str] | None,
    replace_decisions: bool,
    replace_open_issues: bool,
    replace_all: bool,
    force_discard_md_only: bool,
) -> str:
    """Create or update one chapter note in context.json."""
    if replace_all and (replace_decisions or replace_open_issues):
        raise _err(
            "chapter_note_replace_all_conflict",
            "--replace-all conflicts with "
            "--replace-decisions and --replace-open-issues",
        )
    try:
        ensure_context_markdown_safe_to_overwrite(
            proj, ctx, allow_discard_md_only=force_discard_md_only
        )
    except ValueError as exc:
        raise BooktxError(
            "chapter_note_drift",
            f"{exc} Pass --force-discard-md-only to overwrite anyway, or run "
            "`booktx context import-md . --write` first.",
        ) from exc
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
        replace_all=replace_all,
    )
    write_context(proj, ctx)
    write_context_markdown(proj, ctx)
    return f"updated chapter note: {chapter_id}"


__all__ = [
    "add_or_update_term_workflow",
    "add_question_workflow",
    "answer_question_workflow",
    "approve_question_workflow",
    "audit_term_workflow",
    "build_context_status_payload",
    "context_pack_import_has_failures",
    "context_pack_import_payload",
    "context_sync_workflow",
    "export_context_pack_workflow",
    "import_context_pack_workflow",
    "import_md_workflow",
    "init_context_workflow",
    "list_questions_lines",
    "load_context_or_die",
    "mandate_term_workflow",
    "mark_ready_workflow",
    "recommend_question_workflow",
    "remove_term_workflow",
    "render_context_command",
    "render_questionnaire_text",
    "reset_term_workflow",
    "upsert_chapter_note_workflow",
    "write_audit_blocks",
]
