"""Persistent translation context for a booktx project.

The translation contract in :mod:`booktx.models` preserves JSON structure,
record ids, placeholders, tags, and protected names. It does **not** preserve
translation *intent*: style, world terminology, and user-specific decisions.

This module owns the machine-readable translation context
(``.booktx/context.json``) and the rendered human/agent view
(``.booktx/context.md``). ``context.json`` is authoritative; ``context.md`` is
always regenerated from it.

The context is built deterministically and locally. It never calls an LLM, never
makes a network request, and never approves a glossary target on its own.
``ready=false`` means translation must not begin; the user (or an agent driving
the CLI on the user's behalf) must answer the required questions first.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from booktx.config import Project

__all__ = [
    "StyleProfile",
    "GlossaryEntry",
    "ContextQuestion",
    "ChapterContext",
    "TranslationContext",
    "apply_answer_to_context",
    "context_path",
    "context_markdown_path",
    "chapter_map_path",
    "load_context",
    "write_context",
    "default_context",
    "render_context_markdown",
    "write_context_markdown",
    "seed_questions",
    "seed_glossary",
]


# --- models -----------------------------------------------------------------


class StyleProfile(BaseModel):
    """User-approved style decisions for the translation."""

    # ``register`` is a real linguistic term but it shadows a reserved
    # pydantic attribute name, so the Python field uses ``register_level``
    # and serializes to the JSON key ``register``.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    target_locale: str = "de-DE"
    formality: Literal["informal", "neutral", "formal"] = "neutral"
    register_level: str = Field(default="", alias="register")
    prose_style: str = ""
    dialogue_style: str = ""
    sentence_policy: str = (
        "Prefer natural German prose; preserve meaning over word-for-word syntax."
    )
    punctuation_policy: str = ""
    units_policy: str = "Keep source units unless the user says otherwise."


class GlossaryEntry(BaseModel):
    """One glossary term with optional approved target and forbidden targets."""

    model_config = ConfigDict(extra="forbid")

    source: str
    target: str | None = None
    forbidden_targets: list[str] = Field(default_factory=list)
    category: str = "term"  # term, place, people, kinden, title, object, concept
    status: Literal["open", "approved", "rejected"] = "open"
    notes: str = ""
    examples: list[str] = Field(default_factory=list)
    case_sensitive: bool = False
    enforce: Literal["off", "warn", "error"] = "warn"


class ContextQuestion(BaseModel):
    """One question that may need a user answer before translation can begin.

    ``required`` questions gate readiness: a context cannot be marked ready while
    any required question is still open.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    topic: str
    question: str
    answer: str | None = None
    status: Literal["open", "answered"] = "open"
    required: bool = True


class ChapterContext(BaseModel):
    """Per-chapter notes appended as the agent completes chapters."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str
    title: str = ""
    chunk_ids: list[str] = Field(default_factory=list)
    source_summary: str = ""
    translation_summary: str = ""
    decisions_added: list[str] = Field(default_factory=list)
    open_issues: list[str] = Field(default_factory=list)


class TranslationContext(BaseModel):
    """The authoritative translation context for one project."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    source_language: str
    target_language: str
    source_title: str = ""
    source_author: str = ""
    source_sha256: str = ""
    ready: bool = False
    style: StyleProfile = Field(default_factory=StyleProfile)
    global_rules: list[str] = Field(default_factory=list)
    glossary: list[GlossaryEntry] = Field(default_factory=list)
    questions: list[ContextQuestion] = Field(default_factory=list)
    chapter_contexts: list[ChapterContext] = Field(default_factory=list)


# --- paths ------------------------------------------------------------------


def context_path(project: Project) -> Path:
    """Path to the authoritative ``.booktx/context.json``."""
    return project.booktx_dir / "context.json"


def context_markdown_path(project: Project) -> Path:
    """Path to the rendered ``.booktx/context.md``."""
    return project.booktx_dir / "context.md"


def chapter_map_path(project: Project) -> Path:
    """Path to ``.booktx/chapter-map.json`` (owned by :mod:`booktx.chapters`)."""
    return project.booktx_dir / "chapter-map.json"


# --- IO ---------------------------------------------------------------------


def load_context(project: Project) -> TranslationContext | None:
    """Load the context, or return ``None`` if no context file exists.

    A corrupt context raises a :class:`ValueError` so the caller can surface it
    rather than silently dropping the user's decisions.
    """
    path = context_path(project)
    if not path.is_file():
        return None
    return TranslationContext.model_validate_json(path.read_text("utf-8"))


