"""Persistent translation context for a booktx project.

The translation contract in :mod:`booktx.models` preserves JSON structure,
record ids, placeholders, tags, and protected names. It does **not** preserve
translation *intent*: style, world terminology, and user-specific decisions.

This module owns the profile-local machine-readable translation context
(``translations/<profile>/context.json```) and the rendered human/agent view
(``translations/<profile>/context.md```) for normal profile workflows.
``context.json`` is authoritative; ``context.md`` is always regenerated from it.

The context is built deterministically and locally. It never calls an LLM, never
makes a network request, and never approves a glossary target on its own.
``ready=false`` means translation must not begin; the user (or an agent driving
the CLI on the user's behalf) must answer the required questions first.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from booktx.chapters import ChapterMap
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
    "baseline_payload",
    "baseline_sha256",
    "chapter_notes_before_target",
    "context_history_dir",
    "context_history_views_dir",
    "ensure_context_view_snapshot",
    "render_context_markdown",
    "write_context_markdown",
    "parse_context_markdown_chapter_notes",
    "chapter_contexts_equivalent",
    "ContextMarkdownDrift",
    "analyze_context_markdown_drift",
    "hydrate_chapter_contexts_from_chapter_map",
    "merge_chapter_contexts",
    "upsert_chapter_context",
    "ensure_context_markdown_safe_to_overwrite",
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


@dataclass(frozen=True, slots=True)
class ContextViewSnapshot:
    """Immutable task-scoped context view metadata."""

    context_view_sha256: str
    baseline_ref: str
    baseline_sha256: str
    notes_scope: str
    target_chapter_id: str
    notes_through_chapter_id: str | None
    note_chapter_ids: list[str]
    context_path: str
    context_md_path: str
    created_at: str


# --- paths ------------------------------------------------------------------


def context_path(project: Project) -> Path:
    """Path to the authoritative context JSON for the selected translation scope."""
    return project.context_json_path or (project.booktx_dir / "context.json")


def context_markdown_path(project: Project) -> Path:
    """Path to the rendered context markdown for the selected translation scope."""
    return project.context_md_path or (project.booktx_dir / "context.md")


def chapter_map_path(project: Project) -> Path:
    """Path to ``.booktx/chapter-map.json`` (owned by :mod:`booktx.chapters`)."""
    return project.chapter_map_path


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
    """Persist ``context`` to the active profile's ``context.json``."""
    from booktx.io_utils import write_json_text_atomic

    write_json_text_atomic(
        context_path(project),
        context.model_dump_json(indent=2, by_alias=True),
    )


def baseline_payload(context: TranslationContext) -> dict[str, object]:
    """Return the semantic baseline payload for version resolution."""
    data = context.model_dump(mode="json", by_alias=True)
    data.pop("chapter_contexts", None)
    return data


def baseline_sha256(context: TranslationContext) -> str:
    """Return the baseline hash excluding chronological chapter notes."""
    from booktx.versioning import canonical_json_sha256

    return canonical_json_sha256(baseline_payload(context))


def context_history_dir(project: Project) -> Path:
    """Return the history directory adjacent to the live context files."""
    return context_path(project).parent / "context-history"


def context_history_views_dir(project: Project) -> Path:
    """Return the directory containing immutable context view snapshots."""
    return context_history_dir(project) / "views"


def chapter_notes_before_target(
    chapter_map: ChapterMap,
    notes: list[ChapterContext],
    target_chapter_id: str,
) -> list[ChapterContext]:
    """Return prior chapter notes in chapter-map order for one target chapter."""
    ordered_ids = [chapter.chapter_id for chapter in chapter_map.chapters]
    try:
        target_index = ordered_ids.index(target_chapter_id)
    except ValueError as exc:
        raise ValueError(f"unknown target chapter id: {target_chapter_id}") from exc
    notes_by_id = {note.chapter_id: note for note in notes}
    return [
        notes_by_id[chapter_id].model_copy(deep=True)
        for chapter_id in ordered_ids[:target_index]
        if chapter_id in notes_by_id
    ]


