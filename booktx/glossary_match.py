"""Shared glossary matching for booktx.

One boundary-delimited matcher backs every glossary code path so a single
:class:`~booktx.context.GlossaryEntry` produces identical results whether it is
checked by ``booktx validate``, task submission validation, ``qa-scan``,
``context audit-term``, or fix-template record selection.

The matcher is deliberately boundary-aware: the fictional calendar term
``tenday`` matches ``a tenday later`` but not ``pretenday``, and the source term
``tenday`` does not match the plural token ``tendays`` (which is why plural
forms are modeled as explicit ``source_variants``). It does not turn a literal
phrase such as ``ten days`` into a match for ``tenday``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from booktx.context import GlossaryEntry


def _edge_prefix(term: str) -> str:
    """Left boundary: require a non-word char (or start) before an alnum edge."""
    return r"(?<!\w)" if term[0].isalnum() or term[0] == "_" else ""


def _edge_suffix(term: str) -> str:
    """Right boundary: require a non-word char (or end) after an alnum edge."""
    return r"(?!\w)" if term[-1].isalnum() or term[-1] == "_" else ""


def iter_term_matches(text: str, term: str, *, case_sensitive: bool) -> list[re.Match[str]]:
    """Return boundary-delimited matches for ``term`` in ``text``."""
    term = term.strip()
    if not term:
        return []
    pattern = f"{_edge_prefix(term)}{re.escape(term)}{_edge_suffix(term)}"
    flags = 0 if case_sensitive else re.IGNORECASE
    return list(re.finditer(pattern, text, flags))


def contains_term(text: str, term: str, *, case_sensitive: bool) -> bool:
    """Return True if ``term`` occurs in ``text`` as a boundary-delimited token."""
    return bool(iter_term_matches(text, term, case_sensitive=case_sensitive))


def _dedupe_terms(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        value = raw.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def source_terms(entry: GlossaryEntry) -> list[str]:
    """The source term plus any source variants, trimmed and deduplicated."""
    return _dedupe_terms([entry.source, *entry.source_variants])


def target_terms(entry: GlossaryEntry) -> list[str]:
    """The approved target plus any target variants, trimmed and deduplicated."""
    raw: list[str] = ([entry.target] if entry.target else []) + list(
        entry.target_variants
    )
    return _dedupe_terms(raw)


@dataclass(frozen=True, slots=True)
class TermSpan:
    entry_index: int
    term_index: int
    matched_term: str
    start: int
    end: int
    is_primary: bool
    shadowed: bool = False


def source_glossary_matches(source_text: str, glossary: list[GlossaryEntry]) -> list[TermSpan]:
    """Return source glossary spans with global longest-match suppression.

    All candidate primary/variant terms are discovered first.  Spans are then
    accepted in deterministic order: earlier start, longer span, primary before
    variant, then entry/term order. Fully contained later spans are returned as
    ``shadowed=True`` so callers can detect mixed short/long ambiguity.
    """
    candidates: list[TermSpan] = []
    for entry_index, entry in enumerate(glossary):
        for term_index, term in enumerate(source_terms(entry)):
            for match in iter_term_matches(source_text, term, case_sensitive=entry.case_sensitive):
                candidates.append(
                    TermSpan(
                        entry_index=entry_index,
                        term_index=term_index,
                        matched_term=match.group(0),
                        start=match.start(),
                        end=match.end(),
                        is_primary=term_index == 0,
                    )
                )
    ordered = sorted(
        candidates,
        key=lambda span: (
            span.start,
            -(span.end - span.start),
            0 if span.is_primary else 1,
            span.entry_index,
            span.term_index,
        ),
    )
    accepted: list[TermSpan] = []
    result: list[TermSpan] = []
    for span in ordered:
        contained = any(span.start >= a.start and span.end <= a.end for a in accepted)
        if contained:
            result.append(
                TermSpan(
                    span.entry_index, span.term_index, span.matched_term, span.start, span.end, span.is_primary, True
                )
            )
        else:
            accepted.append(span)
            result.append(span)
    return sorted(result, key=lambda span: (span.start, span.end, span.entry_index, span.term_index))


def applicable_entry_indexes(source_text: str, glossary: list[GlossaryEntry]) -> set[int]:
    return {span.entry_index for span in source_glossary_matches(source_text, glossary) if not span.shadowed}


def source_rule_applies(source_text: str, entry: GlossaryEntry) -> bool:
    """Compatibility wrapper for single-entry applicability checks."""
    return any(
        contains_term(source_text, term, case_sensitive=entry.case_sensitive)
        for term in source_terms(entry)
    )


def target_contains_approved(target_text: str, entry: GlossaryEntry) -> bool:
    """Return True if any approved target/variant occurs in the target text."""
    return any(
        contains_term(target_text, term, case_sensitive=entry.case_sensitive)
        for term in target_terms(entry)
    )


def entry_is_binding(entry: GlossaryEntry) -> bool:
    """Return True if the entry enforces a mandatory glossary decision.

    A glossary entry is binding only when it is enforced (``enforce != "off"``)
    and it carries either a required approved target or at least one forbidden
    target. Advisory approved-target notes (target set, no require/forbid) are
    not binding.
    """
    if entry.enforce == "off":
        return False
    return bool(entry.require_target or entry.forbidden_targets)


def mandatory_glossary_sha256(glossary: list[GlossaryEntry]) -> str:
    """Deterministic sha256 over only the binding glossary fields.

    Covers, for each binding entry (see :func:`entry_is_binding`), the source
    term and source variants, the approved target and target variants,
    ``require_target``, forbidden targets, case sensitivity, and the
    enforcement level. Non-binding advisory entries and chapter notes are
    excluded, so a chapter-note-only change does not alter the fingerprint.

    Binding entries are sorted deterministically before hashing.
    """
    payload: list[dict[str, object]] = []
    for entry in glossary:
        if not entry_is_binding(entry):
            continue
        payload.append(
            {
                "source": entry.source.strip(),
                "source_variants": _dedupe_terms(entry.source_variants),
                "target": (entry.target or "").strip(),
                "target_variants": _dedupe_terms(entry.target_variants),
                "require_target": bool(entry.require_target),
                "forbidden_targets": _dedupe_terms(entry.forbidden_targets),
                "case_sensitive": bool(entry.case_sensitive),
                "enforce": entry.enforce,
            }
        )
    payload.sort(key=lambda item: (item["source"], item["target"]))
    serialized = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def live_mandatory_glossary_sha256(project: object) -> str:
    """Hash of the project's *current* binding glossary fields.

    Used both at task/todo creation (to stamp a fingerprint) and at submission
    time (to detect that a mandatory glossary decision changed after the task
    was created).
    """
    from booktx.context import load_context

    ctx = load_context(project)  # type: ignore[arg-type]
    glossary = list(ctx.glossary) if ctx is not None else []
    return mandatory_glossary_sha256(glossary)
