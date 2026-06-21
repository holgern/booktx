"""Validation of agent-translated chunks against the spinetx contract.

The validator loads every source chunk in ``.spinetx/chunks/`` and the matching
translated chunk in ``.spinetx/translated/`` (if present), and enforces the
hard rules from ``spinetx_coding_agent_start.md``:

- The translated JSON must be valid JSON.
- The record count must be unchanged.
- No record id may change.
- No target may be empty.
- No placeholder may be removed or changed.
- No protected name may be translated or removed.
- The translated file must contain no commentary outside the JSON structure.

The goal is **one source sentence to one translated sentence**. The validator
never merges or splits records.

A :class:`ValidationReport` collects per-chunk findings and a summary, and is
written to ``.spinetx/reports/``. ``validate_project`` returns the report and
exits non-zero on any mandatory failure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from spinetx.config import Project
from spinetx.context import GlossaryEntry, TranslationContext, load_context
from spinetx.models import Chunk, Placeholder, TranslatedChunk
from spinetx.placeholders import TOKEN_RE, collect_tokens

__all__ = [
    "Severity",
    "Finding",
    "ValidationReport",
    "validate_project",
    "validate_chunk_pair",
    "write_report",
]

#: Severity ordering for reporting.
SEVERITY_ORDER = ("info", "warn", "error")


class Severity:
    """Finding severity labels."""

    INFO = "info"
    WARN = "warn"
    ERROR = "error"


@dataclass(slots=True)
class Finding:
    """One validation finding for one chunk."""

    chunk_id: str
    severity: str
    rule: str
    message: str
    record_id: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "chunk_id": self.chunk_id,
            "severity": self.severity,
            "rule": self.rule,
            "message": self.message,
            "record_id": self.record_id,
        }


@dataclass(slots=True)
class ValidationReport:
    """Aggregated validation result for a project."""

    project: str
    findings: list[Finding] = field(default_factory=list)
    chunks_checked: int = 0
    chunks_passed: int = 0
    chunks_missing_translation: int = 0
    generated_at: str = ""

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == Severity.WARN]

    @property
    def passed(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, object]:
        return {
            "project": self.project,
            "generated_at": self.generated_at,
            "passed": self.passed,
            "chunks_checked": self.chunks_checked,
            "chunks_passed": self.chunks_passed,
            "chunks_missing_translation": self.chunks_missing_translation,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "findings": [f.as_dict() for f in self.findings],
        }


# --- per-pair validation -----------------------------------------------------


def _load_source_chunk(path: Path) -> Chunk:
    return Chunk.model_validate_json(path.read_text("utf-8"))


def _strict_load_translated(path: Path) -> tuple[TranslatedChunk | None, str | None]:
    """Load a translated chunk, detecting commentary outside the JSON.

    Returns ``(model_or_None, error_message_or_None)``. We parse twice: once
    permissively into the model, once strictly to catch trailing commentary.
    """
    raw = path.read_text("utf-8")
    # Strict parse: json.loads rejects trailing data, so commentary after the
    # closing brace (the common agent mistake) is caught here.
    try:
        data = json.loads(raw, parse_int=int, parse_float=float)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc.msg} (line {exc.lineno} col {exc.colno})"
    if not isinstance(data, dict):
        return None, "translated file is not a JSON object"
    try:
        model = TranslatedChunk.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        return None, f"schema mismatch: {exc}"
    # Detect commentary BEFORE the first '{' or AFTER the last '}'.
    stripped = raw.lstrip()
    if not stripped.startswith("{"):
        return model, "translated file has commentary before the JSON object"
    # Find the real end of the top-level object via the parsed span.
    decoder = json.JSONDecoder()
    obj, end = decoder.raw_decode(raw.lstrip())
    trailing = raw.lstrip()[end:].strip()
    if trailing:
        return model, "translated file has commentary after the JSON object"
    return model, None


def _check_placeholders_preserved(
    source_rec, target_rec, chunk_id: str
) -> list[Finding]:
    """Ensure every placeholder token in the source appears unchanged in target."""
    findings: list[Finding] = []
    src_tokens = set(collect_tokens(source_rec.source))
    tgt_text = target_rec.target
    tgt_tokens = set(collect_tokens(tgt_text))
    missing = src_tokens - tgt_tokens
    for tok in sorted(missing):
        findings.append(
            Finding(
                chunk_id=chunk_id,
                severity=Severity.ERROR,
                rule="placeholder_removed_or_changed",
                message=f"placeholder {tok} removed/changed in record {target_rec.id}",
                record_id=target_rec.id,
            )
        )
    # Also flag any NEW placeholder tokens the agent invented.
    extra = tgt_tokens - src_tokens
    for tok in sorted(extra):
        findings.append(
            Finding(
                chunk_id=chunk_id,
                severity=Severity.ERROR,
                rule="placeholder_added",
                message=(
                    f"unexpected placeholder {tok} appears in record "
                    f"{target_rec.id}; placeholders must be preserved exactly"
                ),
                record_id=target_rec.id,
            )
        )
    return findings


def _check_protected_names_preserved(
    source_rec, target_rec, chunk_id: str
) -> list[Finding]:
    """Ensure each protected name (NAME placeholder) survives verbatim.

    Protected names are encoded as ``__NAME_NNN__`` placeholders whose
    ``Placeholder.original`` is the verbatim term. After translation, the token
    must still be present (handled by placeholder check) AND, when restored,
    yield the original term. We additionally forbid the agent from inlining a
    translated form of the name: if the original term was protected but a
    non-token variant appears... we rely on the placeholder check, and add a
    dedicated finding category if the NAME token is absent so the rule name
    matches the spec.
    """
    findings: list[Finding] = []
    visible_tokens = set(collect_tokens(source_rec.source))
    name_tokens = {
        p.token: p.original
        for p in source_rec.placeholders
        if p.kind == "name" and p.token in visible_tokens
    }
    tgt_text = target_rec.target
    for token, original in name_tokens.items():
        if token not in tgt_text:
            findings.append(
                Finding(
                    chunk_id=chunk_id,
                    severity=Severity.ERROR,
                    rule="protected_name_translated_or_removed",
                    message=(
                        f"protected name {original!r} ({token}) was translated "
                        f"or removed in record {target_rec.id}"
                    ),
                    record_id=target_rec.id,
                )
            )
    return findings


def _contains_term(text: str, term: str, *, case_sensitive: bool) -> bool:
    if case_sensitive:
        return term in text
    return term.casefold() in text.casefold()


def _check_forbidden_terms(
    source_rec,
    target_rec,
    chunk_id: str,
    context: TranslationContext | None,
 ) -> list[Finding]:
    """Check glossary forbidden target terms for one record pair."""
    if context is None:
        return []
    findings: list[Finding] = []
    for entry in context.glossary:
        if entry.enforce == "off" or not entry.forbidden_targets:
            continue
        if not _contains_term(
            source_rec.source, entry.source, case_sensitive=entry.case_sensitive
        ):
            continue
        severity = Severity.ERROR if entry.enforce == "error" else Severity.WARN
        findings.extend(
            _forbidden_target_findings(entry, target_rec, chunk_id, severity)
        )
    return findings


def _forbidden_target_findings(
    entry: GlossaryEntry,
    target_rec,
    chunk_id: str,
    severity: str,
 ) -> list[Finding]:
    findings: list[Finding] = []
    for forbidden in entry.forbidden_targets:
        if not _contains_term(
            target_rec.target, forbidden, case_sensitive=entry.case_sensitive
        ):
            continue
        findings.append(
            Finding(
                chunk_id=chunk_id,
                severity=severity,
                rule="forbidden_term_used",
                message=f"{entry.source} must not be translated as {forbidden}",
                record_id=target_rec.id,
            )
        )
    return findings


def validate_chunk_pair(
    source: Chunk,
    translated_path: Path | None,
    context: TranslationContext | None = None,
 ) -> list[Finding]:
    """Validate one source chunk against its translated file (if any)."""
    chunk_id = source.chunk_id
    findings: list[Finding] = []

    if translated_path is None:
        # Missing translation is not itself an error; it just means the chunk
        # is not yet translated. Caller decides how to surface this.
        return findings

    if not translated_path.is_file():
        return findings

    translated, err = _strict_load_translated(translated_path)
    if err is not None:
        findings.append(
            Finding(
                chunk_id=chunk_id,
                severity=Severity.ERROR,
                rule="invalid_json_or_commentary",
                message=err,
            )
        )
        return findings

    # chunk_id must match.
    if translated.chunk_id != chunk_id:
        findings.append(
            Finding(
                chunk_id=chunk_id,
                severity=Severity.ERROR,
                rule="chunk_id_changed",
                message=(
                    f"translated chunk_id is {translated.chunk_id!r}, "
                    f"expected {chunk_id!r}"
                ),
            )
        )

    src_records = {r.id: r for r in source.records}
    tgt_records = {r.id: r for r in translated.records}

    # Record count must be unchanged.
    if len(translated.records) != len(source.records):
        findings.append(
            Finding(
                chunk_id=chunk_id,
                severity=Severity.ERROR,
                rule="record_count_changed",
                message=(
                    f"translated chunk has {len(translated.records)} records, "
                    f"expected {len(source.records)}"
                ),
            )
        )

    # No record id may change: exact set equality.
    if set(src_records) != set(tgt_records):
        missing = sorted(set(src_records) - set(tgt_records))
        added = sorted(set(tgt_records) - set(src_records))
        if missing:
            findings.append(
                Finding(
                    chunk_id=chunk_id,
                    severity=Severity.ERROR,
                    rule="record_id_removed",
                    message=(f"record ids removed/changed: {', '.join(missing)}"),
                )
            )
        if added:
            findings.append(
                Finding(
                    chunk_id=chunk_id,
                    severity=Severity.ERROR,
                    rule="record_id_added",
                    message=(f"unexpected record ids: {', '.join(added)}"),
                )
            )

    # Per-record checks for ids present on both sides.
    for rid, src_rec in src_records.items():
        tgt_rec = tgt_records.get(rid)
        if tgt_rec is None:
            continue
        # Target must not be empty.
        if not tgt_rec.target or not tgt_rec.target.strip():
            findings.append(
                Finding(
                    chunk_id=chunk_id,
                    severity=Severity.ERROR,
                    rule="empty_target",
                    message=f"record {rid} has an empty target",
                    record_id=rid,
                )
            )
        findings.extend(_check_placeholders_preserved(src_rec, tgt_rec, chunk_id))
        findings.extend(_check_protected_names_preserved(src_rec, tgt_rec, chunk_id))
        findings.extend(_check_forbidden_terms(src_rec, tgt_rec, chunk_id, context))

    return findings


# --- project-level validation -----------------------------------------------


def validate_project(project: Project) -> ValidationReport:
    """Validate every translated chunk in ``project``.

    Missing translations are *not* errors; only present-but-invalid translated
    files produce error findings. Stale translated files whose chunk id no
    longer exists produce a warning.
    """
    report = ValidationReport(
        project=str(project.root),
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    chunk_paths = {p.stem: p for p in project.chunks()}
    translated_paths = {p.stem: p for p in project.translated()}
    try:
        context = load_context(project)
    except Exception as exc:  # noqa: BLE001 - surface invalid context as a finding
        context = None
        report.findings.append(
            Finding(
                chunk_id="context",
                severity=Severity.ERROR,
                rule="invalid_context",
                message=f"context.json is invalid: {exc}",
            )
        )

    for chunk_id in sorted(chunk_paths):
        source = _load_source_chunk(chunk_paths[chunk_id])
        report.chunks_checked += 1
        translated_path = translated_paths.get(chunk_id)
        if translated_path is None:
            report.chunks_missing_translation += 1
            continue
        findings = validate_chunk_pair(source, translated_path, context)
        report.findings.extend(findings)
        if not any(f.severity == Severity.ERROR for f in findings):
            report.chunks_passed += 1

    # Stale translated files: present in translated/ but no matching chunk.
    stale = sorted(set(translated_paths) - set(chunk_paths))
    for chunk_id in stale:
        report.findings.append(
            Finding(
                chunk_id=chunk_id,
                severity=Severity.WARN,
                rule="stale_translation",
                message=(
                    f"translated chunk {chunk_id} has no matching source chunk "
                    f"(left in place; remove or re-extract to clear)"
                ),
            )
        )

    return report


def write_report(project: Project, report: ValidationReport) -> Path:
    """Persist the validation report to ``.spinetx/reports/`` and return it."""
    project.reports_dir.mkdir(parents=True, exist_ok=True)
    out = project.reports_dir / "validation-report.json"
    out.write_text(
        json.dumps(report.as_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return out


# Keep TOKEN_RE referenced for callers that want the raw regex.
_ = TOKEN_RE
_ = Placeholder  # re-export surface stability
