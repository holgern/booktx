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
from html import unescape
from pathlib import Path
from typing import Any

from booktx.chunking import RECORD_ID_SCHEME
from booktx.config import (
    Project,
    load_manifest,
    load_translation_store,
    load_translation_version_ledger,
    resolve_stored_path,
    translation_store_path,
)
from booktx.context import (
    GlossaryEntry,
    TranslationContext,
    analyze_context_markdown_drift,
    context_markdown_path,
    load_context,
    render_context_markdown,
)
from booktx.epub_manifest import load_epub_template_from_manifest
from booktx.glossary_match import (
    contains_term,
    source_rule_applies,
    target_contains_approved,
    target_terms,
)
from booktx.models import (
    Chunk,
    Placeholder,
    QualityReviewConfig,
    Record,
    StoredTranslationRecordV2,
    TranslatedChunk,
    TranslatedRecord,
    TranslationReviewCandidate,
)
from booktx.placeholders import TOKEN_RE, collect_tokens
from booktx.progress import source_record_sha256
from booktx.translation_store import (
    active_candidate,
    effective_target_candidate,
    find_review_candidate,
    review_chain_is_stale,
)
from booktx.versioning import lookup_version

__all__ = [
    "Severity",
    "Finding",
    "ValidationReport",
    "EffectiveTranslations",
    "load_validation_context",
    "strict_load_translated",
    "validate_record_pair",
    "load_effective_translated_chunks",
    "validate_project",
    "validate_chunk_pair",
    "review_coverage_findings",
    "write_report",
]

#: Severity ordering for reporting.
SEVERITY_ORDER = ("info", "warn", "error")
SUPPORTED_SOURCE_CHUNK_SCHEMA_VERSIONS = {2, 3}


class Severity:
    """Finding severity labels."""

    INFO = "info"
    WARN = "warn"
    ERROR = "error"


@dataclass(slots=True)
class Finding:
    """One validation finding for one chunk.

    Optional location/context fields are populated from EPUB inline-XHTML
    preflight findings and left empty for plain record-level findings. They
    are included in :meth:`as_dict` only when non-empty so the JSON report
    stays backward compatible and readable.
    """

    chunk_id: str
    severity: str
    rule: str
    message: str
    record_id: str = ""
    record_ids: list[str] = field(default_factory=list)
    chapter_id: str = ""
    chapter_title: str = ""
    span_index: int | None = None
    block_id: str = ""
    document_href: str = ""
    source: str = ""
    target: str = ""
    candidate_kind: str = ""
    candidate_ref: str = ""
    candidate_scope: str = ""

    def as_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "chunk_id": self.chunk_id,
            "severity": self.severity,
            "rule": self.rule,
            "message": self.message,
            "record_id": self.record_id,
        }
        if self.record_ids:
            data["record_ids"] = list(self.record_ids)
        if self.chapter_id:
            data["chapter_id"] = self.chapter_id
        if self.chapter_title:
            data["chapter_title"] = self.chapter_title
        if self.span_index is not None:
            data["span_index"] = self.span_index
        if self.block_id:
            data["block_id"] = self.block_id
        if self.document_href:
            data["document_href"] = self.document_href
        if self.source:
            data["source"] = self.source
        if self.target:
            data["target"] = self.target
        if self.candidate_kind:
            data["candidate_kind"] = self.candidate_kind
        if self.candidate_ref:
            data["candidate_ref"] = self.candidate_ref
        if self.candidate_scope:
            data["candidate_scope"] = self.candidate_scope
        return data


@dataclass(slots=True)
class ValidationReport:
    """Aggregated validation result for a project."""

    project: str
    profile: str = ""
    target_language: str = ""
    target_locale: str = ""
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
            "profile": self.profile,
            "target_language": self.target_language,
            "target_locale": self.target_locale,
            "generated_at": self.generated_at,
            "passed": self.passed,
            "chunks_checked": self.chunks_checked,
            "chunks_passed": self.chunks_passed,
            "chunks_missing_translation": self.chunks_missing_translation,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "findings": [f.as_dict() for f in self.findings],
        }

    @property
    def blocking_findings(self) -> list[Finding]:
        """Findings that affect pass/fail regardless of history mode.

        Effective-output and structural findings always count. Inactive
        historical content findings are excluded: they describe candidates
        that are not the current output and must not fail a normal build.
        """
        return [f for f in self.findings if f.candidate_scope != "inactive"]

    @property
    def inactive_findings(self) -> list[Finding]:
        """Findings about inactive historical translation candidates."""
        return [f for f in self.findings if f.candidate_scope == "inactive"]


@dataclass(slots=True)
class EffectiveTranslations:
    """Merged accepted translations from the store and valid legacy chunks."""

    chunks: dict[str, TranslatedChunk] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)


def validation_exits_nonzero(
    report: ValidationReport,
    *,
    fail_on_warnings: bool = False,
    fail_on_history_warnings: bool = False,
) -> bool:
    """Decide whether the ``validate``/``check`` CLI should exit non-zero.

    Shared by both commands so history handling stays identical:

    - Effective-output and structural findings always count.
    - Inactive historical content errors are fatal when present (they only
      survive as errors under ``--all-versions-strict``, where inactive
      content errors are not downgraded to warnings).
    - Effective/structural warnings count only under ``fail_on_warnings``.
    - Inactive warnings count only under ``fail_on_history_warnings``.
    """
    blocking = report.blocking_findings
    inactive = report.inactive_findings
    if any(f.severity == Severity.ERROR for f in blocking):
        return True
    if any(f.severity == Severity.ERROR for f in inactive):
        return True
    if fail_on_warnings and any(f.severity == Severity.WARN for f in blocking):
        return True
    if fail_on_history_warnings and any(f.severity == Severity.WARN for f in inactive):
        return True
    return False