def ensure_context_view_snapshot(
    project: Project,
    *,
    baseline_ref: str,
    baseline_sha256: str,
    target_chapter_id: str,
    notes_scope: str = "before_target_chapter",
) -> ContextViewSnapshot:
    """Compose and persist an immutable task context view snapshot."""
    from booktx.chapters import ensure_chapter_map, load_chapter_map
    from booktx.io_utils import utc_timestamp, write_json_text_atomic, write_text_atomic
    from booktx.versioning import canonical_json_sha256

    context = load_context(project)
    if context is None:
        raise ValueError("translation context is missing")

    chapter_map = load_chapter_map(project) or ensure_chapter_map(project)
    selected_notes = chapter_notes_before_target(
        chapter_map, context.chapter_contexts, target_chapter_id
    )
    view = context.model_copy(deep=True)
    view.chapter_contexts = selected_notes
    view_payload = view.model_dump(mode="json", by_alias=True)
    view_sha256 = canonical_json_sha256(view_payload)
    snapshot_dir = context_history_views_dir(project) / view_sha256
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    context_json_path = snapshot_dir / "context.json"
    context_md_path = snapshot_dir / "context.md"
    manifest_path = snapshot_dir / "manifest.json"

    context_json_text = json.dumps(view_payload, indent=2, ensure_ascii=False)
    if not context_json_text.endswith("\n"):
        context_json_text += "\n"
    context_md_text = render_context_markdown(view)
    context_rel = context_json_path.relative_to(project.root).as_posix()
    context_md_rel = context_md_path.relative_to(project.root).as_posix()
    note_ids = [note.chapter_id for note in selected_notes]
    notes_through = note_ids[-1] if note_ids else None

    expected_payload = {
        "version": 1,
        "context_view_sha256": view_sha256,
        "baseline_ref": baseline_ref,
        "baseline_sha256": baseline_sha256,
        "notes_scope": notes_scope,
        "target_chapter_id": target_chapter_id,
        "notes_through_chapter_id": notes_through,
        "note_chapter_ids": note_ids,
        "context_path": context_rel,
        "context_md_path": context_md_rel,
    }

    if context_json_path.is_file():
        existing = context_json_path.read_text("utf-8")
        if existing != context_json_text:
            raise ValueError(
                f"context snapshot collision for {view_sha256}: context.json differs"
            )
    else:
        write_json_text_atomic(context_json_path, context_json_text)

    if context_md_path.is_file():
        existing = context_md_path.read_text("utf-8")
        if existing != context_md_text:
            raise ValueError(
                f"context snapshot collision for {view_sha256}: context.md differs"
            )
    else:
        write_text_atomic(context_md_path, context_md_text)

    created_at = utc_timestamp()
    if manifest_path.is_file():
        existing_manifest = json.loads(manifest_path.read_text("utf-8"))
        comparable_keys = (
            "version",
            "context_view_sha256",
            "baseline_sha256",
            "notes_scope",
            "notes_through_chapter_id",
            "note_chapter_ids",
            "context_path",
            "context_md_path",
        )
        comparable = {k: existing_manifest.get(k) for k in comparable_keys}
        expected_comparable = {k: expected_payload[k] for k in comparable_keys}
        if comparable != expected_comparable:
            raise ValueError(
                f"context snapshot collision for {view_sha256}: manifest differs"
            )
        created_at = str(existing_manifest.get("created_at") or created_at)
    else:
        manifest_payload = {
            **expected_payload,
            "created_at": created_at,
        }
        write_json_text_atomic(
            manifest_path,
            json.dumps(manifest_payload, indent=2, ensure_ascii=False),
        )

    return ContextViewSnapshot(
        context_view_sha256=view_sha256,
        baseline_ref=baseline_ref,
        baseline_sha256=baseline_sha256,
        notes_scope=notes_scope,
        target_chapter_id=target_chapter_id,
        notes_through_chapter_id=notes_through,
        note_chapter_ids=note_ids,
        context_path=context_rel,
        context_md_path=context_md_rel,
        created_at=created_at,
    )


# --- seed questionnaire and glossary ----------------------------------------


