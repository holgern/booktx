"""Series-wide translation context packs.

A translator may work on several books from the same series. Translation policy
must stay consistent across those books without mixing record-level
translations, tasks, profiles, source-extraction state, or model experiments.

A *series context pack* is a reusable, schema-validated policy artifact exported
from one approved profile context and explicitly imported into another book's
profile before new translation tasks are created. Version 1 carries
profile-local translation policy only:

- style
- global rules
- glossary entries
- approved reusable question answers

The pack never carries records, candidates, tasks, todos, ingest files,
generated output, reports, chapter contexts, source identity, or profile
identity. It is structurally separate from :class:`booktx.context.TranslationContext`
and excludes readiness, source-book, and chapter fields by construction.

``schema-validated`` does not mean authenticated. A version 1 pack is not signed;
users must trust the file's origin.

This module owns:

- strict pack models and semantic validation
- pack read/write
- export selection and provenance
- pure import planning
- deterministic merge helpers
- findings and summaries
- readiness invalidation

CLI presentation and exit handling live in :mod:`booktx.cli`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from booktx.config import BooktxError
from booktx.context import (
    ContextQuestion,
    GlossaryEntry,
    StyleProfile,
    TranslationContext,
    apply_answer_to_context,
    baseline_payload,
    baseline_sha256,
    default_context,
    ensure_context_markdown_safe_to_overwrite,
    load_context,
    next_question_id,
)

if TYPE_CHECKING:
    from booktx.config import Project

__all__ = [
    "ContextPackSource",
    "SeriesContextPack",
    "ContextPackImportFinding",
    "ContextPackImportResult",
    "ContextPackError",
    "PackConflictMode",
    "PackQuestionInclusion",
    "CORE_QUESTION_STYLE_FIELDS",
    "collapse_whitespace",
    "glossary_identity",
    "question_identity",
    "normalize_glossary_entry",
    "validate_pack_glossary",
    "validate_pack_questions",
    "parse_context_pack",
    "read_context_pack",
    "write_context_pack",
    "export_context_pack",
    "plan_context_pack_import",
    "import_context_pack",
    "has_unfinished_tasks",
]


PackConflictMode = Literal["fail", "keep-local", "replace"]
PackQuestionInclusion = Literal["none", "approved"]


# Core questions whose answers map to a :class:`StyleProfile` field via
# :func:`booktx.context.apply_answer_to_context`. A pack's core-question answers
# must agree with the corresponding pack style values, so question application
# during import cannot silently bypass style-conflict handling.
CORE_QUESTION_STYLE_FIELDS: dict[str, str] = {
    "Q001": "target_locale",
    "Q002": "prose_style",
    "Q003": "register_level",
    "Q004": "dialogue_style",
    "Q010": "punctuation_policy",
    "Q011": "units_policy",
}

_SERIES_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ContextPackError(BooktxError):
    """A series-context-pack error with a stable ``code``.

    Subclasses :class:`booktx.config.BooktxError` so the CLI's existing
    ``_handle_booktx_error`` handler renders it consistently.
    """


# --- normalization helpers ---------------------------------------------------


def collapse_whitespace(text: str) -> str:
    """Trim ``text`` and collapse internal whitespace runs to single spaces."""
    return " ".join(text.split())


def glossary_identity(entry: GlossaryEntry) -> str:
    """Return the v1 glossary identity: the trimmed, casefolded source term.

    ``category`` and ``case_sensitive`` are fields, not identity components.
    """
    return entry.source.strip().casefold()


def question_identity(question: ContextQuestion) -> tuple[str, str]:
    """Return the v1 question semantic identity.

    Identity is the whitespace-collapsed ``topic`` and ``question`` text.
    Comparison is case-sensitive. Question ids (Q001, S001, ...) are not
    portable identities.
    """
    return (
        collapse_whitespace(question.topic),
        collapse_whitespace(question.question),
    )


def _term_equal(a: str, b: str, *, case_sensitive: bool) -> bool:
    return a == b if case_sensitive else a.casefold() == b.casefold()


def _dedupe_trimmed(values: list[str]) -> list[str]:
    """Trim and drop empty strings; preserve first-seen order (case-sensitive)."""
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        value = raw.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def normalize_glossary_entry(entry: GlossaryEntry) -> GlossaryEntry:
    """Return a copy of ``entry`` with whitespace-normalized string fields.

    Trims the source term, variants, target, target variants, forbidden
    targets, category, and notes. Drops empty variant/forbidden strings and
    case-sensitive duplicates within each list. Does not alter semantics such
    as the approved-target-also-forbidden relationship or enforcement; those
    are validated separately by :func:`validate_pack_glossary`.
    """
    return entry.model_copy(
        update={
            "source": entry.source.strip(),
            "source_variants": _dedupe_trimmed(entry.source_variants),
            "target": (entry.target or "").strip() or None,
            "target_variants": _dedupe_trimmed(entry.target_variants),
            "forbidden_targets": _dedupe_trimmed(entry.forbidden_targets),
            "category": entry.category.strip(),
            "notes": entry.notes.strip(),
            "examples": _dedupe_trimmed(entry.examples),
        }
    )


# --- pack models -------------------------------------------------------------


class ContextPackSource(BaseModel):
    """Provenance metadata about the profile context a pack was exported from.

    ``context_sha256`` and ``baseline_sha256`` are provenance only. Import does
    not use them as evidence that the target book has the same source or
    context.
    """

    model_config = ConfigDict(extra="forbid")

    project_profile: str = ""
    context_sha256: str = ""
    baseline_sha256: str = ""


class SeriesContextPack(BaseModel):
    """A version 1 series-wide translation context pack.

    Carries only reusable profile-local translation policy. Excludes readiness,
    source-book, and chapter fields by construction. Strict semantic validation
    runs at model validation time via :func:`validate_pack_glossary` and
    :func:`validate_pack_questions`.
    """

    model_config = ConfigDict(extra="forbid")

    format: Literal["booktx.series-context-pack"] = "booktx.series-context-pack"
    version: Literal[1] = 1

    series_id: str
    title: str = ""
    source_language: str
    target_language: str
    target_locale: str = ""

    created_at: str
    created_by: str = ""
    source: ContextPackSource = Field(default_factory=ContextPackSource)

    style: StyleProfile | None = None
    global_rules: list[str] = Field(default_factory=list)
    glossary: list[GlossaryEntry] = Field(default_factory=list)
    questions: list[ContextQuestion] = Field(default_factory=list)
    notes: str = ""

    @field_validator("series_id")
    @classmethod
    def _validate_series_id(cls, value: str) -> str:
        if not value or not _SERIES_ID_RE.fullmatch(value):
            raise ValueError("series_id must match ^[A-Za-z0-9][A-Za-z0-9._-]*$")
        return value

    @field_validator("source_language", "target_language")
    @classmethod
    def _validate_nonempty_language(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("source_language and target_language must be non-empty")
        return value.strip()

    @field_validator("global_rules")
    @classmethod
    def _validate_global_rules(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in value:
            if raw is None:
                raise ValueError("global rule must not be null")
            collapsed = collapse_whitespace(raw)
            if not collapsed:
                raise ValueError("global rule must not be empty after normalization")
            normalized.append(collapsed)
        return normalized

    @model_validator(mode="after")
    def _validate_semantic_fields(self) -> SeriesContextPack:
        # Normalize glossary entries first so semantic checks see trimmed values.
        self.glossary = [normalize_glossary_entry(entry) for entry in self.glossary]
        validate_pack_glossary(self.glossary)
        validate_pack_questions(self.questions, self.style)
        return self


# --- pack glossary and question semantic validation --------------------------


def validate_pack_glossary(glossary: list[GlossaryEntry]) -> None:
    """Run the same semantic checks as glossary CLI mutation on a pack list.

    Pydantic shape validation alone is insufficient. Rejects:

    - empty source terms
    - empty variants or forbidden targets (after trimming)
    - duplicates within a variant or forbidden list (after trimming)
    - an approved target also present in ``forbidden_targets``
    - ``require_target=True`` without an approved target or target variant
    - a mandatory rule (``require_target`` or non-empty ``forbidden_targets``)
      with ``enforce="off"``
    - multiple entries with the same normalized source identity
    """
    seen_identities: dict[str, str] = {}
    for index, entry in enumerate(glossary):
        where = f"glossary[{index}] source={entry.source!r}"
        source = entry.source.strip()
        if not source:
            raise ContextPackError(
                "pack_glossary_empty_source", f"{where}: source term must not be empty"
            )
        for field_name, values in (
            ("source_variants", entry.source_variants),
            ("target_variants", entry.target_variants),
            ("forbidden_targets", entry.forbidden_targets),
            ("examples", entry.examples),
        ):
            stripped = [v.strip() for v in values]
            if any(not v for v in stripped):
                raise ContextPackError(
                    "pack_glossary_empty_member",
                    f"{where}: {field_name} contains an empty value",
                )
            if len(stripped) != len({v for v in stripped}):
                raise ContextPackError(
                    "pack_glossary_duplicate_member",
                    f"{where}: {field_name} contains duplicates after trimming",
                )
        approved_targets = [entry.target] if entry.target else []
        approved_targets = _dedupe_trimmed(approved_targets + entry.target_variants)
        for forbidden in entry.forbidden_targets:
            if any(
                _term_equal(forbidden, approved, case_sensitive=entry.case_sensitive)
                for approved in approved_targets
            ):
                raise ContextPackError(
                    "pack_glossary_approved_forbidden",
                    f"{where}: approved target {forbidden!r} is also forbidden",
                )
        if entry.require_target and not approved_targets:
            raise ContextPackError(
                "pack_glossary_require_without_target",
                f"{where}: require_target=True needs an approved target or variant",
            )
        if entry.enforce == "off" and (entry.require_target or entry.forbidden_targets):
            raise ContextPackError(
                "pack_glossary_mandatory_disabled",
                f"{where}: a mandatory rule cannot use enforce=off",
            )
        identity = source.casefold()
        if identity in seen_identities:
            raise ContextPackError(
                "pack_glossary_duplicate_identity",
                f"{where}: duplicate glossary identity {seen_identities[identity]!r}",
            )
        seen_identities[identity] = source


def validate_pack_questions(
    questions: list[ContextQuestion], style: StyleProfile | None
) -> None:
    """Validate question semantic identity and core-question/style consistency.

    Rejects duplicate question semantic identities and any core-question answer
    that disagrees with the corresponding pack style field value.
    """
    seen: dict[tuple[str, str], str] = {}
    for index, question in enumerate(questions):
        ident = question_identity(question)
        if not ident[0] and not ident[1]:
            raise ContextPackError(
                "pack_question_empty_identity",
                f"questions[{index}]: topic and question must not both be empty",
            )
        if ident in seen:
            raise ContextPackError(
                "pack_question_duplicate_identity",
                f"questions[{index}]: duplicate question semantic identity "
                f"{seen[ident]!r}",
            )
        seen[ident] = question.id
    if style is not None:
        for question in questions:
            if (
                question.origin == "core"
                and question.id in CORE_QUESTION_STYLE_FIELDS
                and question.status == "answered"
                and (question.answer or "").strip()
            ):
                field_name = CORE_QUESTION_STYLE_FIELDS[question.id]
                style_value = ""
                if hasattr(style, field_name):
                    style_value = str(getattr(style, field_name) or "")
                answer = (question.answer or "").strip()
                if style_value and answer != style_value.strip():
                    raise ContextPackError(
                        "pack_core_style_conflict",
                        f"core question {question.id} answer {question.answer!r} "
                        f"disagrees with style.{field_name}={style_value!r}",
                    )


# --- pack IO -----------------------------------------------------------------


def parse_context_pack(data: str | bytes) -> SeriesContextPack:
    """Parse and strictly validate a series context pack JSON document.

    Raises :class:`ContextPackError` on any shape, parse, or semantic error.
    """
    try:
        pack = SeriesContextPack.model_validate_json(data)
    except ContextPackError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface as pack error with code
        raise ContextPackError(
            "pack_parse_error", f"invalid series context pack: {exc}"
        ) from exc
    return pack


def read_context_pack(path: Path) -> SeriesContextPack:
    """Read and validate a series context pack from ``path``."""
    try:
        text = path.read_text("utf-8")
    except OSError as exc:
        raise ContextPackError(
            "pack_read_error", f"could not read context pack {path}: {exc}"
        ) from exc
    return parse_context_pack(text)


def write_context_pack(path: Path, pack: SeriesContextPack) -> None:
    """Write ``pack`` atomically to ``path`` using a sibling temporary file.

    The parent directory is created on demand. A reader sees either the previous
    file or the complete new file, never a partial write.
    """
    from booktx.io_utils import write_json_text_atomic

    payload = pack.model_dump_json(indent=2, by_alias=True)
    write_json_text_atomic(path, payload)


# --- export ------------------------------------------------------------------


def export_context_pack(
    project: Project,
    *,
    series_id: str,
    title: str = "",
    source_language: str | None = None,
    target_language: str | None = None,
    target_locale: str | None = None,
    include_style: bool = True,
    include_global_rules: bool = True,
    include_glossary: bool = True,
    include_questions: PackQuestionInclusion = "approved",
    allow_not_ready: bool = False,
) -> SeriesContextPack:
    """Build a :class:`SeriesContextPack` from the selected profile context.

    Loads and validates ``context.json`` for the active profile, refuses unsafe
    ``context.md`` drift, requires ``ready`` and not ``ready_forced`` unless
    ``allow_not_ready`` is set, and exports only reusable fields. Exports all
    glossary entries (including open entries) and only reusable answered
    questions whose ``answer_source`` is ``user``, ``imported``, or ``legacy``.

    ``source_language``/``target_language``/``target_locale`` default to the
    loaded context's values when not supplied.
    """
    context = load_context(project)
    if context is None:
        raise ContextPackError("context_missing", "translation context is missing")
    # Refuse unsafe Markdown drift using the existing context mutation guard.
    ensure_context_markdown_safe_to_overwrite(project, context)

    if not allow_not_ready:
        if not context.ready:
            raise ContextPackError(
                "context_not_ready",
                "context is not ready; pass --allow-not-ready to export a draft",
            )
        if context.ready_forced:
            raise ContextPackError(
                "context_ready_forced",
                "context readiness was forced; pass --allow-not-ready to export",
            )

    src_lang = (source_language or context.source_language).strip()
    tgt_lang = (target_language or context.target_language).strip()
    if not src_lang or not tgt_lang:
        raise ContextPackError(
            "pack_language_missing", "source and target languages must be non-empty"
        )
    locale = target_locale
    if locale is None:
        locale = context.style.target_locale

    style: StyleProfile | None = None
    if include_style:
        style = context.style.model_copy(deep=True)

    global_rules: list[str] = []
    if include_global_rules:
        global_rules = [collapse_whitespace(rule) for rule in context.global_rules]
        global_rules = [rule for rule in global_rules if rule]

    glossary: list[GlossaryEntry] = []
    if include_glossary:
        glossary = [entry.model_copy(deep=True) for entry in context.glossary]

    questions: list[ContextQuestion] = []
    if include_questions == "approved":
        for question in context.questions:
            if (
                question.status == "answered"
                and (question.answer or "").strip()
                and question.answer_source in {"user", "imported", "legacy"}
            ):
                questions.append(question.model_copy(deep=True))
    elif include_questions == "none":
        questions = []
    else:  # pragma: no cover - exhaustive literal
        raise ContextPackError(
            "pack_question_inclusion_invalid",
            f"unknown question inclusion: {include_questions}",
        )

    from booktx.io_utils import utc_timestamp

    pack = SeriesContextPack(
        series_id=series_id,
        title=title,
        source_language=src_lang,
        target_language=tgt_lang,
        target_locale=locale,
        created_at=utc_timestamp(),
        source=ContextPackSource(
            project_profile=project.profile or "",
            context_sha256=_context_sha256(context),
            baseline_sha256=baseline_sha256(context),
        ),
        style=style,
        global_rules=global_rules,
        glossary=glossary,
        questions=questions,
    )
    return pack


def _context_sha256(context: TranslationContext) -> str:
    """Deterministic hash of the full canonical context payload (provenance)."""
    from booktx.versioning import canonical_json_sha256

    return canonical_json_sha256(context.model_dump(mode="json", by_alias=True))


# --- import planning ---------------------------------------------------------


class ContextPackImportFinding(BaseModel):
    """One planned import outcome for a single section/key."""

    model_config = ConfigDict(extra="forbid")

    section: str
    key: str
    action: Literal["add", "update", "skip", "conflict", "error", "warning"]
    message: str


class ContextPackImportResult(BaseModel):
    """Aggregated import planning result with deterministic finding order."""

    model_config = ConfigDict(extra="forbid")

    changed: bool
    findings: list[ContextPackImportFinding] = Field(default_factory=list)
    added: int = 0
    updated: int = 0
    skipped: int = 0
    conflicts: int = 0
    errors: int = 0
    warnings: int = 0

    @classmethod
    def from_findings(
        cls, findings: list[ContextPackImportFinding], *, changed: bool
    ) -> ContextPackImportResult:
        counts = {
            "add": 0,
            "update": 0,
            "skip": 0,
            "conflict": 0,
            "error": 0,
            "warning": 0,
        }
        for finding in findings:
            counts[finding.action] += 1
        return cls(
            changed=changed,
            findings=findings,
            added=counts["add"],
            updated=counts["update"],
            skipped=counts["skip"],
            conflicts=counts["conflict"],
            errors=counts["error"],
            warnings=counts["warning"],
        )


def plan_context_pack_import(
    project: Project,
    pack: SeriesContextPack,
    *,
    conflict: PackConflictMode = "fail",
    init_missing_context: bool = False,
) -> tuple[TranslationContext, ContextPackImportResult]:
    """Plan a context-pack import without mutating inputs or writing files.

    Returns a deep-copied planned target context and a deterministic findings
    result. Raises :class:`ContextPackError` for hard preflight failures (unsafe
    drift, language mismatch, missing context). Unresolved conflicts are
    reported as findings; with ``conflict="fail"`` (the default) the caller
    treats ``conflicts > 0`` as a non-zero-exit condition.
    """
    target = load_context(project)
    if target is None:
        if not init_missing_context:
            raise ContextPackError(
                "context_missing",
                "translation context is missing; pass --init-missing-context",
            )
        target = default_context(project, source_sha256="")

    # Preflight: refuse unsafe Markdown drift against the current context.
    ensure_context_markdown_safe_to_overwrite(project, target)

    if pack.source_language != target.source_language:
        raise ContextPackError(
            "source_language_mismatch",
            f"pack source language {pack.source_language!r} != "
            f"context source language {target.source_language!r}",
        )
    if pack.target_language != target.target_language:
        raise ContextPackError(
            "target_language_mismatch",
            f"pack target language {pack.target_language!r} != "
            f"context target language {target.target_language!r}",
        )

    findings: list[ContextPackImportFinding] = []
    merged = target.model_copy(deep=True)

    if pack.style is not None:
        _merge_style(
            merged, target, pack, project, conflict=conflict, findings=findings
        )
    if pack.global_rules:
        _merge_global_rules(merged, pack, findings=findings)
    if pack.glossary:
        _merge_glossary(merged, pack, conflict=conflict, findings=findings)
    if pack.questions:
        _merge_questions(merged, pack, conflict=conflict, findings=findings)

    # Readiness invalidation: clear if the effective policy changed, preserve if no-op.
    changed = _effective_context_changed(target, merged)
    if changed:
        _clear_readiness(merged)
        findings.append(
            ContextPackImportFinding(
                section="readiness",
                key="readiness",
                action="update",
                message=(
                    "readiness cleared; run `booktx context mark-ready` after approval"
                ),
            )
        )
        # Workflow warnings are advisory and only relevant when policy changed.
        _record_workflow_warnings(project, pack, findings=findings)

    # Deterministic finding order:
    # 1. compatibility errors, 2. style, 3. global rules, 4. glossary,
    # 5. questions, 6. readiness and workflow warnings.
    ordered = _order_findings(findings)
    result = ContextPackImportResult.from_findings(ordered, changed=changed)
    return merged, result


def import_context_pack(
    project: Project,
    pack: SeriesContextPack,
    *,
    conflict: PackConflictMode = "fail",
    init_missing_context: bool = False,
) -> tuple[TranslationContext, ContextPackImportResult]:
    """Plan an import and commit it atomically (context.json then context.md).

    Performs an optimistic canonical-hash recheck immediately before writing:
    if another process changed the canonical context after preflight, nothing
    is written. Raises :class:`ContextPackError` if the planned result has any
    errors or unresolved conflicts. Returns the persisted context and result.

    The two file replacements are individually atomic, not a filesystem-wide
    transaction. A crash between them can leave ``context.md`` stale, but cannot
    leave partial JSON or lose canonical context; existing context validation
    and ``context render --write`` provide deterministic recovery.
    """
    from booktx.context import write_context, write_context_markdown
    from booktx.versioning import canonical_json_sha256

    # Capture the preflight canonical hash from the live target context before
    # planning. plan_context_pack_import performs no writes, so the live context
    # immediately after planning still equals the preflight state.
    preflight_ctx = load_context(project)
    preflight_existed = preflight_ctx is not None
    if preflight_ctx is None:
        preflight_ctx = default_context(project, source_sha256="")
    preflight_hash = canonical_json_sha256(baseline_payload(preflight_ctx))

    planned, result = plan_context_pack_import(
        project,
        pack,
        conflict=conflict,
        init_missing_context=init_missing_context,
    )
    if result.errors or result.conflicts:
        raise ContextPackError(
            "import_unresolved",
            "import has unresolved conflicts or errors; nothing written",
        )

    # Optimistic write check: refuse the write if another process changed the
    # canonical context (or deleted it) after preflight.
    live = load_context(project)
    if preflight_existed and live is None:
        raise ContextPackError(
            "optimistic_write_failed",
            "canonical context disappeared during import; nothing written",
        )
    if (
        live is not None
        and canonical_json_sha256(baseline_payload(live)) != preflight_hash
    ):
        raise ContextPackError(
            "optimistic_write_failed",
            "canonical context changed during import; nothing written",
        )

    write_context(project, planned)
    try:
        write_context_markdown(project, planned)
    except Exception:
        # context.json is canonical and was written atomically. A failure here
        # leaves context.md stale but recoverable via `context render --write`.
        pass
    return planned, result


# --- merge helpers -----------------------------------------------------------


_STYLE_FIELDS = (
    "target_locale",
    "formality",
    "register_level",
    "prose_style",
    "dialogue_style",
    "sentence_policy",
    "punctuation_policy",
    "units_policy",
)


def _is_pristine_default(
    target: TranslationContext,
    project: Project,
    field_name: str,
    *,
    governing_question_answered: bool,
) -> bool:
    """Return True if the local style field is a replaceable profile default.

    A local value is pristine and replaceable without conflict only when all of:
    the target context is not ready, the local value equals the value produced
    by ``default_context(project)`` for that field, and no answered local core
    question governs that field.
    """
    if target.ready:
        return False
    if governing_question_answered:
        return False
    defaults = default_context(project)
    local_value = getattr(target.style, field_name)
    default_value = getattr(defaults.style, field_name)
    return bool(local_value == default_value)


def _local_core_question_answers_field(
    target: TranslationContext, field_name: str
) -> bool:
    """Return True if an answered local core question governs ``field_name``."""
    id_for_field = {
        style_field: qid for qid, style_field in CORE_QUESTION_STYLE_FIELDS.items()
    }
    qid = id_for_field.get(field_name)
    if qid is None:
        return False
    for question in target.questions:
        if (
            question.id == qid
            and question.origin == "core"
            and question.status == "answered"
            and (question.answer or "").strip()
        ):
            return True
    return False


def _merge_style(
    merged: TranslationContext,
    target: TranslationContext,
    pack: SeriesContextPack,
    project: Project,
    *,
    conflict: PackConflictMode,
    findings: list[ContextPackImportFinding],
) -> None:
    assert pack.style is not None
    for field_name in _STYLE_FIELDS:
        imported_value = getattr(pack.style, field_name)
        local_value = getattr(merged.style, field_name)
        # formality is a Literal; treat its default as a normal value.
        if imported_value == local_value:
            continue
        if not _style_value_meaningful(field_name, imported_value):
            # Imported empty: never overwrite a local value with emptiness.
            continue
        if not _style_value_meaningful(field_name, local_value) or _is_pristine_default(
            target,
            project,
            field_name,
            governing_question_answered=_local_core_question_answers_field(
                target, field_name
            ),
        ):
            setattr(merged.style, field_name, imported_value)
            findings.append(
                ContextPackImportFinding(
                    section="style",
                    key=field_name,
                    action="update",
                    message=_style_field_message(
                        field_name, local_value, imported_value
                    ),
                )
            )
            continue
        # Different non-empty values.
        if conflict == "replace":
            setattr(merged.style, field_name, imported_value)
            findings.append(
                ContextPackImportFinding(
                    section="style",
                    key=field_name,
                    action="update",
                    message=_style_field_message(
                        field_name, local_value, imported_value
                    ),
                )
            )
        elif conflict == "keep-local":
            findings.append(
                ContextPackImportFinding(
                    section="style",
                    key=field_name,
                    action="skip",
                    message=_style_field_message(
                        field_name, local_value, imported_value
                    ),
                )
            )
        else:  # fail
            findings.append(
                ContextPackImportFinding(
                    section="style",
                    key=field_name,
                    action="conflict",
                    message=_style_field_message(
                        field_name, local_value, imported_value
                    )
                    + "; resolve with --conflict keep-local or --conflict replace",
                )
            )


def _style_value_meaningful(field_name: str, value: object) -> bool:
    """Return True when ``value`` is a non-default, non-empty style value.

    For string fields, meaningful means non-empty. For ``formality`` (a Literal
    with a real default of ``neutral``), the default is meaningful and is
    compared directly by the caller.
    """
    if field_name == "formality":
        return True
    return bool(str(value or "").strip())


def _style_field_message(field_name: str, local: object, imported: object) -> str:
    return f"{field_name}: local={local!r} pack={imported!r}"


def _merge_global_rules(
    merged: TranslationContext,
    pack: SeriesContextPack,
    *,
    findings: list[ContextPackImportFinding],
) -> None:
    """Append imported global rules with no normalized local equivalent.

    Global rules never conflict in version 1. Comparison is case-sensitive after
    whitespace normalization; the original local text and order are preserved.
    """
    local_normalized = {collapse_whitespace(rule) for rule in merged.global_rules}
    for raw_rule in pack.global_rules:
        normalized = collapse_whitespace(raw_rule)
        if not normalized:
            continue
        if normalized in local_normalized:
            findings.append(
                ContextPackImportFinding(
                    section="global_rules",
                    key=normalized,
                    action="skip",
                    message=f"global rule already present: {normalized}",
                )
            )
            continue
        merged.global_rules.append(raw_rule.strip())
        local_normalized.add(normalized)
        findings.append(
            ContextPackImportFinding(
                section="global_rules",
                key=normalized,
                action="add",
                message=f"global rule: {normalized}",
            )
        )


def _merge_glossary(
    merged: TranslationContext,
    pack: SeriesContextPack,
    *,
    conflict: PackConflictMode,
    findings: list[ContextPackImportFinding],
) -> None:
    """Merge glossary entries by normalized source identity only.

    Missing identity: add. Full model equality after normalization: skip. Any
    unequal field: conflict, resolved by the conflict mode. Field-wise additive
    glossary merging is not implemented in version 1.
    """
    local_by_identity: dict[str, GlossaryEntry] = {}
    for entry in merged.glossary:
        local_by_identity[glossary_identity(entry)] = entry
    for imported in pack.glossary:
        imported = normalize_glossary_entry(imported)
        identity = glossary_identity(imported)
        existing = local_by_identity.get(identity)
        if existing is None:
            merged.glossary.append(imported)
            local_by_identity[identity] = imported
            findings.append(
                ContextPackImportFinding(
                    section="glossary",
                    key=imported.source,
                    action="add",
                    message=_glossary_entry_message("add", imported),
                )
            )
            continue
        existing_normalized = normalize_glossary_entry(existing)
        if existing_normalized.model_dump(mode="json") == imported.model_dump(
            mode="json"
        ):
            findings.append(
                ContextPackImportFinding(
                    section="glossary",
                    key=imported.source,
                    action="skip",
                    message=_glossary_entry_message("skip", imported),
                )
            )
            continue
        if conflict == "replace":
            index = merged.glossary.index(existing)
            merged.glossary[index] = imported
            local_by_identity[identity] = imported
            findings.append(
                ContextPackImportFinding(
                    section="glossary",
                    key=imported.source,
                    action="update",
                    message=_glossary_entry_message("replace", imported, existing),
                )
            )
        elif conflict == "keep-local":
            findings.append(
                ContextPackImportFinding(
                    section="glossary",
                    key=imported.source,
                    action="skip",
                    message=_glossary_entry_message("keep-local", imported, existing),
                )
            )
        else:  # fail
            findings.append(
                ContextPackImportFinding(
                    section="glossary",
                    key=imported.source,
                    action="conflict",
                    message=_glossary_entry_message("conflict", imported, existing)
                    + "; resolve with --conflict keep-local or --conflict replace",
                )
            )


def _glossary_entry_message(
    action: str, imported: GlossaryEntry, local: GlossaryEntry | None = None
) -> str:
    parts = [f"{action} glossary: {imported.source}"]
    if imported.target:
        parts.append(f"target={imported.target}")
    if imported.forbidden_targets:
        parts.append(f"forbidden={','.join(imported.forbidden_targets)}")
    parts.append(f"enforce={imported.enforce}")
    if local is not None and action in {"conflict", "replace", "keep-local"}:
        local_target = local.target or "<open>"
        parts.append(f"(local target={local_target})")
    return "; ".join(parts)


def _merge_questions(
    merged: TranslationContext,
    pack: SeriesContextPack,
    *,
    conflict: PackConflictMode,
    findings: list[ContextPackImportFinding],
) -> None:
    """Merge questions by semantic identity.

    Question ids are not portable identities. Match exact semantic identity
    first; for core questions, accept an id match only when both entries are
    ``origin="core"`` and semantic identity also matches. New imported questions
    receive the next available ``SNNN`` id. Linked core-question/style decisions
    are resolved together via :func:`apply_answer_to_context` after conflict
    resolution.
    """
    from booktx.io_utils import utc_timestamp

    local_by_identity: dict[tuple[str, str], ContextQuestion] = {}
    for question in merged.questions:
        local_by_identity[question_identity(question)] = question

    for imported in pack.questions:
        ident = question_identity(imported)
        local = local_by_identity.get(ident)
        if local is None:
            # Core-question id match only counts when both are core AND
            # identity matches.
            local = _find_core_id_match(merged.questions, imported)
        if local is None:
            new_id = next_question_id(merged, prefix="S")
            new_question = imported.model_copy(
                update={
                    "id": new_id,
                    "answer_source": "imported",
                    "approved_by": f"context-pack:{pack.series_id}",
                    "approved_at": utc_timestamp(),
                    "status": "answered"
                    if (imported.answer or "").strip()
                    else imported.status,
                }
            )
            merged.questions.append(new_question)
            local_by_identity[question_identity(new_question)] = new_question
            findings.append(
                ContextPackImportFinding(
                    section="questions",
                    key=new_id,
                    action="add",
                    message=f"add question {new_id}: {imported.question}",
                )
            )
            continue

        local_answer = (local.answer or "").strip()
        imported_answer = (imported.answer or "").strip()
        if imported_answer and local_answer == imported_answer:
            findings.append(
                ContextPackImportFinding(
                    section="questions",
                    key=local.id,
                    action="skip",
                    message=f"question {local.id}: equal answer",
                )
            )
            continue
        if not imported_answer:
            findings.append(
                ContextPackImportFinding(
                    section="questions",
                    key=local.id,
                    action="skip",
                    message=f"question {local.id}: pack has no answer",
                )
            )
            continue
        if not local_answer:
            # Empty local answer, non-empty imported answer: update.
            _apply_imported_answer(local, imported, pack)
            if local.origin == "core" and local.id in CORE_QUESTION_STYLE_FIELDS:
                apply_answer_to_context(merged, local.id, imported_answer)
            findings.append(
                ContextPackImportFinding(
                    section="questions",
                    key=local.id,
                    action="update",
                    message=f"question {local.id}: answer imported",
                )
            )
            continue
        # Different non-empty answers: conflict.
        if conflict == "replace":
            _apply_imported_answer(local, imported, pack)
            if local.origin == "core" and local.id in CORE_QUESTION_STYLE_FIELDS:
                apply_answer_to_context(merged, local.id, imported_answer)
            findings.append(
                ContextPackImportFinding(
                    section="questions",
                    key=local.id,
                    action="update",
                    message=f"question {local.id}: answer replaced",
                )
            )
        elif conflict == "keep-local":
            findings.append(
                ContextPackImportFinding(
                    section="questions",
                    key=local.id,
                    action="skip",
                    message=f"question {local.id}: kept local answer",
                )
            )
        else:  # fail
            findings.append(
                ContextPackImportFinding(
                    section="questions",
                    key=local.id,
                    action="conflict",
                    message=f"question {local.id}: local={local_answer!r} "
                    f"pack={imported_answer!r}; resolve with "
                    "--conflict keep-local or --conflict replace",
                )
            )


def _find_core_id_match(
    locals_: list[ContextQuestion], imported: ContextQuestion
) -> ContextQuestion | None:
    """Return a local core question sharing id+origin, ignoring semantic drift.

    Used only after semantic identity fails to match. Core questions accept an
    id match only when both entries have ``origin="core"`` and their semantic
    identity also matches; this helper therefore only returns a match for the
    narrow case where identities coincide.
    """
    if imported.origin != "core":
        return None
    for local in locals_:
        if (
            local.origin == "core"
            and local.id == imported.id
            and question_identity(local) == question_identity(imported)
        ):
            return local
    return None


def _apply_imported_answer(
    local: ContextQuestion, imported: ContextQuestion, pack: SeriesContextPack
) -> None:
    """Rewrite the local question's answer and provenance for an accepted import."""
    from booktx.io_utils import utc_timestamp

    local.answer = imported.answer
    local.status = "answered"
    local.answer_source = "imported"
    local.approved_by = f"context-pack:{pack.series_id}"
    local.approved_at = utc_timestamp()