# --- per-pair validation -----------------------------------------------------

# Recognized outer (enclosing) quotation-mark pairs.
# Visible-text edge checks use these to decide whether a record is fully
# enclosed in dialogue quotes. A pair must match exactly: we reject
# mismatched open/close combinations such as `„...«`.
_SOURCE_OUTER_QUOTE_PAIRS = frozenset(
    {
        ("\u2018", "\u2019"),  # ‘...’
        ("\u201c", "\u201d"),  # “...”
        ('"', '"'),  # "..."
        ("'", "'"),  # '...'
        ("\u00ab", "\u00bb"),  # «...»
        ("\u00bb", "\u00ab"),  # »...«
    }
)

# Target pairs are locale-tolerant: German „...“, guillemets, curly
# quotes, low-high ‚...‘, plus straight-quote fallbacks.
_TARGET_OUTER_QUOTE_PAIRS = frozenset(
    {
        ("\u201e", "\u201c"),  # „...“ (German)
        ("\u00bb", "\u00ab"),  # »...« (German guillemets)
        ("\u201c", "\u201d"),  # “...”
        ("\u2018", "\u2019"),  # ‘...’
        ("\u201a", "\u2018"),  # ‚...‘ (German single low-9)
        ('"', '"'),  # "..."
        ("'", "'"),  # '...'
        ("\u00ab", "\u00bb"),  # «...»
    }
)


def _visible_text_for_quote_edges(fragment: str) -> str:
    """Return visible text for quote-edge checks.

    When the fragment carries inline XHTML, the surrounding/inner tags must
    not affect which characters sit at the visible edges, so `<i>‘X.’</i>`
    is treated as `‘X.’`. Plain (non-XHTML) fragments are only HTML-unescaped
    and stripped of surrounding whitespace.
    """
    if "<" in fragment and ">" in fragment:
        try:
            from booktx.epub_inline_xhtml import strip_inline_xhtml

            return strip_inline_xhtml(fragment).strip()
        except Exception:  # noqa: BLE001 - never mask XHTML validation root cause
            return unescape(fragment).strip()
    return unescape(fragment).strip()


def _outer_quote_pair(text: str) -> tuple[str, str] | None:
    """Return the (open, close) visible edge chars of *text*, or None.

    None is returned for fragments shorter than two visible characters so
    that a lone quote mark or empty record is never reported as a complete
    outer pair on either side.
    """
    visible = _visible_text_for_quote_edges(text)
    if len(visible) < 2:
        return None
    return visible[0], visible[-1]


def _source_is_fully_outer_quoted(source_text: str) -> bool:
    pair = _outer_quote_pair(source_text)
    return pair in _SOURCE_OUTER_QUOTE_PAIRS


def _target_is_fully_outer_quoted(target_text: str) -> bool:
    pair = _outer_quote_pair(target_text)
    return pair in _TARGET_OUTER_QUOTE_PAIRS


def _load_source_chunk(path: Path) -> Chunk:
    return Chunk.model_validate_json(path.read_text("utf-8"))


def _load_source_chunk_with_findings(path: Path) -> tuple[Chunk | None, list[Finding]]:
    chunk_id = path.stem
    try:
        raw = json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as exc:
        return None, [
            Finding(
                chunk_id=chunk_id,
                severity=Severity.ERROR,
                rule="invalid_source_chunk",
                message=(
                    f"source chunk {path.name} is invalid JSON: {exc.msg} "
                    f"(line {exc.lineno} col {exc.colno})"
                ),
            )
        ]
    if not isinstance(raw, dict):
        return None, [
            Finding(
                chunk_id=chunk_id,
                severity=Severity.ERROR,
                rule="invalid_source_chunk",
                message=f"source chunk {path.name} is not a JSON object",
            )
        ]
    try:
        chunk = Chunk.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        return None, [
            Finding(
                chunk_id=str(raw.get("chunk_id", chunk_id)),
                severity=Severity.ERROR,
                rule="invalid_source_chunk",
                message=f"source chunk {path.name} is invalid: {exc}",
            )
        ]
    findings: list[Finding] = []
    raw_schema_version = raw.get("schema_version")
    if (
        raw_schema_version is not None
        and isinstance(raw_schema_version, int)
        and raw_schema_version not in SUPPORTED_SOURCE_CHUNK_SCHEMA_VERSIONS
    ):
        findings.append(
            Finding(
                chunk_id=chunk.chunk_id,
                severity=Severity.ERROR,
                rule="unsupported_chunk_schema_version",
                message=(
                    f"chunk {chunk.chunk_id} uses unsupported schema_version "
                    f"{raw_schema_version}"
                ),
            )
        )
    return chunk, findings


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
    source_rec: Record, target_rec: TranslatedRecord, chunk_id: str
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
    source_rec: Record, target_rec: TranslatedRecord, chunk_id: str
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