def write_context(project: Project, context: TranslationContext) -> None:
    """Persist ``context`` to ``.booktx/context.json``."""
    path = context_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        context.model_dump_json(indent=2, by_alias=True) + "\n", encoding="utf-8"
    )


# --- seed questionnaire and glossary ----------------------------------------


def seed_questions() -> list[ContextQuestion]:
    """Deterministic initial questionnaire (mirrors the handoff).

    The questions marked ``required=True`` gate ``ready``. Typography and units
    are left optional: they affect style but a missing answer does not block the
    terminology safety that the context gate exists to provide.
    """
    items: list[tuple[str, str, str, bool]] = [
        (
            "Q001",
            "target_locale",
            "Target language and locale (e.g. German Germany de-DE, "
            "Austrian German de-AT, Swiss German de-CH).",
            True,
        ),
        (
            "Q002",
            "overall_style",
            "Overall style: literal, balanced, or fluent literary German?",
            True,
        ),
        (
            "Q003",
            "register",
            "Register: neutral, elevated, gritty, archaic, YA-like, etc.?",
            True,
        ),
        (
            "Q004",
            "dialogue_style",
            "Dialogue style: modern/natural vs. stylized; how to handle "
            "insults, slang, and contractions?",
            True,
        ),
        (
            "Q005",
            "names",
            "Which names/titles/place names must remain unchanged?",
            True,
        ),
        (
            "Q006",
            "world_terms",
            "Invented world terms: translate, partially translate, or keep "
            "source form?",
            True,
        ),
        (
            "Q007",
            "kinden",
            "Species/culture terms: how to handle Wasp-kinden, Ant-kinden, "
            "Spider-kinden, Fly-kinden, etc.?",
            True,
        ),
        (
            "Q008",
            "honorifics",
            "Honorifics: keep Sieur, translate it, or use a German equivalent?",
            True,
        ),
        (
            "Q009",
            "place_geopolitical",
            "Place/geopolitical terms: especially Lowlands, Lowlander, Lowland cities.",
            True,
        ),
        (
            "Q010",
            "typography",
            "Typography: German quotation marks, em dashes, italics "
            "placeholders, chapter titles.",
            False,
        ),
        (
            "Q011",
            "units",
            "Units: preserve feet/miles or convert?",
            False,
        ),
        (
            "Q012",
            "glossary_enforcement",
            "Glossary enforcement: should forbidden terms be validation "
            "warnings or errors?",
            True,
        ),
    ]
    return [
        ContextQuestion(id=qid, topic=topic, question=text, required=required)
        for qid, topic, text, required in items
    ]


def seed_glossary() -> list[GlossaryEntry]:
    """Deterministic book-specific glossary seeds (all ``status='open'``).

    No target is approved. The Lowlands/Lowlander entries pin the forbidden
    real-world-Netherlands renderings that motivated this feature.
    """
    return [
        GlossaryEntry(
            source="Lowlands",
            target=None,
            forbidden_targets=["Niederlande", "die Niederlande", "Holland"],
            category="place",
            status="open",
            notes=(
                "Fantasy-world geopolitical term; do not use the real-world "
                "Netherlands meaning. Ask the user for the approved rendering."
            ),
            enforce="error",
        ),
        GlossaryEntry(
            source="Lowlander",
            target=None,
            forbidden_targets=["Niederländer", "Holländer"],
            category="demonym",
            status="open",
            notes=(
                "Demonym for Lowlands. German target must avoid the "
                "Dutch/Nederlander meaning."
            ),
            enforce="error",
        ),
    ]


def default_context(project: Project, source_sha256: str = "") -> TranslationContext:
    """Build a not-ready context pre-filled from the project config.

    Source/target languages and the source digest are taken from the project.
    Style and glossary start empty/seeded; required questions are open.
    """
    return TranslationContext(
        source_language=project.config.source_language,
        target_language=project.config.target_language,
        source_sha256=source_sha256,
        ready=False,
        style=StyleProfile(
            target_locale=project.config.target_language or "de-DE",
        ),
        glossary=seed_glossary(),
        questions=seed_questions(),
    )


def apply_answer_to_context(
    context: TranslationContext, question_id: str, text: str
) -> None:
    value = text.strip()
    if not value:
        return
    if question_id == "Q001":
        context.style.target_locale = value
    elif question_id == "Q002":
        context.style.prose_style = value
    elif question_id == "Q003":
        context.style.register_level = value
    elif question_id == "Q004":
        context.style.dialogue_style = value
    elif question_id == "Q010":
        context.style.punctuation_policy = value
    elif question_id == "Q011":
        context.style.units_policy = value


# --- markdown rendering ------------------------------------------------------