def _record_workflow_warnings(
    project: Project,
    pack: SeriesContextPack,
    *,
    findings: list[ContextPackImportFinding],
) -> None:
    """Record an advisory warning if unfinished tasks/todos exist.

    Uses existing status/index logic where possible; historical completed task
    files must not cause a permanent warning. Never modifies work artifacts.
    """
    if has_unfinished_tasks(project):
        findings.append(
            ContextPackImportFinding(
                section="workflow",
                key="tasks",
                action="warning",
                message=(
                    "existing tasks may carry the previous context view; "
                    "create fresh tasks to use the imported policy"
                ),
            )
        )


def has_unfinished_tasks(project: Project) -> bool:
    """Return True if in-flight translation work exists in the selected profile.

    A profile has unfinished work when it has durable task or todo files AND
    its status snapshot shows outstanding untranslated source records. This
    uses the existing status/index logic so that historical completed task
    files in a fully-translated project do not cause a permanent warning.
    Returns False if the status snapshot cannot be computed.
    """
    from booktx.config import translation_task_dir, translation_todo_dir

    task_dir = translation_task_dir(project)
    todo_dir = translation_todo_dir(project)
    has_work_files = _dir_has_json(task_dir) or _dir_has_json(todo_dir)
    if not has_work_files:
        return False
    try:
        from booktx.status import build_status_snapshot

        ctx = load_context(project)
        bundle = build_status_snapshot(
            project,
            context_exists=ctx is not None,
            context_ready=bool(ctx.ready) if ctx is not None else False,
        )
        totals = bundle.snapshot.totals
    except Exception:  # noqa: BLE001 - advisory warning; never block import
        return False
    return totals.records_translated < totals.records_total