def seed_questions() -> list[ContextQuestion]:
    """Generic initial questionnaire for any translation project.

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
            "Overall style: literal, balanced, or fluent literary?",
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
            "Q010",
            "typography",
            "Typography: quotation marks, em dashes, italics, "
            "placeholders, chapter titles.",
            False,
        ),
        (
            "Q011",
            "units",
            "Units: preserve original or convert to target locale conventions?",
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
    """Generic glossary seeds (empty by default).

    Book-specific seeds can be loaded via the ``--seed`` option on
    ``booktx context init``. The Shadows-of-Apt template is available as
    ``--seed shadows-of-apt``.
    """
    return []


def load_seed_template(name: str) -> tuple[list[ContextQuestion], list[GlossaryEntry]]:
    """Load book-specific seeds from a packaged template.

    Returns ``(extra_questions, glossary_entries)`` to merge into the default
    context. Raises ``FileNotFoundError`` for unknown template names.
    """
    import json
    from pathlib import Path

    template_dir = Path(__file__).parent / "templates"
    template_path = template_dir / f"{name}.json"
    if not template_path.is_file():
        raise FileNotFoundError(f"unknown seed template: {name}")
    data = json.loads(template_path.read_text("utf-8"))
    questions = [
        ContextQuestion(
            id=q["id"],
            topic=q["topic"],
            question=q["question"],
            required=q.get("required", True),
        )
        for q in data.get("questions", [])
    ]
    glossary = [
        GlossaryEntry(
            source=g["source"],
            target=g.get("target"),
            forbidden_targets=g.get("forbidden_targets", []),
            category=g.get("category", ""),
            status=g.get("status", "open"),
            notes=g.get("notes", ""),
            enforce=g.get("enforce", "warn"),
        )
        for g in data.get("glossary", [])
    ]
    return questions, glossary


def available_seed_templates() -> list[str]:
    """Return names of packaged seed templates."""
    from pathlib import Path

    template_dir = Path(__file__).parent / "templates"
    if not template_dir.is_dir():
        return []
    return sorted(p.stem for p in template_dir.glob("*.json"))


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
    """Render ``context`` to the active profile's ``context.md``."""
    from booktx.io_utils import write_text_atomic

    write_text_atomic(
        context_markdown_path(project),
        render_context_markdown(context),
    )


def _read_text_normalized(path: Path) -> str:
    """Read UTF-8 text and normalize all line endings to LF."""
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


# --- chapter note parsing and markdown drift --------------------------------


_CHAPTER_NOTE_HEADING_RE = re.compile(
    r"^(?P<id>\S+)(?:\s+(?P<sep>[\u2014\u2013-])\s+(?P<title>.+))?$"
)

_CHAPTER_NOTE_BULLETS: tuple[tuple[str, str], ...] = (
    ("- Source summary: ", "source_summary"),
    ("- Translation summary: ", "translation_summary"),
    ("- Decision: ", "decisions_added"),
    ("- Open issue: ", "open_issues"),
)

_CHAPTER_NOTES_HEADING = "## Chapter notes"


def parse_context_markdown_chapter_notes(markdown: str) -> list[ChapterContext]:
    """Parse the rendered ``## Chapter notes`` section from ``context.md``.

    Only the ``## Chapter notes`` section is parsed; parsing stops at the next
    level-2 heading. Chapter headings must match the rendered shape
    ``### 0006`` or ``### 0006 <separator> Title`` where ``<separator>`` is an
    em dash, en dash, or ASCII hyphen. The four bullet prefixes
    ``- Source summary:``, ``- Translation summary:``, ``- Decision:``, and
    ``- Open issue:`` are parsed exactly. Unknown non-empty content inside a
    chapter note raises :class:`ValueError`; nothing is silently mapped to
    open issues.
    """
    lines = markdown.split("\n")
    start: int | None = None
    for i, line in enumerate(lines):
        if line.strip() == _CHAPTER_NOTES_HEADING:
            start = i + 1
            break
    if start is None:
        return []
    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    chapters: list[ChapterContext] = []
    current: ChapterContext | None = None
    for raw in lines[start:end]:
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        if line.startswith("### "):
            match = _CHAPTER_NOTE_HEADING_RE.match(line[4:])
            if match is None:
                raise ValueError(f"unparsable chapter note heading: {line!r}")
            current = ChapterContext(
                chapter_id=match.group("id"),
                title=(match.group("title") or "").strip(),
            )
            chapters.append(current)
            continue
        if current is None:
            raise ValueError(f"unexpected content before first chapter note: {line!r}")
        matched = False
        for prefix, field_name in _CHAPTER_NOTE_BULLETS:
            if line.startswith(prefix):
                value = line[len(prefix) :]
                if field_name in ("decisions_added", "open_issues"):
                    getattr(current, field_name).append(value)
                else:
                    setattr(current, field_name, value)
                matched = True
                break
        if not matched:
            raise ValueError(
                f"unknown chapter note line for {current.chapter_id}: {line!r}"
            )
    return chapters