def _check_inline_xhtml(
    source_rec: Record, target_rec: TranslatedRecord, chunk_id: str
) -> list[Finding]:
    if source_rec.source_markup != "epub-inline-xhtml:v1":
        return []
    from booktx.epub_inline_xhtml import sanitize_target_fragment

    sanitized = sanitize_target_fragment(target_rec.target, source_rec.source)
    findings: list[Finding] = []
    for issue in sanitized.issues:
        findings.append(
            Finding(
                chunk_id=chunk_id,
                severity=issue.severity,
                rule=issue.rule,
                message=issue.message,
                record_id=target_rec.id,
            )
        )
    return findings


def _check_outer_quotation_preserved(
    source_rec: Record, target_rec: TranslatedRecord, chunk_id: str
) -> list[Finding]:
    """Require a complete enclosing quote pair in the target when the source has one.

    Rule ``outer_quotation_marks_preserved``. The check operates on visible
    text so inline XHTML tags at the edges are ignored. Only records whose
    visible source text starts AND ends with a recognized outer pair are in
    scope; split/continuation dialogue spans are not flagged.
    """
    if not _source_is_fully_outer_quoted(source_rec.source):
        return []
    if _target_is_fully_outer_quoted(target_rec.target):
        return []
    source_pair = _outer_quote_pair(source_rec.source)
    # _source_is_fully_outer_quoted() above already confirmed a recognized
    # source pair exists, so source_pair cannot be None here.
    assert source_pair is not None
    target_pair = _outer_quote_pair(target_rec.target)
    detail = "the target is not enclosed in a complete accepted quotation pair"
    if target_pair is not None:
        opener, closer = target_pair
        accepted_openers = {pair[0] for pair in _TARGET_OUTER_QUOTE_PAIRS}
        accepted_closers = {pair[1] for pair in _TARGET_OUTER_QUOTE_PAIRS}
        source_openers = {pair[0] for pair in _SOURCE_OUTER_QUOTE_PAIRS}
        source_closers = {pair[1] for pair in _SOURCE_OUTER_QUOTE_PAIRS}
        if opener not in accepted_openers and opener not in source_openers:
            detail = "the target opening quotation mark is not recognized"
        elif (
            closer is not None
            and closer not in accepted_closers
            and closer not in source_closers
        ):
            detail = "the target closing quotation mark is not recognized"
        elif opener in accepted_openers:
            # Recognized opener but the pair as a whole is not accepted:
            # the closer is missing or mismatches the opener.
            matching = {
                pair[1] for pair in _TARGET_OUTER_QUOTE_PAIRS if pair[0] == opener
            }
            if closer in matching:
                # Defensive: should have been caught by the pass branch.
                detail = "the target quotation pair is not accepted"
            elif closer in accepted_closers or closer in source_closers:
                detail = (
                    "the target mixes quote styles within one outer pair "
                    f"(opens {opener!r}, closes {closer!r})"
                )
            else:
                detail = (
                    f"the target opens {opener!r} but lacks the matching closing quote"
                )
        elif closer in accepted_closers or closer in source_closers:
            detail = (
                f"the target closes {closer!r} but lacks the matching opening quote"
            )
    return [
        Finding(
            chunk_id=chunk_id,
            severity=Severity.ERROR,
            rule="outer_quotation_marks_preserved",
            message=(
                f"record {target_rec.id} source is fully enclosed in "
                f"quotation marks {tuple(c for c in source_pair)}, but " + detail
            ),
            record_id=target_rec.id,
            source=source_rec.source,
            target=target_rec.target,
        )
    ]


