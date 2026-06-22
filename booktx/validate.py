"""Validation of agent-translated chunks against the booktx contract.

The validator loads every source chunk in ``.booktx/chunks/`` and the matching
translated chunk in ``.booktx/translated/`` (if present), and enforces the
hard rules from ``booktx_coding_agent_start.md``:

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
written to ``.booktx/reports/``. ``validate_project`` returns the report and
exits non-zero on any mandatory failure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from booktx.config import Project, load_manifest, load_translation_store
from booktx.context import GlossaryEntry, TranslationContext, load_context
from booktx.epub_manifest import load_epub_template_from_manifest
from booktx.models import Chunk, Placeholder, TranslatedChunk, TranslatedRecord
from booktx.placeholders import TOKEN_RE, collect_tokens
from booktx.progress import source_record_sha256

__all__ = [
    "Severity",
    "Finding",
    "ValidationReport",
    "EffectiveTranslations",
    "strict_load_translated",
    "validate_record_pair",
    "load_effective_translated_chunks",
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


@dataclass(slots=True)
class EffectiveTranslations:
    """Merged accepted translations from the store and valid legacy chunks."""

    chunks: dict[str, TranslatedChunk] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)


# --- per-pair validation -----------------------------------------------------


def _load_source_chunk(path: Path) -> Chunk:
    return Chunk.model_validate_json(path.read_text("utf-8"))


def strict_load_translated(path: Path) -> tuple[TranslatedChunk | None, str | None]:
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


def validate_record_pair(
    source_rec,
    target_rec,
    chunk_id: str,
    context: TranslationContext | None = None,
) -> list[Finding]:
    """Validate one translated record against one source record."""
    findings: list[Finding] = []
    if not target_rec.target or not target_rec.target.strip():
        findings.append(
            Finding(
                chunk_id=chunk_id,
                severity=Severity.ERROR,
                rule="empty_target",
                message=f"record {target_rec.id} has an empty target",
                record_id=target_rec.id,
            )
        )
    findings.extend(_check_placeholders_preserved(source_rec, target_rec, chunk_id))
    findings.extend(_check_protected_names_preserved(source_rec, target_rec, chunk_id))
    findings.extend(_check_forbidden_terms(source_rec, target_rec, chunk_id, context))
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


def _validate_translated_chunk(
    source: Chunk,
    translated: TranslatedChunk,
    context: TranslationContext | None = None,
) -> list[Finding]:
    """Validate one source chunk against a translated chunk model."""
    chunk_id = source.chunk_id
    findings: list[Finding] = []

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
        findings.extend(validate_record_pair(src_rec, tgt_rec, chunk_id, context))

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
        return findings
    if not translated_path.is_file():
        return findings

    translated, err = strict_load_translated(translated_path)
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
    return _validate_translated_chunk(source, translated, context)


# --- project-level validation -----------------------------------------------


def load_effective_translated_chunks(
    project: Project,
    *,
    source_chunks: dict[str, Chunk] | None = None,
    context: TranslationContext | None = None,
) -> EffectiveTranslations:
    """Merge valid legacy chunk files and accepted store records."""
    if source_chunks is None:
        source_chunks = {
            path.stem: _load_source_chunk(path)
            for path in sorted(project.chunks(), key=lambda path: path.stem)
        }

    translated_paths = {p.stem: p for p in project.translated()}
    findings: list[Finding] = []
    valid_legacy: dict[str, TranslatedChunk] = {}
    store_records: dict[str, dict[str, TranslatedRecord]] = {}

    for chunk_id, source in source_chunks.items():
        translated_path = translated_paths.get(chunk_id)
        if translated_path is None:
            continue
        translated, err = strict_load_translated(translated_path)
        if err is not None:
            findings.append(
                Finding(
                    chunk_id=chunk_id,
                    severity=Severity.ERROR,
                    rule="invalid_json_or_commentary",
                    message=err,
                )
            )
            continue
        chunk_findings = _validate_translated_chunk(source, translated, context)
        findings.extend(chunk_findings)
        if not any(f.severity == Severity.ERROR for f in chunk_findings):
            valid_legacy[chunk_id] = translated

    stale = sorted(set(translated_paths) - set(source_chunks))
    for chunk_id in stale:
        findings.append(
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

    try:
        store = load_translation_store(project)
    except Exception as exc:  # noqa: BLE001 - surface invalid store structure
        findings.append(
            Finding(
                chunk_id="store",
                severity=Severity.ERROR,
                rule="invalid_translation_store",
                message=f"translation-store.json is invalid: {exc}",
            )
        )
        store = None

    if store is not None:
        for record_id, stored in store.records.items():
            chunk_id = stored.chunk_id
            source = source_chunks.get(chunk_id)
            if source is None:
                findings.append(
                    Finding(
                        chunk_id=chunk_id,
                        severity=Severity.ERROR,
                        rule="stale_store_record",
                        message=(
                            f"store record {record_id} has no matching source "
                            f"chunk {chunk_id}"
                        ),
                        record_id=record_id,
                    )
                )
                continue
            if chunk_id != source.chunk_id or chunk_id != record_id.split("-", 1)[0]:
                findings.append(
                    Finding(
                        chunk_id=chunk_id,
                        severity=Severity.ERROR,
                        rule="store_chunk_mismatch",
                        message=(
                            f"store record {record_id} points to chunk {chunk_id}, "
                            f"but the record id does not match that chunk"
                        ),
                        record_id=record_id,
                    )
                )
                continue

            source_records = {record.id: record for record in source.records}
            source_rec = source_records.get(record_id)
            if source_rec is None:
                findings.append(
                    Finding(
                        chunk_id=chunk_id,
                        severity=Severity.ERROR,
                        rule="stale_store_record",
                        message=(
                            f"store record {record_id} has no matching source record"
                        ),
                        record_id=record_id,
                    )
                )
                continue
            if stored.source_sha256 and stored.source_sha256 != source_record_sha256(
                source_rec.source
            ):
                findings.append(
                    Finding(
                        chunk_id=chunk_id,
                        severity=Severity.ERROR,
                        rule="stale_store_record",
                        message=(
                            f"store record {record_id} no longer matches the "
                            "current source text"
                        ),
                        record_id=record_id,
                    )
                )
                continue

            translated_rec = TranslatedRecord(id=record_id, target=stored.target)
            record_findings = validate_record_pair(
                source_rec, translated_rec, chunk_id, context
            )
            findings.extend(record_findings)
            if any(f.severity == Severity.ERROR for f in record_findings):
                continue
            store_records.setdefault(chunk_id, {})[record_id] = translated_rec

    merged: dict[str, TranslatedChunk] = {}
    for chunk_id, source in source_chunks.items():
        by_id: dict[str, TranslatedRecord] = {}
        legacy = valid_legacy.get(chunk_id)
        if legacy is not None:
            by_id.update({record.id: record for record in legacy.records})
        by_id.update(store_records.get(chunk_id, {}))
        if not by_id:
            continue
        merged[chunk_id] = TranslatedChunk(
            chunk_id=chunk_id,
            records=[
                by_id[record.id] for record in source.records if record.id in by_id
            ],
        )

    return EffectiveTranslations(chunks=merged, findings=findings)


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

    source_chunks = {
        path.stem: _load_source_chunk(path)
        for path in sorted(project.chunks(), key=lambda path: path.stem)
    }
    new_epub_pipeline = _uses_new_epub_pipeline(project)
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

    effective = load_effective_translated_chunks(
        project,
        source_chunks=source_chunks,
        context=context,
    )
    report.findings.extend(effective.findings)

    for chunk_id in sorted(source_chunks):
        source = source_chunks[chunk_id]
        report.chunks_checked += 1
        if new_epub_pipeline:
            report.findings.extend(_check_new_epub_source_chunk(source))
        translated_chunk = effective.chunks.get(chunk_id)
        if translated_chunk is None or not translated_chunk.records:
            report.chunks_missing_translation += 1
            continue
        chunk_errors = [
            finding
            for finding in report.findings
            if finding.chunk_id == chunk_id and finding.severity == Severity.ERROR
        ]
        if len(translated_chunk.records) == len(source.records) and not chunk_errors:
            report.chunks_passed += 1

    return report


def _uses_new_epub_pipeline(project: Project) -> bool:
    if project.config.format != "epub":
        return False
    manifest = load_manifest(project)
    if manifest is None:
        return False
    try:
        load_epub_template_from_manifest(manifest)
    except ValueError:
        return False
    return True


def _check_new_epub_source_chunk(source: Chunk) -> list[Finding]:
    findings: list[Finding] = []
    for record in source.records:
        if "__TAG_" in record.source or "__SPANTX_" in record.source:
            findings.append(
                Finding(
                    chunk_id=source.chunk_id,
                    severity=Severity.ERROR,
                    rule="epub_source_contains_legacy_placeholders",
                    message=(
                        "new EPUB extraction must not expose __TAG_NNN__ or "
                        "__SPANTX_NNNN__ placeholders in source records"
                    ),
                    record_id=record.id,
                )
            )
    return findings


def write_report(project: Project, report: ValidationReport) -> Path:
    """Persist the validation report to ``.booktx/reports/`` and return it."""
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
