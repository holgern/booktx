"""Acceptance logic for judge submissions into selection profiles."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from booktx.acceptance import SubmissionValidationError
from booktx.config import (
    Project,
    _err,
    current_source_sha256,
    load_profile_project,
    load_translation_selection_ledger,
    load_translation_store,
    load_translation_version_ledger,
    write_translation_selection_ledger,
    write_translation_store,
)
from booktx.glossary_match import (
    applicable_entry_indexes,
    contains_term,
    live_mandatory_glossary_sha256,
    target_contains_approved,
)
from booktx.models import (
    JudgeCandidateEvidence,
    JudgeDecision,
    JudgeTask,
    JudgeTaskCandidate,
    TranslatedRecord,
)
from booktx.translation_store import (
    EffectiveCandidateError,
    effective_candidate_selection,
    ensure_store_record,
    sha256_text,
    upsert_translation_version,
)
from booktx.validate import Severity, load_validation_context, validate_record_pair
from booktx.versioning import canonical_json_sha256, lookup_version, resolve_identity

if TYPE_CHECKING:
    from booktx.status import StatusBundle
    from booktx.validate import Finding

__all__ = [
    "SubmittedJudgeRecord",
    "JudgeInsertResult",
    "parse_judge_block_submission",
    "parse_judge_json_submission",
    "accept_judge_submission",
]

_BLOCK_HEADER_RE = re.compile(r"^##\s+(?P<id>\S+)\s*$")


@dataclass(slots=True)
class SubmittedJudgeRecord:
    id: str
    selected: str
    decision_kind: str
    target: str
    reason: str = ""


@dataclass(slots=True)
class JudgeInsertResult:
    accepted_records: int
    version_refs: list[str]


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def parse_judge_json_submission(
    text: str,
) -> tuple[str | None, list[SubmittedJudgeRecord]]:
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise _err("judge_submission_json", "judge JSON submission must be an object")
    task_id = payload.get("judge_task_id")
    records_raw = payload.get("records")
    if not isinstance(records_raw, list):
        raise _err(
            "judge_submission_json",
            "judge JSON submission must contain a records array",
        )
    records: list[SubmittedJudgeRecord] = []
    for item in records_raw:
        if not isinstance(item, dict):
            raise _err("judge_submission_json", "each judge record must be an object")
        records.append(
            SubmittedJudgeRecord(
                id=str(item.get("id") or "").strip(),
                selected=str(item.get("selected") or "").strip(),
                decision_kind=str(item.get("decision_kind") or "").strip(),
                target=str(item.get("target") or ""),
                reason=str(item.get("reason") or ""),
            )
        )
    return (str(task_id).strip() if task_id else None, records)


def parse_judge_block_submission(
    text: str,
) -> tuple[str | None, list[SubmittedJudgeRecord]]:
    task_id: str | None = None
    records: list[SubmittedJudgeRecord] = []
    current_id: str | None = None
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_id, current_lines
        if current_id is None:
            return
        decision: dict[str, str] = {}
        target_lines: list[str] = []
        section = ""
        for raw in current_lines:
            stripped = raw.strip()
            if stripped == "DECISION:":
                section = "decision"
                continue
            if stripped == "TARGET:":
                section = "target"
                continue
            if section == "decision":
                if ":" in raw:
                    key, value = raw.split(":", 1)
                    decision[key.strip().lower()] = value.strip()
            elif section == "target":
                target_lines.append(raw)
        target = "\n".join(target_lines).strip("\n")
        records.append(
            SubmittedJudgeRecord(
                id=current_id,
                selected=decision.get("selected", ""),
                decision_kind=decision.get("decision_kind", ""),
                target=target,
                reason=decision.get("reason", ""),
            )
        )
        current_id = None
        current_lines = []

    for raw in text.splitlines():
        header = _BLOCK_HEADER_RE.match(raw)
        if header:
            flush()
            current_id = header.group("id")
            continue
        if current_id is None:
            if raw.startswith("judge_task_id:"):
                task_id = raw.split(":", 1)[1].strip() or None
            continue
        current_lines.append(raw)
    flush()
    if not records:
        raise _err(
            "judge_submission_block",
            "judge block submission did not contain any records",
        )
    return task_id, records


def _error_findings(findings: list[Finding]) -> list[Finding]:
    blocking_rules = {"glossary_target_missing", "forbidden_term_used"}
    return [
        finding
        for finding in findings
        if finding.severity == Severity.ERROR or finding.rule in blocking_rules
    ]


def _binding_glossary_findings(
    source_record: object,
    *,
    target_text: str,
    chunk_id: str,
    context: object | None,
) -> list[Finding]:
    if context is None:
        return []
    from booktx.validate import Finding

    findings: list[Finding] = []
    applicable = applicable_entry_indexes(source_record.source, context.glossary)
    for idx, entry in enumerate(context.glossary):
        if entry.enforce == "off" or idx not in applicable:
            continue
        severity = Severity.ERROR if entry.enforce == "error" else Severity.WARN
        for forbidden in entry.forbidden_targets:
            if contains_term(
                target_text, forbidden, case_sensitive=entry.case_sensitive
            ):
                findings.append(
                    Finding(
                        chunk_id=chunk_id,
                        severity=severity,
                        rule="forbidden_term_used",
                        message=f"{entry.source} must not be translated as {forbidden}",
                        record_id=source_record.id,
                    )
                )
        if entry.require_target and not target_contains_approved(target_text, entry):
            approved = [entry.target, *entry.target_variants]
            approved = [item for item in approved if item]
            findings.append(
                Finding(
                    chunk_id=chunk_id,
                    severity=severity,
                    rule="glossary_target_missing",
                    message=(
                        f"{entry.source} must be translated using an approved target "
                        f"({' / '.join(approved)})"
                    ),
                    record_id=source_record.id,
                )
            )
    return findings


def _validate_task_profile(project: Project, task: JudgeTask) -> None:
    selected = project.profile or ""
    if task.profile and task.profile != selected:
        raise _err(
            "judge_task_profile_mismatch",
            f"judge task {task.judge_task_id} belongs to profile {task.profile}, "
            f"but selected profile is {selected or '<none>'}",
        )
    if project.profile_config is None or project.profile_config.kind != "selection":
        raise _err("judge_profile_kind", "judge workflows require a selection profile")


def _validate_task_evidence(project: Project, task: JudgeTask) -> None:
    if task.source_sha256 and task.source_sha256 != current_source_sha256(project):
        raise _err(
            "judge_source_drift",
            f"project source changed since judge task {task.judge_task_id} was created",
        )
    if task.profile_config_sha256 is not None and project.profile_config is not None:
        actual = canonical_json_sha256(project.profile_config.model_dump(mode="json"))
        if actual != task.profile_config_sha256:
            raise _err(
                "judge_profile_config_drift",
                f"profile config changed since judge task {task.judge_task_id} "
                "was created",
            )
    if task.source_config_sha256 is not None:
        actual = canonical_json_sha256(project.source_config.model_dump(mode="json"))
        if actual != task.source_config_sha256:
            raise _err(
                "judge_source_config_drift",
                f"source config changed since judge task {task.judge_task_id} "
                "was created",
            )
    if task.mandatory_glossary_sha256 is not None:
        if live_mandatory_glossary_sha256(project) != task.mandatory_glossary_sha256:
            raise _err(
                "task_context_policy_stale",
                "judge task context predates mandatory glossary changes; "
                "recreate the task",
            )


def _candidate_for_label(task_record: object, label: str) -> JudgeTaskCandidate | None:
    for candidate in getattr(task_record, "candidates", []):
        if candidate.label == label:
            return candidate
    return None


def _candidate_evidence(candidate: JudgeTaskCandidate) -> JudgeCandidateEvidence:
    status = "ok"
    if any(f.severity == "error" for f in candidate.validation_findings):
        status = "error"
    elif any(f.severity == "warn" for f in candidate.validation_findings):
        status = "warning"
    return JudgeCandidateEvidence(
        label=candidate.label,
        profile=candidate.profile,
        selected_kind=candidate.selected_kind,
        selected_ref=candidate.selected_ref,
        version_ref=candidate.version_ref,
        review_ref=candidate.review_ref,
        target_sha256=candidate.target_sha256,
        validation_status=status,  # type: ignore[arg-type]
        findings=[
            f"{finding.severity}:{finding.rule}:{finding.message}"
            for finding in candidate.validation_findings
        ],
    )


def _validate_edited_targets_allowed(
    project: Project, item: SubmittedJudgeRecord
) -> None:
    if item.decision_kind != "edited":
        return
    cfg = project.profile_config
    selection_cfg = cfg.selection if cfg is not None else None
    allow_edited = (
        selection_cfg.allow_edited_targets if selection_cfg is not None else True
    )
    if not allow_edited:
        raise _err(
            "judge_edited_disabled",
            f"record {item.id} edited judge targets are disabled for this profile",
        )


def accept_judge_submission(
    project: Project,
    task: JudgeTask,
    submitted: list[SubmittedJudgeRecord],
    *,
    bundle: StatusBundle,
) -> JudgeInsertResult:
    if not submitted:
        raise _err("empty_submission", "no judge decisions to accept")

    _validate_task_profile(project, task)
    _validate_task_evidence(project, task)

    task_records = {record.id: record for record in task.records}
    source_by_id = bundle.index.source_by_id
    source_chunks = bundle.index.source_chunks
    validation_context = load_validation_context(
        project,
        context_view_path=task.context_view_path,
    )

    findings: list[Finding] = []
    store = load_translation_store(project)
    ledger = load_translation_selection_ledger(project)
    version_ledger = load_translation_version_ledger(project)
    from booktx.io_utils import utc_timestamp

    timestamp = utc_timestamp()
    accepted_versions: list[str] = []
    seen_ids: set[str] = set()

    for item in submitted:
        if item.id in seen_ids:
            raise _err("duplicate_record_id", f"duplicate judge record id: {item.id}")
        seen_ids.add(item.id)
        task_record = task_records.get(item.id)
        if task_record is None:
            raise _err(
                "record_not_in_task",
                f"record {item.id} is not part of judge task {task.judge_task_id}",
            )
        if item.decision_kind not in {"copy", "edited"}:
            raise _err(
                "judge_decision_kind",
                f"record {item.id} decision_kind must be copy or edited",
            )
        _validate_edited_targets_allowed(project, item)
        selected_candidate: JudgeTaskCandidate | None = None
        if item.selected and item.selected != "edited":
            selected_candidate = _candidate_for_label(task_record, item.selected)
            if selected_candidate is None:
                raise _err(
                    "judge_selected_label",
                    f"record {item.id} selected label {item.selected!r} "
                    f"is not present in judge task {task.judge_task_id}",
                )
        elif item.decision_kind == "copy":
            raise _err(
                "judge_selected_label",
                f"record {item.id} copy decisions require a candidate label",
            )
        target_text = item.target.strip("\n")
        if not target_text.strip():
            raise _err(
                "judge_empty_target", f"record {item.id} target must not be empty"
            )

        if selected_candidate is not None:
            if item.decision_kind == "copy" and any(
                finding.rule in {"glossary_target_missing", "forbidden_term_used"}
                for finding in selected_candidate.validation_findings
            ):
                raise _err(
                    "judge_candidate_validation",
                    f"record {item.id} selected candidate {selected_candidate.label} "
                    "violates the selection profile glossary "
                    "and cannot be copied unchanged",
                )
            source_project = load_profile_project(
                project.root, selected_candidate.profile
            )
            source_stored = load_translation_store(source_project).records.get(item.id)
            if source_stored is None:
                raise _err(
                    "judge_candidate_missing",
                    f"source profile {selected_candidate.profile} "
                    f"no longer has record {item.id}",
                )
            selection = effective_candidate_selection(
                source_stored, strict_active_review=True
            )
            if isinstance(selection, EffectiveCandidateError) or selection is None:
                raise _err(
                    "judge_candidate_drift",
                    f"source profile {selected_candidate.profile} no longer has "
                    f"the selected effective candidate for record {item.id}",
                )
            if selection.selected_ref != selected_candidate.selected_ref:
                raise _err(
                    "judge_candidate_drift",
                    f"record {item.id} selected candidate ref changed from "
                    f"{selected_candidate.selected_ref} to {selection.selected_ref}",
                )
            live_target_sha = sha256_text(selection.candidate.target)
            if live_target_sha != selected_candidate.target_sha256:
                raise _err(
                    "judge_candidate_hash_drift",
                    f"record {item.id} selected candidate content changed "
                    f"since judge task {task.judge_task_id} was created",
                )
            if item.decision_kind == "copy" and _normalize_newlines(
                target_text
            ) != _normalize_newlines(selected_candidate.target):
                raise _err(
                    "judge_copy_target_mismatch",
                    f"record {item.id} copy target must exactly match "
                    f"selected candidate {selected_candidate.label}",
                )

        if item.id not in source_by_id:
            raise _err("unknown_record_id", f"unknown source record id: {item.id}")
        source_view = source_by_id[item.id]
        source_chunk = source_chunks[source_view.chunk_id]
        source_record = next(
            record for record in source_chunk.records if record.id == item.id
        )
        findings.extend(
            validate_record_pair(
                source_record,
                TranslatedRecord(id=item.id, target=target_text),
                source_chunk.chunk_id,
                validation_context,
            )
        )
        findings.extend(
            _binding_glossary_findings(
                source_record,
                target_text=target_text,
                chunk_id=source_chunk.chunk_id,
                context=validation_context,
            )
        )

    errors = _error_findings(findings)
    if errors:
        raise SubmissionValidationError(errors)

    for item in submitted:
        task_record = task_records[item.id]
        source_view = source_by_id[item.id]
        ensure_store_record(
            store,
            item.id,
            source=source_view.source,
            source_sha256=source_view.source_sha256,
        )
        _track, subversion = lookup_version(
            version_ledger, task_record.output_version_ref
        )
        upsert_translation_version(
            store.records[item.id],
            task_record.output_version_ref,
            item.target.strip("\n"),
            updated_at=timestamp,
            activate=True,
            baseline_ref=task_record.output_version_ref,
            baseline_sha256=subversion.baseline_sha256,
            context_view_sha256=task.context_view_sha256,
            context_view_path=task.context_view_path,
        )
        accepted_versions.append(task_record.output_version_ref)

        selected_candidate = (
            _candidate_for_label(task_record, item.selected)
            if item.selected and item.selected != "edited"
            else None
        )
        candidate_evidence = [
            _candidate_evidence(candidate) for candidate in task_record.candidates
        ]
        ledger.records[item.id] = JudgeDecision(
            record_id=item.id,
            output_version_ref=task_record.output_version_ref,
            decision_kind=item.decision_kind,  # type: ignore[arg-type]
            selected_profile=selected_candidate.profile
            if selected_candidate is not None
            else None,
            selected_kind=selected_candidate.selected_kind
            if selected_candidate is not None
            else None,
            selected_ref=selected_candidate.selected_ref
            if selected_candidate is not None
            else None,
            selected_target_sha256=selected_candidate.target_sha256
            if selected_candidate is not None
            else None,
            judge_task_id=task.judge_task_id,
            judge_model=resolve_identity(project).model,
            reason=item.reason,
            candidate_evidence_sha256=canonical_json_sha256(
                [entry.model_dump(mode="json") for entry in candidate_evidence]
            ),
            candidate_evidence=candidate_evidence,
            created_at=timestamp,
            updated_at=timestamp,
        )

    store.source_sha256 = bundle.snapshot.source.source_sha256
    write_translation_store(project, store)
    ledger.profile = project.profile or ledger.profile
    ledger.source_sha256 = bundle.snapshot.source.source_sha256
    ledger.source_profiles = list(task.source_profiles)
    write_translation_selection_ledger(project, ledger)
    return JudgeInsertResult(
        accepted_records=len(submitted),
        version_refs=accepted_versions,
    )