def _escape_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _status_label(ready: bool) -> str:
    return "READY" if ready else "NOT READY"


def _render_header(context: TranslationContext) -> list[str]:
    style = context.style
    lines: list[str] = [
        "# booktx translation context",
        "",
        f"Status: {_status_label(context.ready)}",
        f"Source language: {context.source_language}",
        f"Target language: {context.target_language}",
    ]
    if style.target_locale:
        lines.append(f"Target locale: {style.target_locale}")
    if context.source_title:
        lines.append(f"Source title: {context.source_title}")
    if context.source_author:
        lines.append(f"Source author: {context.source_author}")
    lines.append("")
    return lines


def _render_style_section(style: StyleProfile) -> list[str]:
    dialogue_default = "natural dialogue, preserve character voice"
    lines = [
        "## Style",
        "",
        f"- Formality: {style.formality}",
        f"- Prose style: {style.prose_style or 'fluent literary, not word-for-word'}",
        f"- Dialogue: {style.dialogue_style or dialogue_default}",
        f"- Sentence policy: {style.sentence_policy}",
    ]
    if style.register_level:
        lines.append(f"- Register: {style.register_level}")
    if style.punctuation_policy:
        lines.append(f"- Punctuation: {style.punctuation_policy}")
    lines.append(f"- Units: {style.units_policy}")
    lines.append("")
    return lines


def _render_glossary_section(glossary: list[GlossaryEntry]) -> list[str]:
    lines = [
        "## Mandatory glossary",
        "",
        "| Source | Approved target | Forbidden targets | Status | Notes |",
        "|---|---|---|---|---|",
    ]
    if glossary:
        for entry in glossary:
            approved = entry.target if entry.target else "<open>"
            if entry.forbidden_targets:
                forbidden = "; ".join(entry.forbidden_targets)
            else:
                forbidden = ""
            notes = entry.notes or ""
            lines.append(
                f"| {_escape_cell(entry.source)} "
                f"| {_escape_cell(approved)} "
                f"| {_escape_cell(forbidden)} "
                f"| {entry.status} "
                f"| {_escape_cell(notes)} |"
            )
    else:
        lines.append("| _(no glossary entries yet)_ | | | | |")
    lines.append("")
    return lines


def _render_questions_section(questions: list[ContextQuestion]) -> list[str]:
    lines: list[str] = []
    open_q = [q for q in questions if q.status == "open"]
    if open_q:
        lines += ["## Open questions", ""]
        for q in open_q:
            lines.append(f"- {q.id}: {q.question}")
        lines.append("")
    answered = [q for q in questions if q.status == "answered"]
    if answered:
        lines += ["## Answered questions", ""]
        for q in answered:
            ans = q.answer or ""
            lines.append(f"- {q.id}: {q.question} -> {ans}")
        lines.append("")
    return lines


def _render_chapter_notes_section(chapters: list[ChapterContext]) -> list[str]:
    if not chapters:
        return []
    lines = ["## Chapter notes", ""]
    for ch in chapters:
        title = f" \u2014 {ch.title}" if ch.title else ""
        lines.append(f"### {ch.chapter_id}{title}")
        if ch.source_summary:
            lines.append(f"- Source summary: {ch.source_summary}")
        if ch.translation_summary:
            lines.append(f"- Translation summary: {ch.translation_summary}")
        for issue in ch.open_issues:
            lines.append(f"- Open issue: {issue}")
        for dec in ch.decisions_added:
            lines.append(f"- Decision: {dec}")
    lines.append("")
    return lines


def render_context_markdown(context: TranslationContext) -> str:
    """Render a short, agent-readable markdown view of ``context``.

    The markdown is always derived from ``context``; it is never authoritative.
    """
    lines: list[str] = []
    lines.extend(_render_header(context))
    lines.extend(_render_style_section(context.style))
    lines.extend(_render_glossary_section(context.glossary))
    lines.extend(_render_questions_section(context.questions))
    if context.global_rules:
        lines += ["## Global rules", ""]
        for rule in context.global_rules:
            lines.append(f"- {rule}")
        lines.append("")
    lines.extend(_render_chapter_notes_section(context.chapter_contexts))
    lines += [
        "## Rules for agents",
        "",
        "- Read this file before translating every new chapter.",
        "- Glossary rules override dictionary translations.",
        "- Do not use forbidden target terms.",
        "- Update chapter notes after completing a chapter.",
        "",
    ]
    return "\n".join(lines)


def write_context_markdown(project: Project, context: TranslationContext) -> None:
    """Render ``context`` to ``.booktx/context.md``."""
    path = context_markdown_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_context_markdown(context), encoding="utf-8")
