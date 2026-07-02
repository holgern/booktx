"""Durable source-analysis decisions and profile-local context workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from booktx.config import (
    Project,
    list_profiles,
    load_profile_project,
    source_analysis_decisions_path,
)
from booktx.context import (
    ContextQuestion,
    GlossaryEntry,
    TranslationContext,
    load_context,
    next_question_id,
    write_context,
    write_context_markdown,
)
from booktx.errors import _err
from booktx.io_utils import utc_timestamp, write_json_text_atomic
from booktx.source_analysis import SourceAnalysisReport, SourceCandidate

DECISIONS_SCHEMA: Literal["booktx.source-analysis-decisions.v1"] = (
    "booktx.source-analysis-decisions.v1"
)


class CandidateDisposition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    normalized: str
    disposition: Literal["ignored", "reviewed"]
    reason: str = ""
    decided_by: str
    decided_at: str


class CandidatePromotionRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    profile: str
    context_kind: Literal["glossary", "question"]
    context_ref: str
    promoted_by: str
    promoted_at: str


class SourceAnalysisDecisions(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_name: Literal["booktx.source-analysis-decisions.v1"] = Field(
        default=DECISIONS_SCHEMA, alias="schema"
    )
    dispositions: list[CandidateDisposition] = Field(default_factory=list)
    promotions: list[CandidatePromotionRef] = Field(default_factory=list)


def load_decisions(project: Project) -> SourceAnalysisDecisions:
    path = source_analysis_decisions_path(project)
    if not path.is_file():
        return SourceAnalysisDecisions()
    try:
        return SourceAnalysisDecisions.model_validate_json(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise _err(
            "source_analysis_decisions_invalid",
            f"invalid source-analysis decisions sidecar: {exc}",
        ) from exc


def write_decisions(project: Project, decisions: SourceAnalysisDecisions) -> None:
    write_json_text_atomic(
        source_analysis_decisions_path(project),
        decisions.model_dump_json(by_alias=True, indent=2),
    )


def find_candidate(report: SourceAnalysisReport, candidate_id: str) -> SourceCandidate:
    candidate = next((c for c in report.candidates if c.id == candidate_id), None)
    if candidate is None:
        raise _err(
            "source_analysis_candidate_missing",
            f"unknown source-analysis candidate: {candidate_id}",
        )
    return candidate


def set_disposition(
    project: Project,
    report: SourceAnalysisReport,
    *,
    candidate_id: str,
    disposition: Literal["ignored", "reviewed"],
    reason: str,
    decided_by: str,
    write: bool,
) -> tuple[CandidateDisposition, bool]:
    candidate = find_candidate(report, candidate_id)
    decisions = load_decisions(project)
    current = next(
        (d for d in decisions.dispositions if d.candidate_id == candidate.id), None
    )
    unchanged = (
        current is not None
        and current.disposition == disposition
        and current.reason == reason
        and current.decided_by == decided_by
    )
    result = current or CandidateDisposition(
        candidate_id=candidate.id,
        normalized=candidate.normalized,
        disposition=disposition,
        reason=reason,
        decided_by=decided_by,
        decided_at=utc_timestamp(),
    )
    if not unchanged:
        result = CandidateDisposition(
            candidate_id=candidate.id,
            normalized=candidate.normalized,
            disposition=disposition,
            reason=reason,
            decided_by=decided_by,
            decided_at=utc_timestamp(),
        )
        decisions.dispositions = [
            d for d in decisions.dispositions if d.candidate_id != candidate.id
        ]
        decisions.dispositions.append(result)
        decisions.dispositions.sort(key=lambda d: d.candidate_id)
        if write:
            write_decisions(project, decisions)
    return result, not unchanged


def clear_context_readiness(context: TranslationContext) -> None:
    context.ready = False
    context.ready_forced = False
    context.ready_reason = ""
    context.ready_by = ""
    context.ready_at = ""


@dataclass
class ProfilePrefillResult:
    profile: str
    added: int = 0
    updated: int = 0
    skipped: int = 0
    conflicts: int = 0
    changed: bool = False
    written: bool = False
    error: str | None = None


@dataclass
class PrefillResult:
    write: bool
    profiles: list[ProfilePrefillResult] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(p.error for p in self.profiles)


def _normalized_entry_terms(entry: GlossaryEntry) -> set[str]:
    return {entry.source.casefold(), *(v.casefold() for v in entry.source_variants)}


def _prefill_glossary_candidate(
    context: TranslationContext,
    candidate: SourceCandidate,
    result: ProfilePrefillResult,
) -> None:
    exact = next(
        (
            entry
            for entry in context.glossary
            if entry.source_analysis_candidate_id == candidate.id
        ),
        None,
    )
    fallback = next(
        (
            entry
            for entry in context.glossary
            if candidate.normalized.casefold() in _normalized_entry_terms(entry)
        ),
        None,
    )
    existing = exact or fallback
    if existing is not None:
        if existing.origin == "source_analysis" and existing.status == "open":
            if existing.source_analysis_candidate_id is None:
                existing.source_analysis_candidate_id = candidate.id
                result.updated += 1
                result.changed = True
            else:
                result.skipped += 1
        else:
            result.conflicts += 1
        return
    context.glossary.append(
        GlossaryEntry(
            source=candidate.text,
            source_variants=[
                value
                for value in (
                    [*candidate.source_variants, *candidate.surface_forms]
                    if candidate.source_variants
                    else candidate.surface_forms
                )
                if value != candidate.text
            ],
            category=candidate.category_hint or candidate.kind,
            status="open",
            notes=candidate.reason,
            enforce="warn",
            origin="source_analysis",
            source_analysis_candidate_id=candidate.id,
        )
    )
    result.added += 1
    result.changed = True


def _ensure_source_analysis_question(
    context: TranslationContext,
    report: SourceAnalysisReport,
    result: ProfilePrefillResult,
    *,
    topic: str,
    candidate_ids: list[str],
    question: str,
    recommendation: str,
    recommendation_reason: str,
) -> None:
    if not candidate_ids:
        return
    existing_question = next(
        (
            q
            for q in context.questions
            if q.origin == "source_analysis"
            and q.topic == topic
            and set(q.source_analysis_candidate_ids) == set(candidate_ids)
        ),
        None,
    )
    if existing_question is not None:
        result.skipped += 1
        return
    names = [
        find_candidate(report, candidate_id).text for candidate_id in candidate_ids[:12]
    ]
    rendered_question = question + ": " + ", ".join(names)
    context.questions.append(
        ContextQuestion(
            id=next_question_id(context),
            topic=topic,
            question=rendered_question,
            required=False,
            status="recommended",
            origin="source_analysis",
            recommendation=recommendation,
            recommendation_reason=recommendation_reason,
            recommendation_source="booktx source analysis",
            source_analysis_candidate_ids=candidate_ids,
        )
    )
    result.added += 1
    result.changed = True


def _prefill_one(
    context: TranslationContext,
    report: SourceAnalysisReport,
    ignored: set[str],
    profile: str,
    *,
    include_advisory: bool,
) -> ProfilePrefillResult:
    result = ProfilePrefillResult(profile=profile)
    binding_ids: list[str] = []
    name_ids: list[str] = []
    rare_ids: list[str] = []
    for candidate in report.candidates:
        if candidate.id in ignored:
            result.skipped += 1
            continue
        if candidate.review_bucket == "no_action":
            result.skipped += 1
            continue
        if candidate.review_bucket == "binding_glossary":
            binding_ids.append(candidate.id)
            continue
        if candidate.review_bucket == "name_policy":
            name_ids.append(candidate.id)
            continue
        if candidate.review_bucket == "invented_or_rare":
            rare_ids.append(candidate.id)
            continue
        if (
            not include_advisory
            or candidate.suggested_context_action != "add_advisory_glossary"
        ):
            continue
        _prefill_glossary_candidate(context, candidate, result)
    _ensure_source_analysis_question(
        context,
        report,
        result,
        topic="source-analysis binding glossary",
        candidate_ids=binding_ids,
        question="Review binding glossary candidates",
        recommendation=(
            "Confirm which terms need a binding glossary decision, then "
            "promote each approved term with `booktx context promote-candidate`.",
        ),
        recommendation_reason=(
            "Source analysis found likely world-building or terminology "
            "candidates that should be reviewed before translation.",
        ),
    )
    _ensure_source_analysis_question(
        context,
        report,
        result,
        topic="source-analysis names",
        candidate_ids=name_ids,
        question="Review translation policy for recurring names and titles",
        recommendation=(
            "Decide which names remain unchanged, which need transliteration, "
            "and which should become glossary-backed policy.",
        ),
        recommendation_reason="Source analysis found recurring title/name candidates.",
    )
    _ensure_source_analysis_question(
        context,
        report,
        result,
        topic="source-analysis rare terms",
        candidate_ids=rare_ids,
        question="Review rare or invented-looking source terms",
        recommendation=(
            "Confirm whether these rare terms need a glossary decision, a "
            "name-policy note, or an explicit ignore/review decision.",
        ),
        recommendation_reason=(
            "Source analysis kept rare singleton or low-frequency candidates "
            "because they look translation-relevant.",
        ),
    )
    return result


def prefill_contexts(
    project: Project,
    report: SourceAnalysisReport,
    *,
    profiles: list[str],
    write: bool,
    include_advisory: bool = False,
) -> PrefillResult:
    decisions = load_decisions(project)
    ignored = {
        d.candidate_id for d in decisions.dispositions if d.disposition == "ignored"
    }
    planned: list[tuple[Project, TranslationContext, ProfilePrefillResult]] = []
    output = PrefillResult(write=write)
    # Complete validation/preflight before the first write.
    for profile in profiles:
        try:
            profile_project = load_profile_project(project.root, profile)
            context = load_context(profile_project)
            if context is None:
                raise _err(
                    "source_analysis_prefill_context_missing",
                    f"profile {profile!r} has no context; run context init first",
                )
            result = _prefill_one(
                context,
                report,
                ignored,
                profile,
                include_advisory=include_advisory,
            )
            planned.append((profile_project, context, result))
            output.profiles.append(result)
        except (OSError, ValueError) as exc:
            output.profiles.append(
                ProfilePrefillResult(profile=profile, error=str(exc))
            )
    if output.blocked or not write:
        return output
    for profile_project, context, result in planned:
        if not result.changed:
            continue
        clear_context_readiness(context)
        try:
            write_context(profile_project, context)
            write_context_markdown(profile_project, context)
            result.written = True
        except OSError as exc:
            result.error = str(exc)
    return output


def compatible_prefill_profiles(project: Project) -> list[str]:
    result: list[str] = []
    for profile in list_profiles(project):
        profile_project = load_profile_project(project.root, profile)
        cfg = profile_project.profile_config
        if cfg is not None and cfg.kind != "pass-through":
            result.append(profile)
    return result


def promote_candidate(
    project: Project,
    report: SourceAnalysisReport,
    *,
    profile: str,
    candidate_id: str,
    category: str | None,
    target: str | None,
    forbidden_targets: list[str],
    require_target: bool,
    enforce: Literal["off", "warn", "error"],
    as_question: bool,
    promoted_by: str,
    write: bool,
) -> tuple[str, bool]:
    candidate = find_candidate(report, candidate_id)
    if (target or forbidden_targets or require_target) and not write:
        # The dry-run is still useful, but binding inputs are never interpreted
        # as approved until the explicit write is performed.
        pass
    if enforce == "off" and (require_target or forbidden_targets):
        raise _err(
            "source_analysis_promotion_enforcement",
            "binding promotion cannot use --enforce off",
        )
    profile_project = load_profile_project(project.root, profile)
    context = load_context(profile_project)
    if context is None:
        raise _err(
            "source_analysis_promotion_context_missing",
            f"profile {profile!r} has no context; run context init first",
        )
    if as_question:
        existing = next(
            (
                q
                for q in context.questions
                if candidate.id in q.source_analysis_candidate_ids
            ),
            None,
        )
        if existing is None:
            existing = ContextQuestion(
                id=next_question_id(context),
                topic=f"source-analysis candidate {candidate.text}",
                question=f"How should {candidate.text!r} be translated?",
                required=False,
                status="recommended",
                origin="source_analysis",
                recommendation=candidate.reason,
                recommendation_reason=candidate.reason,
                recommendation_source="booktx source analysis",
                source_analysis_candidate_ids=[candidate.id],
            )
            context.questions.append(existing)
        context_kind: Literal["glossary", "question"] = "question"
        context_ref = existing.id
    else:
        existing_entry = next(
            (
                entry
                for entry in context.glossary
                if entry.source_analysis_candidate_id == candidate.id
            ),
            None,
        )
        if existing_entry is not None and existing_entry.origin != "source_analysis":
            raise _err(
                "source_analysis_promotion_conflict",
                "candidate conflicts with local glossary entry "
                f"{existing_entry.source!r}",
            )
        replacement = GlossaryEntry(
            source=candidate.text,
            source_variants=[
                value for value in candidate.surface_forms if value != candidate.text
            ],
            target=target,
            require_target=require_target,
            forbidden_targets=forbidden_targets,
            category=category or candidate.category_hint or candidate.kind,
            status="approved" if target else "open",
            notes=candidate.reason,
            enforce=enforce,
            origin="source_analysis",
            source_analysis_candidate_id=candidate.id,
        )
        context.glossary = [
            entry
            for entry in context.glossary
            if entry.source_analysis_candidate_id != candidate.id
        ]
        context.glossary.append(replacement)
        context_kind = "glossary"
        context_ref = replacement.source
    if not write:
        return context_ref, True
    clear_context_readiness(context)
    write_context(profile_project, context)
    write_context_markdown(profile_project, context)
    decisions = load_decisions(project)
    decisions.promotions = [
        ref
        for ref in decisions.promotions
        if not (
            ref.candidate_id == candidate.id
            and ref.profile == profile
            and ref.context_kind == context_kind
        )
    ]
    decisions.promotions.append(
        CandidatePromotionRef(
            candidate_id=candidate.id,
            profile=profile,
            context_kind=context_kind,
            context_ref=context_ref,
            promoted_by=promoted_by,
            promoted_at=utc_timestamp(),
        )
    )
    decisions.promotions.sort(
        key=lambda ref: (ref.candidate_id, ref.profile, ref.context_kind)
    )
    write_decisions(project, decisions)
    return context_ref, True
