"""Context organization audits for translation policy clarity."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from booktx.context import ChapterContext, GlossaryEntry, TranslationContext
from booktx.glossary_match import entry_is_binding

ARROW_RE = re.compile(
    r"(?P<source>[^\n.;:,()\[\]{}]{2,80}?)\s*(?:->|=>|→)\s*(?P<target>[^\n.;,()\[\]{}]{1,80})"
)
STYLE_QUESTION_IDS = {"Q001", "Q002", "Q003", "Q004", "Q010", "Q011"}
TERM_QUESTION_IDS = {"Q005", "Q006"}
LEGACY_LAYOUT_MARKERS = (
    "## Answered questions",
    "## Binding glossary",
    "## Advisory glossary",
    "## Disabled glossary rules",
    "## Mandatory glossary",
)


class ContextOrganizationIssue(BaseModel):
    """One stable context organization finding."""

    code: str
    severity: Literal["info", "warning", "error"]
    profile: str | None = None
    location: str
    message: str
    evidence: dict[str, object] = Field(default_factory=dict)
    suggested_action: str = ""


def _norm(text: str) -> str:
    return " ".join(text.casefold().strip().split())


def _arrow_pairs(text: str) -> list[dict[str, str]]:
    pairs: list[dict[str, str]] = []
    for match in ARROW_RE.finditer(text):
        source = match.group("source").strip(" -•\t")
        target = match.group("target").strip(" -•\t")
        if source and target:
            pairs.append({"source": source, "target": target})
    return pairs


def _issue(
    code: str,
    severity: Literal["info", "warning", "error"],
    *,
    profile: str | None,
    location: str,
    message: str,
    evidence: dict[str, object] | None = None,
    suggested_action: str = "",
) -> ContextOrganizationIssue:
    return ContextOrganizationIssue(
        code=code,
        severity=severity,
        profile=profile,
        location=location,
        message=message,
        evidence=evidence or {},
        suggested_action=suggested_action,
    )


def audit_context_organization(
    ctx: TranslationContext,
    *,
    profile: str | None = None,
    rendered_markdown: str | None = None,
) -> list[ContextOrganizationIssue]:
    """Return report-only organization issues for one context."""
    issues: list[ContextOrganizationIssue] = []

    for q in ctx.questions:
        if q.id in STYLE_QUESTION_IDS and q.status == "answered" and q.answer:
            issues.append(
                _issue(
                    "style_question_rendered_in_prompt",
                    "info",
                    profile=profile,
                    location=f"questions.{q.id}",
                    message=(
                        f"{q.id} duplicates structured style policy when rendered "
                        "in the full agent prompt."
                    ),
                    evidence={"question_id": q.id, "topic": q.topic},
                    suggested_action=(
                        "Use the effective render for agent prompts, and keep "
                        "answered questions as provenance."
                    ),
                )
            )
        if q.id in TERM_QUESTION_IDS and q.answer:
            pairs = _arrow_pairs(q.answer)
            if pairs:
                issues.append(
                    _issue(
                        "question_contains_term_arrow",
                        "warning",
                        profile=profile,
                        location=f"questions.{q.id}.answer",
                        message=f"{q.id} contains terminology-like arrow decisions.",
                        evidence={"question_id": q.id, "pairs": pairs},
                        suggested_action=(
                            "Review these candidates and promote approved mandatory "
                            "decisions to structured glossary or name policy."
                        ),
                    )
                )

    for idx, entry in enumerate(ctx.glossary):
        if entry.target and entry.enforce != "off" and not entry_is_binding(entry):
            issues.append(
                _issue(
                    "advisory_entry_looks_binding",
                    "warning",
                    profile=profile,
                    location=f"glossary[{idx}]",
                    message=(
                        f"Glossary entry {entry.source!r} has target {entry.target!r} "
                        f"and enforce={entry.enforce!r}, but validation does not "
                        "require the approved target."
                    ),
                    evidence={
                        "source": entry.source,
                        "target": entry.target,
                        "enforce": entry.enforce,
                        "require_target": entry.require_target,
                        "forbidden_targets": entry.forbidden_targets,
                    },
                    suggested_action=(
                        "If this is mandatory, set require_target or configure "
                        "forbidden targets with an enforcing rule."
                    ),
                )
            )

    forbidden_by_source: dict[str, tuple[GlossaryEntry, set[str]]] = {}
    for entry in ctx.glossary:
        names = {_norm(entry.source), *{_norm(v) for v in entry.source_variants}}
        forbidden = {_norm(t) for t in entry.forbidden_targets}
        for name in names:
            forbidden_by_source[name] = (entry, forbidden)

    for ch in ctx.chapter_contexts:
        for field_name, text in _chapter_arrow_sources(ch):
            pairs = _arrow_pairs(text)
            if pairs:
                issues.append(
                    _issue(
                        "chapter_note_contains_term_arrow",
                        "warning",
                        profile=profile,
                        location=f"chapter_contexts.{ch.chapter_id}.{field_name}",
                        message=(
                            f"Chapter {ch.chapter_id} contains terminology-like "
                            "arrow decisions in chapter memory."
                        ),
                        evidence={"chapter_id": ch.chapter_id, "pairs": pairs},
                        suggested_action=(
                            "Treat these as candidates or historical notes unless "
                            "they are promoted to structured policy."
                        ),
                    )
                )
            for pair in pairs:
                source_key = _norm(pair["source"])
                target_key = _norm(pair["target"])
                entry_info = forbidden_by_source.get(source_key)
                if entry_info is None:
                    continue
                entry, forbidden = entry_info
                if target_key in forbidden:
                    issues.append(
                        _issue(
                            "chapter_note_conflicts_with_forbidden_target",
                            "warning",
                            profile=profile,
                            location=f"chapter_contexts.{ch.chapter_id}.{field_name}",
                            message=(
                                f"Chapter note maps {pair['source']!r} to "
                                f"{pair['target']!r}, which is forbidden by "
                                "the structured glossary."
                            ),
                            evidence={
                                "chapter_id": ch.chapter_id,
                                "source": pair["source"],
                                "target": pair["target"],
                                "glossary_source": entry.source,
                                "forbidden_targets": entry.forbidden_targets,
                            },
                            suggested_action=(
                                "Mark the chapter note as superseded or revise it "
                                "after user approval."
                            ),
                        )
                    )

    if rendered_markdown:
        markers = [m for m in LEGACY_LAYOUT_MARKERS if m in rendered_markdown]
        if markers:
            issues.append(
                _issue(
                    "context_markdown_uses_legacy_layout",
                    "info",
                    profile=profile,
                    location="context.md",
                    message="context.md uses sections that the effective view avoids.",
                    evidence={"markers": markers},
                    suggested_action=(
                        "Inspect `booktx context render . --view effective --stdout` "
                        "for the cleaner agent prompt."
                    ),
                )
            )

    return issues


def _chapter_arrow_sources(chapter: ChapterContext) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    if chapter.translation_summary:
        fields.append(("translation_summary", chapter.translation_summary))
    for idx, decision in enumerate(chapter.decisions_added):
        fields.append((f"decisions_added[{idx}]", decision))
    return fields


def compare_profile_contexts(
    contexts: Mapping[str, TranslationContext],
) -> list[ContextOrganizationIssue]:
    """Return report-only cross-profile organization issues."""
    issues: list[ContextOrganizationIssue] = []
    sources_by_profile: dict[str, dict[str, GlossaryEntry]] = {}
    display_source: dict[str, str] = {}

    for profile, ctx in contexts.items():
        entries: dict[str, GlossaryEntry] = {}
        for entry in ctx.glossary:
            key = _norm(entry.source)
            entries[key] = entry
            display_source.setdefault(key, entry.source)
        sources_by_profile[profile] = entries

    all_sources = sorted(display_source)
    profiles = sorted(contexts)
    for source_key in all_sources:
        present = [p for p in profiles if source_key in sources_by_profile[p]]
        missing = [p for p in profiles if source_key not in sources_by_profile[p]]
        if present and missing:
            for profile in missing:
                issues.append(
                    _issue(
                        "profile_missing_glossary_term",
                        "warning",
                        profile=profile,
                        location="glossary",
                        message=(
                            f"Glossary term {display_source[source_key]!r} exists "
                            "in sibling profiles but is missing here."
                        ),
                        evidence={
                            "source": display_source[source_key],
                            "present_profiles": present,
                            "missing_profiles": missing,
                        },
                        suggested_action=(
                            "Review whether this profile should receive the shared "
                            "term through context sync or an explicit local decision."
                        ),
                    )
                )

        targets: dict[str, list[str]] = {}
        for profile in present:
            target = sources_by_profile[profile][source_key].target
            if target:
                targets.setdefault(target, []).append(profile)
        if len(targets) > 1:
            for profile in present:
                entry = sources_by_profile[profile][source_key]
                issues.append(
                    _issue(
                        "profile_glossary_target_divergence",
                        "warning",
                        profile=profile,
                        location=f"glossary.{entry.source}",
                        message=(
                            f"Glossary term {entry.source!r} has different targets "
                            "across sibling profiles."
                        ),
                        evidence={"source": entry.source, "targets": targets},
                        suggested_action=(
                            "Resolve the intended target with the user, then sync or "
                            "document profile-specific overrides."
                        ),
                    )
                )

    return issues


def render_context_organization_report(
    issues: list[ContextOrganizationIssue],
    *,
    title: str = "Context organization report",
) -> str:
    """Render issues as a deterministic Markdown report."""
    counts = {"error": 0, "warning": 0, "info": 0}
    for issue in issues:
        counts[issue.severity] += 1

    lines = [
        f"# {title}",
        "",
        "## Summary",
        "",
        f"- errors: {counts['error']}",
        f"- warnings: {counts['warning']}",
        f"- info: {counts['info']}",
        f"- total: {len(issues)}",
        "",
        "## Issues",
        "",
    ]
    if not issues:
        lines.append("No context organization issues found.")
        lines.append("")
        return "\n".join(lines)

    for issue in issues:
        profile = f" [{issue.profile}]" if issue.profile else ""
        lines.extend(
            [
                f"### {issue.severity.upper()}: {issue.code}{profile}",
                "",
                f"- Location: `{issue.location}`",
                f"- Message: {issue.message}",
            ]
        )
        if issue.suggested_action:
            lines.append(f"- Suggested action: {issue.suggested_action}")
        if issue.evidence:
            lines.append(f"- Evidence: `{issue.evidence}`")
        lines.append("")
    return "\n".join(lines)


def safe_report_path(path: Path) -> None:
    """Reject report paths that violate the no-/tmp policy."""
    resolved = path.expanduser().resolve()
    if resolved == Path("/tmp") or Path("/tmp") in resolved.parents:
        raise ValueError("context organization reports must not be written under /tmp")