def chapter_contexts_equivalent(left: ChapterContext, right: ChapterContext) -> bool:
    """Return True when two chapter notes have the same durable content.

    ``chunk_ids`` is ignored because rendered Markdown does not include chunk
    ids; they are hydrated from ``chapter-map.json`` on import or upsert.
    """
    return (
        left.chapter_id == right.chapter_id
        and left.title == right.title
        and left.source_summary == right.source_summary
        and left.translation_summary == right.translation_summary
        and left.decisions_added == right.decisions_added
        and left.open_issues == right.open_issues
    )


class ContextMarkdownDrift(BaseModel):
    """Drift between rendered ``context.md`` chapter notes and ``context.json``."""

    model_config = ConfigDict(extra="forbid")

    missing_in_json: list[str] = Field(default_factory=list)
    conflicting: list[str] = Field(default_factory=list)
    parse_errors: list[str] = Field(default_factory=list)

    @property
    def unsafe_to_overwrite(self) -> bool:
        """True when overwriting ``context.md`` would discard Markdown-only notes."""
        return bool(self.missing_in_json or self.conflicting or self.parse_errors)


def analyze_context_markdown_drift(
    project: Project, context: TranslationContext
) -> ContextMarkdownDrift:
    """Compare existing ``context.md`` chapter notes with ``context.json``.

    If ``context.md`` is missing, no drift is reported. Parser failures populate
    ``parse_errors`` and make the overwrite unsafe.
    """
    md_path = context_markdown_path(project)
    if not md_path.is_file():
        return ContextMarkdownDrift()
    markdown = _read_text_normalized(md_path)
    try:
        parsed = parse_context_markdown_chapter_notes(markdown)
    except ValueError as exc:
        return ContextMarkdownDrift(parse_errors=[str(exc)])
    json_by_id = {ch.chapter_id: ch for ch in context.chapter_contexts}
    missing: list[str] = []
    conflicting: list[str] = []
    for note in parsed:
        existing = json_by_id.get(note.chapter_id)
        if existing is None:
            missing.append(note.chapter_id)
        elif not chapter_contexts_equivalent(note, existing):
            conflicting.append(note.chapter_id)
    return ContextMarkdownDrift(missing_in_json=missing, conflicting=conflicting)


def hydrate_chapter_contexts_from_chapter_map(
    project: Project, chapters: list[ChapterContext]
) -> None:
    """Fill ``title`` and ``chunk_ids`` from ``chapter-map.json`` where absent.

    Existing non-empty titles or chunk ids are never overwritten.
    """
    from booktx.chapters import load_chapter_map

    chapter_map = load_chapter_map(project)
    if chapter_map is None:
        return
    by_id = {ch.chapter_id: ch for ch in chapter_map.chapters}
    for note in chapters:
        mapped = by_id.get(note.chapter_id)
        if mapped is None:
            continue
        if not note.title and mapped.title:
            note.title = mapped.title
        if not note.chunk_ids and mapped.chunk_ids:
            note.chunk_ids = list(mapped.chunk_ids)


