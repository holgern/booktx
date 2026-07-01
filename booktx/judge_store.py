"""Selection-profile helpers for judge workflows."""

from __future__ import annotations

from booktx.config import Project, _err, load_profile_project, load_translation_store
from booktx.models import JudgeTaskCandidate, JudgeTaskFinding, Record, TranslatedRecord
from booktx.translation_store import (
    EffectiveCandidateError,
    effective_candidate_selection,
    sha256_text,
)
from booktx.validate import validate_record_pair

__all__ = [
    "parse_sources_csv",
    "resolve_selection_sources",
    "require_selection_profile",
    "validate_judge_source_profile",
    "load_source_profile_projects",
    "selected_record_ids",
    "record_has_candidate_gap",
    "collect_source_candidates",
]


def parse_sources_csv(raw: str | None) -> list[str]:
    if raw is None:
        return []
    seen: set[str] = set()
    values: list[str] = []
    for item in raw.split(","):
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        values.append(value)
    return values


def require_selection_profile(project: Project) -> None:
    cfg = project.profile_config
    if cfg is None or cfg.kind != "selection":
        raise _err(
            "judge_profile_kind",
            "judge workflows require a selection profile",
        )


def resolve_selection_sources(project: Project, sources_csv: str | None) -> list[str]:
    require_selection_profile(project)
    explicit = parse_sources_csv(sources_csv)
    if explicit:
        return explicit
    cfg = project.profile_config
    assert cfg is not None
    selection = cfg.selection
    if selection is None or not selection.sources:
        raise _err(
            "judge_sources_missing",
            "no source profiles configured; pass --sources or "
            "create the selection profile with sources",
        )
    return list(selection.sources)


def validate_judge_source_profile(
    selection_project: Project, source_project: Project
) -> None:
    selection_cfg = selection_project.profile_config
    source_cfg = source_project.profile_config
    assert selection_cfg is not None
    assert source_cfg is not None
    if source_project.profile == selection_project.profile:
        raise _err(
            "judge_source_profile_self",
            "selection profile cannot be a judge source",
        )
    if source_cfg.kind != "translation":
        raise _err(
            "judge_source_profile_kind",
            f"source profile {source_project.profile} must be a translation profile, "
            f"got {source_cfg.kind}",
        )
    if source_cfg.source_language != selection_cfg.source_language:
        raise _err(
            "judge_source_language_mismatch",
            f"source profile {source_project.profile} source language "
            f"{source_cfg.source_language!r} does not match selection profile "
            f"{selection_cfg.source_language!r}",
        )
    if source_cfg.target_language != selection_cfg.target_language:
        raise _err(
            "judge_target_language_mismatch",
            f"source profile {source_project.profile} target language "
            f"{source_cfg.target_language!r} does not match selection profile "
            f"{selection_cfg.target_language!r}",
        )


def load_source_profile_projects(
    selection_project: Project, source_profiles: list[str]
) -> dict[str, Project]:
    projects = {
        profile_name: load_profile_project(selection_project.root, profile_name)
        for profile_name in source_profiles
    }
    for project in projects.values():
        validate_judge_source_profile(selection_project, project)
    return projects


def selected_record_ids(project: Project) -> set[str]:
    ids: set[str] = set()
    for record_id, stored in load_translation_store(project).records.items():
        selection = effective_candidate_selection(stored, strict_active_review=True)
        if isinstance(selection, EffectiveCandidateError) or selection is None:
            continue
        ids.add(record_id)
    return ids


def record_has_candidate_gap(
    source_projects: dict[str, Project],
    record_id: str,
) -> bool:
    for project in source_projects.values():
        stored = load_translation_store(project).records.get(record_id)
        if stored is None:
            return True
        selection = effective_candidate_selection(stored, strict_active_review=True)
        if isinstance(selection, EffectiveCandidateError) or selection is None:
            return True
    return False


def collect_source_candidates(
    *,
    selection_project: Project,
    selection_context: object | None,
    source_projects: dict[str, Project],
    source_record: Record,
    chunk_id: str,
) -> tuple[list[JudgeTaskCandidate], list[str]]:
    candidates: list[JudgeTaskCandidate] = []
    missing_profiles: list[str] = []
    label_ord = ord("A")
    for profile_name in source_projects:
        project = source_projects[profile_name]
        stored = load_translation_store(project).records.get(source_record.id)
        if stored is None:
            missing_profiles.append(profile_name)
            continue
        selection = effective_candidate_selection(stored, strict_active_review=True)
        if isinstance(selection, EffectiveCandidateError):
            raise _err(
                "judge_source_candidate_invalid",
                f"source profile {profile_name} record {source_record.id} "
                f"has invalid effective candidate: {selection.message}",
            )
        if selection is None:
            missing_profiles.append(profile_name)
            continue
        findings = []
        if selection_context is not None:
            findings = validate_record_pair(
                source_record,
                TranslatedRecord(
                    id=source_record.id, target=selection.candidate.target
                ),
                chunk_id,
                selection_context,
            )
        candidates.append(
            JudgeTaskCandidate(
                label=chr(label_ord),
                profile=profile_name,
                target_language=project.config.target_language,
                target_locale=project.config.target_locale
                or project.config.target_language,
                selected_kind=selection.selected_kind,
                selected_ref=selection.selected_ref,
                version_ref=selection.version_ref,
                review_ref=selection.review_ref,
                target=selection.candidate.target,
                target_sha256=sha256_text(selection.candidate.target),
                validation_findings=[
                    JudgeTaskFinding(
                        severity=finding.severity,  # type: ignore[arg-type]
                        rule=finding.rule,
                        message=finding.message,
                    )
                    for finding in findings
                ],
            )
        )
        label_ord += 1

    return candidates, missing_profiles