def _dir_has_json(path: Path) -> bool:
    """Return True if ``path`` is a directory containing at least one .json file."""
    return path.is_dir() and any(path.glob("*.json"))


def _clear_readiness(context: TranslationContext) -> None:
    context.ready = False
    context.ready_forced = False
    context.ready_reason = ""
    context.ready_by = ""
    context.ready_at = ""


def _effective_context_changed(
    original: TranslationContext, merged: TranslationContext
) -> bool:
    """Return True if the effective policy differs between the two contexts.

    Compares the canonical baseline payload (style, global rules, glossary,
    questions, languages). Readiness and chapter contexts are excluded because
    import never touches chapter contexts and readiness is recomputed below.
    """
    from booktx.versioning import canonical_json_sha256

    return canonical_json_sha256(baseline_payload(original)) != canonical_json_sha256(
        baseline_payload(merged)
    )


# --- deterministic finding ordering ------------------------------------------


_FINDING_ORDER = {
    "error": 0,
    "style": 1,
    "global_rules": 2,
    "glossary": 3,
    "questions": 4,
    "readiness": 5,
    "workflow": 6,
}


def _order_findings(
    findings: list[ContextPackImportFinding],
) -> list[ContextPackImportFinding]:
    return sorted(findings, key=lambda f: (_FINDING_ORDER.get(f.section, 99), f.key))