def validate_record_pair(
    source_rec: Record,
    target_rec: TranslatedRecord,
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
    findings.extend(
        _check_required_glossary_targets(source_rec, target_rec, chunk_id, context)
    )
    findings.extend(_check_inline_xhtml(source_rec, target_rec, chunk_id))
    findings.extend(_check_outer_quotation_preserved(source_rec, target_rec, chunk_id))
    return findings


def _check_forbidden_terms(
    source_rec: Record,
    target_rec: TranslatedRecord,
    chunk_id: str,
    context: TranslationContext | None,
) -> list[Finding]:
    """Check glossary forbidden target terms for one record pair.

    Forbidden targets are scoped to records whose source contains the
    entry's source term or one of its source variants.
    """
    if context is None:
        return []
    findings: list[Finding] = []
    for entry in context.glossary:
        if entry.enforce == "off" or not entry.forbidden_targets:
            continue
        if not source_rule_applies(source_rec.source, entry):
            continue
        severity = Severity.ERROR if entry.enforce == "error" else Severity.WARN
        findings.extend(
            _forbidden_target_findings(entry, target_rec, chunk_id, severity)
        )
    return findings


def _forbidden_target_findings(
    entry: GlossaryEntry,
    target_rec: TranslatedRecord,
    chunk_id: str,
    severity: str,
) -> list[Finding]:
    findings: list[Finding] = []
    for forbidden in entry.forbidden_targets:
        if not contains_term(
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


def _check_required_glossary_targets(
    source_rec: Record,
    target_rec: TranslatedRecord,
    chunk_id: str,
    context: TranslationContext | None,
) -> list[Finding]:
    """Positively enforce approved glossary targets for one record pair.

    When an entry has ``require_target`` and its source rule applies, the
    target must contain the approved target or one of its target variants;
    otherwise a ``glossary_target_missing`` finding is emitted at the entry's
    enforcement severity.

    This is a record-level heuristic: it proves only that an allowed target
    form occurs somewhere in the same record. It does not establish
    word-level source/target alignment.
    """
    if context is None:
        return []
    findings: list[Finding] = []
    for entry in context.glossary:
        if entry.enforce == "off" or not entry.require_target:
            continue
        approved = target_terms(entry)
        if not approved:
            # Cannot require a target that is undefined.
            continue
        if not source_rule_applies(source_rec.source, entry):
            continue
        if target_contains_approved(target_rec.target, entry):
            continue
        severity = Severity.ERROR if entry.enforce == "error" else Severity.WARN
        findings.append(
            Finding(
                chunk_id=chunk_id,
                severity=severity,
                rule="glossary_target_missing",
                message=(
                    f"{entry.source} must be translated using an approved "
                    f"target ({' / '.join(approved)})"
                ),
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
    if translated is None:
        return findings
    return _validate_translated_chunk(source, translated, context)


def load_validation_context(
    project: Project, *, context_view_path: str | None = None
) -> TranslationContext | None:
    """Load the context that should be used for validation."""
    if context_view_path is None:
        return load_context(project)
    path = resolve_stored_path(project, context_view_path)
    return TranslationContext.model_validate_json(path.read_text("utf-8"))


# --- project-level validation -----------------------------------------------


def _active_review_usability_finding(
    stored: StoredTranslationRecordV2,
    chunk_id: str,
    record_id: str,
) -> Finding | None:
    """Return an ERROR finding when an active_review cannot be used safely.

    A set-but-unusable active_review means the selected output would silently
    fall back to the active translation version, which is a data-corruption
    risk. Missing, rejected, stale, cyclic, or invalid-pass-order reviews are
    reported as errors regardless of the pass ``enforce`` setting.
    """
    if stored.active_review is None:
        return None
    review = find_review_candidate(stored, stored.active_review)
    if review is None:
        return Finding(
            chunk_id=chunk_id,
            severity=Severity.ERROR,
            rule="active_review_missing",
            message=(
                f"store record {record_id} active_review {stored.active_review!r} "
                "has no matching review candidate"
            ),
            record_id=record_id,
        )
    if review.status != "accepted":
        return Finding(
            chunk_id=chunk_id,
            severity=Severity.ERROR,
            rule="active_review_not_accepted",
            message=(
                f"active review {review.review_ref} for record {record_id} "
                f"is {review.status!r}, not accepted"
            ),
            record_id=record_id,
        )
    if review_chain_is_stale(stored, review.review_ref):
        return Finding(
            chunk_id=chunk_id,
            severity=Severity.ERROR,
            rule="active_review_base_drift",
            message=(
                f"active review {review.review_ref} for record {record_id} "
                "has a stale or missing derivation chain"
            ),
            record_id=record_id,
        )
    return None


def _accepted_review_for_pass(
    stored: StoredTranslationRecordV2,
    pass_number: int,
) -> TranslationReviewCandidate | None:
    """Return an accepted, chain-valid review candidate for a pass, if any."""
    for review in stored.reviews:
        if review.pass_number != pass_number:
            continue
        if review.status != "accepted":
            continue
        if review_chain_is_stale(stored, review.review_ref):
            continue
        return review
    return None


def _stale_or_rejected_review_for_pass(
    stored: StoredTranslationRecordV2,
    pass_number: int,
) -> TranslationReviewCandidate | None:
    """Return a stale, rejected, or superseded review for a pass, if any."""
    for review in stored.reviews:
        if review.pass_number != pass_number:
            continue
        if review.status == "accepted" and not review_chain_is_stale(
            stored, review.review_ref
        ):
            continue
        return review
    return None


def review_coverage_findings(
    stored: StoredTranslationRecordV2,
    quality_cfg: QualityReviewConfig,
    chunk_id: str,
    record_id: str,
    *,
    force_error: bool = False,
) -> list[Finding]:
    """Per-pass review-coverage findings for one record.

    ``force_error`` (used by ``build --require-reviewed``) treats every coverage
    gap as an ERROR regardless of the pass ``enforce`` setting. Otherwise gaps
    are emitted only when ``enforce`` is ``warn`` or ``error`` and the severity
    follows ``enforce``.
    """
    findings: list[Finding] = []
    if not quality_cfg.enabled:
        return findings
    # Only records with an accepted active translation version are eligible.
    active = active_candidate(stored)
    if active is None or active.status != "accepted":
        return findings
    pass_cfg_by_number = {p.pass_number: p for p in quality_cfg.passes}
    for pass_number in quality_cfg.active_passes:
        pcfg = pass_cfg_by_number.get(pass_number)
        enforce = pcfg.enforce if pcfg is not None else "off"
        if force_error:
            severity = Severity.ERROR
        else:
            if enforce == "off":
                continue
            severity = Severity.ERROR if enforce == "error" else Severity.WARN
        # Blocked by a missing required prior pass?
        required = pcfg.required_base_pass if pcfg is not None else None
        if (
            required is not None
            and required != pass_number
            and _accepted_review_for_pass(stored, required) is None
        ):
            findings.append(
                Finding(
                    chunk_id=chunk_id,
                    severity=severity,
                    rule="review_pass_blocked",
                    message=(
                        f"record {record_id} pass {pass_number} blocked: "
                        f"required pass {required} is missing"
                    ),
                    record_id=record_id,
                )
            )
            continue
        if _accepted_review_for_pass(stored, pass_number) is not None:
            continue
        if _stale_or_rejected_review_for_pass(stored, pass_number) is not None:
            findings.append(
                Finding(
                    chunk_id=chunk_id,
                    severity=severity,
                    rule="stale_review_candidate",
                    message=(
                        f"record {record_id} has only a stale review "
                        f"for pass {pass_number}"
                    ),
                    record_id=record_id,
                )
            )
        else:
            findings.append(
                Finding(
                    chunk_id=chunk_id,
                    severity=severity,
                    rule="missing_review_candidate",
                    message=(
                        f"record {record_id} is missing an accepted review "
                        f"for pass {pass_number}"
                    ),
                    record_id=record_id,
                )
            )
    return findings


def load_effective_translated_chunks(  # noqa: C901
    project: Project,
    *,
    source_chunks: dict[str, Chunk] | None = None,
    context: TranslationContext | None = None,
    include_inactive_versions: bool = False,
    all_versions_strict: bool = False,
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
        if translated is None:
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

    raw_store_version = None
    store_path = translation_store_path(project)
    if store_path.is_file():
        try:
            raw_store = json.loads(store_path.read_text("utf-8"))
            if isinstance(raw_store, dict):
                raw_store_version = raw_store.get("version")
        except Exception:  # noqa: BLE001
            raw_store_version = None

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

    try:
        ledger = load_translation_version_ledger(project)
    except Exception as exc:  # noqa: BLE001
        findings.append(
            Finding(
                chunk_id="ledger",
                severity=Severity.ERROR,
                rule="invalid_translation_version_ledger",
                message=f"translation-version-ledger.json is invalid: {exc}",
            )
        )
        ledger = None

    quality_cfg = (
        project.profile_config.quality_review
        if project.profile_config is not None
        else None
    )

    if store is not None:
        for record_id, stored in store.records.items():
            chunk_id = f"{stored.chunk_id:04d}"
            source_chunk = source_chunks.get(chunk_id)
            if source_chunk is None:
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
            if (
                chunk_id != source_chunk.chunk_id
                or chunk_id != record_id.split("-", 1)[0]
            ):
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

            source_records = {record.id: record for record in source_chunk.records}
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
            if stored.source and stored.source != source_rec.source:
                findings.append(
                    Finding(
                        chunk_id=chunk_id,
                        severity=Severity.ERROR,
                        rule="source_text_mismatch",
                        message=(
                            f"store record {record_id} no longer matches the current "
                            "source text"
                        ),
                        record_id=record_id,
                    )
                )
                continue

            # Effective output prefers a chain-valid active review candidate
            # over the active translation version. A set-but-unusable
            # active_review is reported as an error because the selected
            # output would otherwise fall back silently.
            review_finding = _active_review_usability_finding(
                stored, chunk_id, record_id
            )
            if review_finding is not None:
                findings.append(review_finding)
            candidate = effective_target_candidate(stored)
            if candidate is None:
                findings.append(
                    Finding(
                        chunk_id=chunk_id,
                        severity=Severity.ERROR,
                        rule="invalid_active_version",
                        message=(
                            f"store record {record_id} has no active version pointing "
                            "to an available candidate"
                        ),
                        record_id=record_id,
                    )
                )
                continue
            # Review candidates are not registered in the translation version
            # ledger and are only returned when already accepted and valid.
            if not isinstance(candidate, TranslationReviewCandidate):
                if ledger is not None and (raw_store_version == 2 or ledger.tracks):
                    try:
                        lookup_version(ledger, candidate.version_ref)
                    except Exception:
                        findings.append(
                            Finding(
                                chunk_id=chunk_id,
                                severity=Severity.ERROR,
                                rule="missing_ledger_version",
                                message=(
                                    f"store record {record_id} references version "
                                    f"{candidate.version_ref} missing from the ledger"
                                ),
                                record_id=record_id,
                            )
                        )
                        continue
                if candidate.status != "accepted":
                    findings.append(
                        Finding(
                            chunk_id=chunk_id,
                            severity=Severity.ERROR,
                            rule="active_version_not_accepted",
                            message=(
                                f"active version {candidate.version_ref} for record "
                                f"{record_id} is not accepted"
                            ),
                            record_id=record_id,
                        )
                    )
                    continue

            translated_rec = TranslatedRecord(id=record_id, target=candidate.target)
            effective_kind = (
                "review"
                if isinstance(candidate, TranslationReviewCandidate)
                else "translation"
            )
            effective_ref = (
                candidate.review_ref
                if isinstance(candidate, TranslationReviewCandidate)
                else candidate.version_ref
            )
            record_findings = validate_record_pair(
                source_rec, translated_rec, chunk_id, context
            )
            for _finding in record_findings:
                _finding.candidate_kind = effective_kind
                _finding.candidate_ref = effective_ref
                _finding.candidate_scope = "effective"
            findings.extend(record_findings)
            if any(f.severity == Severity.ERROR for f in record_findings):
                continue
            store_records.setdefault(chunk_id, {})[record_id] = translated_rec

            if quality_cfg is not None:
                findings.extend(
                    review_coverage_findings(stored, quality_cfg, chunk_id, record_id)
                )

            # The version already content-validated above as the effective
            # output is excluded from the inactive loop. When the effective
            # output is a review candidate, every translation version is
            # historical content, so none is excluded.
            effective_translation_ref = (
                None
                if isinstance(candidate, TranslationReviewCandidate)
                else candidate.version_ref
            )
            for inactive in stored.versions:
                if inactive.version_ref == effective_translation_ref:
                    continue
                # Structural integrity: ledger referential checks run for ALL
                # stored versions in every mode. A missing ledger entry is
                # structural corruption, not a historical content warning, so
                # it stays fatal regardless of include_inactive_versions.
                if ledger is not None and (raw_store_version == 2 or ledger.tracks):
                    try:
                        lookup_version(ledger, inactive.version_ref)
                    except Exception:
                        findings.append(
                            Finding(
                                chunk_id=chunk_id,
                                severity=Severity.ERROR,
                                rule="missing_ledger_version",
                                message=(
                                    f"store record {record_id} references version "
                                    f"{inactive.version_ref} missing from the ledger"
                                ),
                                record_id=record_id,
                            )
                        )
                        continue
                # Content checks (terminology, placeholders, quotation,
                # markup) on inactive historical candidates run only in
                # explicit history mode and are downgraded to warnings
                # unless all_versions_strict.
                if not include_inactive_versions:
                    continue
                inactive_rec = TranslatedRecord(id=record_id, target=inactive.target)
                inactive_findings = validate_record_pair(
                    source_rec, inactive_rec, chunk_id, context
                )
                for finding in inactive_findings:
                    if finding.severity == Severity.ERROR and not all_versions_strict:
                        finding.severity = Severity.WARN
                    finding.candidate_kind = "translation"
                    finding.candidate_ref = inactive.version_ref
                    finding.candidate_scope = "inactive"
                    finding.message = (
                        f"inactive version {inactive.version_ref}: {finding.message}"
                    )
                findings.extend(inactive_findings)

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


def _context_render_drift_finding(
    project: Project, context: TranslationContext
) -> Finding | None:
    """Return a context_render_drift finding, or None when Markdown matches.

    Line endings are normalized before comparing. Unsafe Markdown-only or
    conflicting chapter notes point the user at ``context import-md``; only a
    safe (non chapter-note) render drift suggests ``context render --write``.
    """
    if not context_markdown_path(project).is_file():
        return None
    rendered = render_context_markdown(context)
    current_markdown = (
        context_markdown_path(project)
        .read_bytes()
        .decode("utf-8")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    if current_markdown == rendered.replace("\r\n", "\n"):
        return None
    drift = analyze_context_markdown_drift(project, context)
    if drift.parse_errors:
        detail = "; ".join(drift.parse_errors)
        message = (
            "context.md chapter notes could not be parsed: "
            f"{detail}. Fix {context_markdown_path(project)}, then run "
            "`booktx context import-md . --write`."
        )
    elif drift.unsafe_to_overwrite:
        parts = []
        if drift.missing_in_json:
            parts.append(f"missing_in_json={','.join(drift.missing_in_json)}")
        if drift.conflicting:
            parts.append(f"conflicting={','.join(drift.conflicting)}")
        message = (
            "context.md has chapter notes not safely represented "
            "in context.json. " + " ".join(parts) + ". Run `booktx "
            "context import-md . --write` before rendering, or `booktx "
            "context render . --write --force-discard-md-only` to discard "
            "Markdown-only notes."
        )
    else:
        message = (
            "context.md differs from the current context.json render; "
            "run `booktx context render . --write`"
        )
    return Finding(
        chunk_id="context",
        severity=Severity.WARN,
        rule="context_render_drift",
        message=message,
    )


def _resolve_validation_scope(
    project: Project,
    *,
    chapter_id: str | None = None,
    record_ids: set[str] | None = None,
    task_id: str | None = None,
) -> tuple[str | None, set[str] | None]:
    """Resolve a validation scope from chapter/record/task filters.

    ``task_id`` expands to the task's chapter and record ids so a bounded
    task run scopes validation to just those records. Explicit ``chapter_id``
    and ``record_ids`` are passed through.
    """
    if task_id is None:
        return chapter_id, record_ids
    from booktx.config import load_translation_task

    task = load_translation_task(project, task_id)
    if task is None:
        return chapter_id, record_ids
    task_records = {record.id for record in task.records}
    merged_records = task_records | (record_ids or set())
    resolved_chapter = chapter_id or task.chapter_id or None
    return resolved_chapter, merged_records or None


def _scoped_chunk_ids(
    project: Project,
    source_chunks: dict[str, Chunk],
    *,
    chapter_id: str | None,
    record_ids: set[str] | None,
) -> set[str] | None:
    """Return the set of chunk ids in the validation scope, or None when unscoped.

    ``None`` means whole-project (every chunk is in scope). When a scope is
    set, the returned set contains only chunks holding at least one in-scope
    record, so the chunks_checked/passed/missing counters match the findings
    a scoped run actually shows.
    """
    if chapter_id is None and record_ids is None:
        return None
    scoped: set[str] = set()
    if chapter_id is not None:
        try:
            from booktx.chapters import load_chapter_map

            chapter_map = load_chapter_map(project)
        except Exception:  # noqa: BLE001
            chapter_map = None
        if chapter_map is not None:
            for chapter in chapter_map.chapters:
                if chapter.chapter_id == chapter_id:
                    scoped.update(chapter.chunk_ids)
                    break
    if record_ids:
        for chunk_id, chunk in source_chunks.items():
            if any(record.id in record_ids for record in chunk.records):
                scoped.add(chunk_id)
    return scoped or set()


def epub_preflight_findings_as_validation_findings(
    preflight_findings: list[Any],
) -> list[Finding]:
    """Convert EPUB preflight findings into validation Findings with location."""
    findings: list[Finding] = []
    for pf in preflight_findings:
        findings.append(
            Finding(
                chunk_id=pf.chunk_id or "epub-preflight",
                severity=pf.severity,
                rule=pf.rule,
                message=pf.message,
                record_id=pf.record_id,
                record_ids=list(pf.record_ids),
                chapter_id=pf.chapter_id,
                chapter_title=pf.chapter_title,
                span_index=pf.span_index,
                block_id=pf.block_id,
                document_href=pf.document_href,
                source=pf.source,
                target=pf.target,
            )
        )
    return findings


def _epub_toc_audit_findings(project: Project) -> list[Finding]:
    """Run the EPUB TOC audit and convert findings to validation Findings.

    Returns an empty list for non-EPUB projects or when no EPUB template is
    stored. Audit errors map to validation errors; warnings map to warnings.
    """
    from booktx.epub_toc_audit import audit_epub_chapter_map

    try:
        result = audit_epub_chapter_map(project)
    except Exception:  # noqa: BLE001 - audit must never break validation
        return []
    findings: list[Finding] = []
    for audit_finding in result.findings:
        findings.append(
            Finding(
                chunk_id="epub-chapter-audit",
                severity=audit_finding.severity,
                rule=audit_finding.code,
                message=audit_finding.message,
                document_href=audit_finding.href or "",
                chapter_title=audit_finding.title or "",
                record_id=audit_finding.source_record_id or "",
            )
        )
    return findings


def _maybe_append_epub_toc_audit(
    report: ValidationReport,
    project: Project,
    *,
    new_epub_pipeline: bool,
    resolved_chapter: str | None,
    task_id: str | None,
) -> None:
    """Append EPUB TOC audit findings for unscoped EPUB validation runs.

    EPUB chapter completeness is a source-level concern. It is only surfaced
    for unscoped validation so a bounded `booktx check --chapter` run is not
    blocked by unrelated global chapter-map warnings.
    """
    if not new_epub_pipeline:
        return
    if resolved_chapter is not None or task_id is not None:
        return
    report.findings.extend(_epub_toc_audit_findings(project))


def _epub_output_policy_findings(project: Project) -> list[Finding]:
    """Findings for the resolved EPUB output policy (pre-build, deterministic).

    Covers invalid/explicit-missing language tags, translation profiles that
    preserve a differing source language, and pass-through profiles that
    explicitly opt into rewriting. These are warnings/errors only; CSS cascade
    conflicts are reported by the build audit, not here.
    """
    if project.config.format != "epub":
        return []
    from booktx.epub_output_policy import PolicyError, resolve_epub_output_policy

    findings: list[Finding] = []
    try:
        policy = resolve_epub_output_policy(project)
    except PolicyError as exc:
        msg = str(exc)
        rule = "invalid_epub_output_language"
        if "explicit" in msg and "language" in msg:
            rule = "epub_output_explicit_language_missing"
        elif "underscore" in msg or "not a valid" in msg:
            rule = "invalid_epub_output_language"
        findings.append(
            Finding(
                chunk_id="epub_output",
                severity=Severity.ERROR,
                rule=rule,
                message=msg,
            )
        )
        return findings

    cfg = project.config
    is_pass_through = (
        project.profile_config is not None
        and project.profile_config.kind == "pass-through"
    )
    if (
        policy.language_policy == "preserve"
        and not is_pass_through
        and cfg.source_language
        and cfg.target_language
        and cfg.source_language != cfg.target_language
    ):
        findings.append(
            Finding(
                chunk_id="epub_output",
                severity=Severity.WARN,
                rule="epub_output_language_policy_preserves_source",
                message=(
                    "EPUB output language policy is 'preserve' but the target "
                    f"language {cfg.target_language!r} differs from source "
                    f"{cfg.source_language!r}; output will keep the source language."
                ),
            )
        )
    if is_pass_through and policy.language_policy != "preserve":
        findings.append(
            Finding(
                chunk_id="epub_output",
                severity=Severity.WARN,
                rule="epub_output_pass_through_rewrite_enabled",
                message=(
                    "pass-through profile explicitly opts into EPUB output "
                    "rewriting; default byte identity is lost."
                ),
            )
        )
    return findings


def _soft_hyphen_findings(project: Project, effective) -> list[Finding]:
    """Warn once per record whose target text contains a soft hyphen (U+00AD).

    Soft hyphens in target text cause unpredictable reader-side breaks. They
    are warnings because a translator may insert them intentionally.
    """
    if project.config.format != "epub":
        return []
    soft_hyphen = "\u00ad"
    findings: list[Finding] = []
    for chunk in effective.chunks.values():
        for record in chunk.records:
            if soft_hyphen in record.target:
                findings.append(
                    Finding(
                        chunk_id=chunk.chunk_id,
                        severity=Severity.WARN,
                        rule="target_contains_soft_hyphen",
                        message=(
                            f"target for record {record.id} contains a soft hyphen "
                            "(U+00AD); this can cause unpredictable hyphenation."
                        ),
                        record_id=record.id,
                    )
                )
    return findings


def validate_project(
    project: Project,
    *,
    include_inactive_versions: bool = False,
    all_versions_strict: bool = False,
    chapter_id: str | None = None,
    record_ids: set[str] | None = None,
    task_id: str | None = None,
    require_complete: bool = False,
) -> ValidationReport:
    """Validate every translated chunk in ``project``.

    Missing translations are *not* errors; only present-but-invalid translated
    files produce error findings. Stale translated files whose chunk id no
    longer exists produce a warning.
    """
    report = ValidationReport(
        project=str(project.root),
        profile=project.profile or "",
        target_language=project.config.target_language,
        target_locale=project.config.target_locale or "",
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    source_chunks: dict[str, Chunk] = {}
    for path in sorted(project.chunks(), key=lambda path: path.stem):
        chunk, findings = _load_source_chunk_with_findings(path)
        report.findings.extend(findings)
        if chunk is not None:
            source_chunks[path.stem] = chunk
    try:
        manifest = load_manifest(project)
    except Exception as exc:  # noqa: BLE001
        manifest = None
        report.findings.append(
            Finding(
                chunk_id="manifest",
                severity=Severity.ERROR,
                rule="invalid_manifest",
                message=f"manifest.json is invalid: {exc}",
            )
        )
    if manifest is not None:
        if manifest.chunk_size != project.config.chunk_size:
            report.findings.append(
                Finding(
                    chunk_id="manifest",
                    severity=Severity.WARN,
                    rule="manifest_chunk_size_drift",
                    message=(
                        f"manifest chunk_size {manifest.chunk_size} differs from the "
                        f"current config chunk_size {project.config.chunk_size}"
                    ),
                )
            )
        if manifest.record_id_scheme != RECORD_ID_SCHEME:
            report.findings.append(
                Finding(
                    chunk_id="manifest",
                    severity=Severity.ERROR,
                    rule="manifest_record_id_scheme_mismatch",
                    message=(
                        "manifest record_id_scheme "
                        f"{manifest.record_id_scheme!r} does not match the current "
                        f"supported scheme {RECORD_ID_SCHEME!r}"
                    ),
                )
            )
        for chunk in source_chunks.values():
            if chunk.chunk_size != manifest.chunk_size:
                report.findings.append(
                    Finding(
                        chunk_id=chunk.chunk_id,
                        severity=Severity.ERROR,
                        rule="chunk_manifest_chunk_size_mismatch",
                        message=(
                            f"chunk {chunk.chunk_id} has chunk_size "
                            f"{chunk.chunk_size}, but the manifest records "
                            f"{manifest.chunk_size}"
                        ),
                    )
                )
            if chunk.record_id_scheme != manifest.record_id_scheme:
                report.findings.append(
                    Finding(
                        chunk_id=chunk.chunk_id,
                        severity=Severity.ERROR,
                        rule="chunk_manifest_record_id_scheme_mismatch",
                        message=(
                            f"chunk {chunk.chunk_id} uses record_id_scheme "
                            f"{chunk.record_id_scheme!r}, but the manifest records "
                            f"{manifest.record_id_scheme!r}"
                        ),
                    )
                )
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
        include_inactive_versions=include_inactive_versions,
        all_versions_strict=all_versions_strict,
    )
    report.findings.extend(effective.findings)

    # Resolve task_id scope into chapter/record filters so a bounded task run
    # does not get blocked by unrelated chapters.
    resolved_chapter, resolved_record_ids = _resolve_validation_scope(
        project, chapter_id=chapter_id, record_ids=record_ids, task_id=task_id
    )
    scoped_chunks = _scoped_chunk_ids(
        project,
        source_chunks,
        chapter_id=resolved_chapter,
        record_ids=resolved_record_ids,
    )
    if new_epub_pipeline:
        from booktx.epub_preflight import validate_epub_inline_preflight

        report.findings.extend(
            epub_preflight_findings_as_validation_findings(
                validate_epub_inline_preflight(
                    project,
                    chapter_id=resolved_chapter,
                    record_ids=resolved_record_ids,
                    require_complete=require_complete,
                    source_chunks=source_chunks,
                    effective_chunks=effective.chunks,
                )
            )
        )

    _maybe_append_epub_toc_audit(
        report,
        project,
        new_epub_pipeline=new_epub_pipeline,
        resolved_chapter=resolved_chapter,
        task_id=task_id,
    )

    if context is not None:
        drift_finding = _context_render_drift_finding(project, context)
        if drift_finding is not None:
            report.findings.append(drift_finding)

    report.findings.extend(_epub_output_policy_findings(project))
    report.findings.extend(_soft_hyphen_findings(project, effective))

    # Scope record-level findings to the resolved chapter/task/record set so a
    # bounded run is not blocked by findings in unrelated chapters. Structural
    # findings (manifest/context/store/ledger, whose chunk_id is not a real
    # source chunk) always remain; only findings attached to a source chunk
    # outside the scope are dropped. EPUB preflight findings are already scoped
    # by chapter inside validate_epub_inline_preflight.
    if scoped_chunks is not None:
        report.findings = [
            finding
            for finding in report.findings
            if finding.chunk_id not in source_chunks
            or finding.chunk_id in scoped_chunks
        ]

    for chunk_id in sorted(source_chunks):
        if scoped_chunks is not None and chunk_id not in scoped_chunks:
            continue
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
    try:
        manifest = load_manifest(project)
    except Exception:  # noqa: BLE001
        return False
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
    from booktx.io_utils import write_json_text_atomic

    if project.reports_dir is None:
        raise ValueError("Reports directory is not configured.")
    project.reports_dir.mkdir(parents=True, exist_ok=True)
    out = project.reports_dir / "validation-report.json"
    write_json_text_atomic(
        out, json.dumps(report.as_dict(), indent=2, ensure_ascii=False)
    )
    return out


# Keep TOKEN_RE referenced for callers that want the raw regex.
_token_re = TOKEN_RE
_placeholder_cls = Placeholder  # re-export surface stability