def merge_chapter_contexts(
    context: TranslationContext,
    imported: list[ChapterContext],
    *,
    replace_existing: bool = False,
    append_existing_lists: bool = False,
) -> list[str]:
    """Merge imported chapter notes into ``context`` and return changed ids.

    Default behavior adds notes whose chapter ids are absent, treats
    equivalent notes as no-ops, and refuses differing existing notes. Pass
    ``replace_existing`` to replace durable fields (preserving or hydrating
    ``chunk_ids``), or ``append_existing_lists`` to keep existing summaries
    unless empty and append non-duplicate decisions and open issues in order.
    The two modes are mutually exclusive.
    """
    if replace_existing and append_existing_lists:
        raise ValueError(
            "replace_existing and append_existing_lists are mutually exclusive"
        )
    existing_by_id = {ch.chapter_id: ch for ch in context.chapter_contexts}
    changed: list[str] = []
    for note in imported:
        current = existing_by_id.get(note.chapter_id)
        if current is None:
            context.chapter_contexts.append(note)
            existing_by_id[note.chapter_id] = note
            changed.append(note.chapter_id)
            continue
        if chapter_contexts_equivalent(note, current):
            continue
        if replace_existing:
            chunk_ids = current.chunk_ids or list(note.chunk_ids)
            current.title = note.title
            current.source_summary = note.source_summary
            current.translation_summary = note.translation_summary
            current.decisions_added = list(note.decisions_added)
            current.open_issues = list(note.open_issues)
            current.chunk_ids = chunk_ids
            changed.append(note.chapter_id)
        elif append_existing_lists:
            modified = False
            if not current.title and note.title:
                current.title = note.title
                modified = True
            if not current.source_summary and note.source_summary:
                current.source_summary = note.source_summary
                modified = True
            if not current.translation_summary and note.translation_summary:
                current.translation_summary = note.translation_summary
                modified = True
            for dec in note.decisions_added:
                if dec not in current.decisions_added:
                    current.decisions_added.append(dec)
                    modified = True
            for issue in note.open_issues:
                if issue not in current.open_issues:
                    current.open_issues.append(issue)
                    modified = True
            if modified:
                changed.append(note.chapter_id)
        else:
            raise ValueError(
                f"chapter {note.chapter_id} differs from context.json; "
                "pass --replace-existing or --append-existing-lists"
            )
    return changed


def upsert_chapter_context(
    context: TranslationContext,
    note: ChapterContext,
    *,
    replace_decisions: bool = False,
    replace_open_issues: bool = False,
) -> None:
    """Create or update one chapter note.

    Title and summaries update only when provided. Decisions and open issues
    append by default (avoiding exact duplicates) and replace only when the
    matching replace flag is true.
    """
    existing: ChapterContext | None = None
    for ch in context.chapter_contexts:
        if ch.chapter_id == note.chapter_id:
            existing = ch
            break
    if existing is None:
        context.chapter_contexts.append(note)
        return
    if note.title:
        existing.title = note.title
    if note.source_summary:
        existing.source_summary = note.source_summary
    if note.translation_summary:
        existing.translation_summary = note.translation_summary
    if replace_decisions:
        existing.decisions_added = list(note.decisions_added)
    else:
        for dec in note.decisions_added:
            if dec not in existing.decisions_added:
                existing.decisions_added.append(dec)
    if replace_open_issues:
        existing.open_issues = list(note.open_issues)
    else:
        for issue in note.open_issues:
            if issue not in existing.open_issues:
                existing.open_issues.append(issue)


def ensure_context_markdown_safe_to_overwrite(
    project: Project,
    context: TranslationContext,
    *,
    allow_discard_md_only: bool = False,
) -> None:
    """Raise ``ValueError`` if writing ``context.md`` would discard notes.

    Runs drift analysis against the existing ``context.md``. Pass
    "allow_discard_md_only=True`` for commands whose purpose is to overwrite
    Markdown despite unsafe drift.
    """
    drift = analyze_context_markdown_drift(project, context)
    if not drift.unsafe_to_overwrite or allow_discard_md_only:
        return
    parts: list[str] = []
    if drift.missing_in_json:
        parts.append(f"missing_in_json: {', '.join(drift.missing_in_json)}")
    if drift.conflicting:
        parts.append(f"conflicting: {', '.join(drift.conflicting)}")
    if drift.parse_errors:
        parts.append(f"parse_errors: {'; '.join(drift.parse_errors)}")
    raise ValueError(
        "context.md contains chapter notes that are not safely represented "
        "in context.json. " + "; ".join(parts)
    )
