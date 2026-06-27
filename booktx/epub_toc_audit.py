"""EPUB visible-TOC chapter-map audit.

The chapter detector derives EPUB chapter boundaries from stored navigation
metadata and only falls back to XHTML headings when navigation yields nothing.
For real-world EPUBs a visible ``Contents`` page can promise more chapters than
navigation actually exposes (for example a preview/truncated EPUB, an extraction
that skipped spine documents, or partial navigation). This module audits the
visible table of contents against the extracted EPUB spans, the stored
navigation, and the current chapter map, and returns structured findings.

The audit is source-level state. It reads the shared
``.booktx/source-manifest.json`` template, the extracted chunk records, and the
shared ``.booktx/chapter-map.json``. It never mutates the chapter map.
"""

from __future__ import annotations

import json
import re
from html import unescape
from typing import TYPE_CHECKING, Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from booktx.config import Project
    from booktx.models import EpubSpanRef

__all__ = [
    "EpubTocEntry",
    "EpubTocAuditFinding",
    "EpubTocAuditResult",
    "audit_epub_chapter_map",
    "write_audit_report",
    "normalize_href",
    "extract_toc_entries",
    "chapter_ordinal",
    "toc_document_start_boundaries",
    "CHAPTER_AUDIT_REPORT_NAME",
]

#: Filename written under ``.booktx/reports/``.
CHAPTER_AUDIT_REPORT_NAME = "chapter-audit.json"

# XHTML heading tags that count as chapter-like boundary candidates.
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

# Anchor + href extraction. Anchors may use single, double, or unquoted hrefs.
_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_HREF_RE = re.compile(
    r"\bhref\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))", re.IGNORECASE
)

# ``chapter``/``part``/``book`` prefixes that wrap a bare ordinal.
_PREFIX_RE = re.compile(
    r"^(?:chapter|chapters|ch\.?|part|book|section)\s+(.+)$", re.IGNORECASE
)

_ROMAN_MAP = {
    "i": 1,
    "v": 5,
    "x": 10,
    "l": 50,
    "c": 100,
    "d": 500,
    "m": 1000,
}

_ONES = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_ONES_MAP = {word: value for value, word in enumerate(_ONES)}
_TENS_MAP = dict(_TENS)


class EpubTocEntry(BaseModel):
    """One visible contents-page link extracted from the EPUB source."""

    model_config = ConfigDict(extra="forbid")

    order: int
    title: str
    href: str
    source_record_id: str | None = None
    is_numbered_chapter: bool = False
    ordinal: int | None = None


class EpubTocAuditFinding(BaseModel):
    """One structured audit finding."""

    model_config = ConfigDict(extra="forbid")

    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    href: str | None = None
    title: str | None = None
    source_record_id: str | None = None


class EpubTocAuditResult(BaseModel):
    """Aggregated audit result for an EPUB project."""

    model_config = ConfigDict(extra="forbid")

    toc_entries: list[EpubTocEntry] = Field(default_factory=list)
    numbered_toc_count: int = 0
    mapped_numbered_chapter_count: int = 0
    missing_numbered_titles: list[str] = Field(default_factory=list)
    extracted_document_count: int = 0
    findings: list[EpubTocAuditFinding] = Field(default_factory=list)
    generated_at: str = ""

    @property
    def error_findings(self) -> list[EpubTocAuditFinding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warning_findings(self) -> list[EpubTocAuditFinding]:
        return [f for f in self.findings if f.severity == "warning"]

    def as_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


# --- href normalization -----------------------------------------------------


def normalize_href(href: str) -> str:
    """Normalize an EPUB anchor href for document comparison.

    Fragments are stripped, percent-encoding is decoded, and the document path
    is preserved. Basename-only matching is intentionally avoided so two
    documents that share a basename (``Text/ch1.xhtml`` vs ``Notes/ch1.xhtml``)
    are not collapsed.
    """
    if not href:
        return ""
    href = href.strip()
    if not href:
        return ""
    # Leave non-document schemes untouched; they are never chapter targets.
    lower = href.lower()
    if lower.startswith(("mailto:", "http://", "https://", "ftp://", "#")):
        if lower.startswith("#"):
            return ""
        return href
    parts = urlsplit(href)
    path = parts.path or href.split("#", 1)[0]
    path = unquote(path)
    return path.strip()


# --- TOC link extraction ----------------------------------------------------


def extract_toc_entries(text: str) -> list[tuple[str, str]]:
    """Return ``[(href, title), ...]`` for ``<a href>`` anchors in *text*.

    Titles are HTML-unescaped and stripped of nested tags so a TOC entry like
    ``<a href="chapter011.xhtml"><em>ELEVEN</em></a>`` yields ``("chapter011.xhtml",
    "ELEVEN")``. Order is preserved as found in the text.
    """
    entries: list[tuple[str, str]] = []
    if not text:
        return entries
    for anchor_match in _ANCHOR_RE.finditer(text):
        attrs = anchor_match.group(1) or ""
        body = anchor_match.group(2) or ""
        href_match = _HREF_RE.search(attrs)
        if not href_match:
            continue
        raw_href = next(group for group in href_match.groups() if group is not None)
        href = normalize_href(raw_href)
        if not href:
            continue
        title = unescape(re.sub(r"<[^>]+>", "", body)).strip()
        if not title:
            continue
        entries.append((href, title))
    return entries


# --- ordinal parsing --------------------------------------------------------


def _roman_to_int(text: str) -> int | None:
    cleaned = text.strip().lower()
    if not cleaned or len(cleaned) > 15:
        return None
    if any(ch not in _ROMAN_MAP for ch in cleaned):
        return None
    total = 0
    prev = 0
    for ch in reversed(cleaned):
        value = _ROMAN_MAP[ch]
        if value < prev:
            total -= value
        else:
            total += value
            prev = value
    return total if total > 0 else None


def _word_to_int(text: str) -> int | None:
    cleaned = text.lower().strip().replace("-", " ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    tokens = cleaned.split()
    if not tokens:
        return None
    total = 0
    for token in tokens:
        if token in _ONES_MAP:
            total += _ONES_MAP[token]
        elif token in _TENS:
            total += _TENS_MAP[token]
        elif token == "hundred":
            if total == 0:
                total = 100
            else:
                total *= 100
        else:
            return None
    return total or None


def chapter_ordinal(title: str) -> int | None:
    """Return the 1-based chapter ordinal for *title*, or ``None``.

    Recognizes arabic numerals (``12``), roman numerals (``XII``), word
    numerals (``TWELVE``, ``twenty-six``), and common prefixes such as
    ``Chapter 12`` or ``Part Two``. Non-chapter titles return ``None``.
    """
    if not title:
        return None
    core = title.strip().strip(":.()[]").strip()
    if not core:
        return None
    prefix_match = _PREFIX_RE.match(core)
    if prefix_match:
        core = prefix_match.group(1).strip()
    if not core:
        return None
    if core.isdigit():
        value = int(core)
        return value if value > 0 else None
    roman = _roman_to_int(core)
    if roman is not None:
        return roman
    return _word_to_int(core)


# --- boundary helpers (shared with chapter detection) -----------------------


def _span_document_href(span_ref: EpubSpanRef) -> str:
    return normalize_href(getattr(span_ref, "document_href", "") or "")


def toc_document_start_boundaries(
    span_refs: list[EpubSpanRef],
) -> list[tuple[int, str]]:
    """Return ``[(span_index, title), ...]`` TOC-derived chapter starts.

    Only numbered TOC entries whose target document has extracted spans produce
    a boundary. Boundaries are placed at the first span of the target document,
    deduplicated by ordinal, and ordered by span index. This is the last-resort
    boundary source used when navigation is partial and headings are absent.
    """
    if not span_refs:
        return []

    ordered = sorted(span_refs, key=lambda item: item.span_index)
    first_pos_by_href: dict[str, int] = {}
    for pos, span_ref in enumerate(ordered):
        href = _span_document_href(span_ref)
        if href and href not in first_pos_by_href:
            first_pos_by_href[href] = pos

    toc_links: list[tuple[int, str, str]] = []  # (order, href, title)
    seen: set[tuple[str, str]] = set()
    for span_ref in ordered:
        text = getattr(span_ref, "source_view_text", "") or getattr(
            span_ref, "source_text", ""
        )
        for href, title in extract_toc_entries(text):
            if not chapter_ordinal(title):
                continue
            key = (href, title.lower())
            if key in seen:
                continue
            seen.add(key)
            toc_links.append((len(toc_links), href, title))

    by_ordinal: dict[int, tuple[int, str]] = {}
    for _, href, title in toc_links:
        ordinal = chapter_ordinal(title)
        if ordinal is None:
            continue
        if href not in first_pos_by_href:
            continue
        if ordinal in by_ordinal:
            continue
        by_ordinal[ordinal] = (first_pos_by_href[href], title)

    return [
        (pos, title)
        for pos, title in sorted(by_ordinal.values(), key=lambda item: item[0])
    ]


# --- audit ------------------------------------------------------------------


def _load_template(project: Project):
    from booktx.config import load_manifest
    from booktx.epub_manifest import load_epub_template_from_manifest

    manifest = load_manifest(project)
    if manifest is None:
        return None
    try:
        return load_epub_template_from_manifest(manifest)
    except ValueError:
        return None


def _collect_toc_entries(project: Project, span_refs: list[EpubSpanRef]):
    """Return ordered, deduplicated TOC entries with optional record mapping."""
    from booktx.models import Chunk

    ordered: list[tuple[int, str, str]] = []  # (order, href, title)
    seen: set[tuple[str, str]] = set()
    for span_ref in sorted(span_refs, key=lambda item: item.span_index):
        text = getattr(span_ref, "source_view_text", "") or getattr(
            span_ref, "source_text", ""
        )
        for href, title in extract_toc_entries(text):
            key = (href, title.lower())
            if key in seen:
                continue
            seen.add(key)
            ordered.append((len(ordered), href, title))

    record_by_key: dict[tuple[str, str], str] = {}
    for chunk_path in sorted(project.chunks(), key=lambda path: path.stem):
        try:
            chunk = Chunk.model_validate_json(chunk_path.read_text("utf-8"))
        except Exception:  # noqa: BLE001 - audit must not crash on a bad chunk
            continue
        for record in chunk.records:
            for href, title in extract_toc_entries(record.source):
                key = (href, title.lower())
                record_by_key.setdefault(key, record.id)

    entries: list[EpubTocEntry] = []
    for order, href, title in ordered:
        ordinal = chapter_ordinal(title)
        entries.append(
            EpubTocEntry(
                order=order,
                title=title,
                href=href,
                source_record_id=record_by_key.get((href, title.lower())),
                is_numbered_chapter=ordinal is not None,
                ordinal=ordinal,
            )
        )
    return entries


def _missing_numbered_titles(
    toc_entries: list[EpubTocEntry], mapped_ordinals: set[int]
) -> list[str]:
    missing: list[str] = []
    for entry in toc_entries:
        if not entry.is_numbered_chapter or entry.ordinal is None:
            continue
        if entry.ordinal in mapped_ordinals:
            continue
        if entry.title in missing:
            continue
        missing.append(entry.title)
    return missing


def audit_epub_chapter_map(project: Project, *, chapter_map=None) -> EpubTocAuditResult:
    """Audit the visible EPUB TOC against extracted spans and the chapter map.

    ``chapter_map`` defaults to the on-disk chapter map. Pass a freshly detected
    map to audit the current source instead of the cached one. Non-EPUB projects
    or projects without a stored EPUB template return an empty result.
    """
    from datetime import datetime, timezone

    empty = EpubTocAuditResult(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    if project.config.format != "epub":
        return empty

    template = _load_template(project)
    if template is None:
        return empty
    span_refs = template.spans
    navigation_refs = template.navigation

    toc_entries = _collect_toc_entries(project, span_refs)
    extracted_hrefs = {
        _span_document_href(span_ref)
        for span_ref in span_refs
        if _span_document_href(span_ref)
    }

    if chapter_map is None:
        from booktx.chapters import load_chapter_map

        chapter_map = load_chapter_map(project)
    mapped_titles = [
        chapter.title for chapter in (chapter_map.chapters if chapter_map else [])
    ]
    mapped_ordinals = {
        ordinal
        for ordinal in (chapter_ordinal(title) for title in mapped_titles)
        if ordinal is not None
    }

    numbered_toc = [entry for entry in toc_entries if entry.is_numbered_chapter]
    numbered_toc_ordinals = {
        entry.ordinal for entry in numbered_toc if entry.ordinal is not None
    }
    missing_titles = _missing_numbered_titles(toc_entries, mapped_ordinals)

    heading_ordinals = {
        chapter_ordinal(span_ref.source_text)
        for span_ref in span_refs
        if getattr(span_ref, "tag_name", "") in _HEADING_TAGS
        and span_ref.source_text.strip()
    }
    heading_ordinals = {o for o in heading_ordinals if o is not None}

    findings: list[EpubTocAuditFinding] = []

    if numbered_toc and missing_titles:
        first = numbered_toc[0].title
        last = numbered_toc[-1].title
        preview = ", ".join(missing_titles[:12])
        suffix = "" if len(missing_titles) <= 12 else ", ..."
        findings.append(
            EpubTocAuditFinding(
                severity="warning",
                code="epub_toc_chapter_missing_from_map",
                message=(
                    f"contents page lists {len(numbered_toc)} numbered chapters "
                    f"({first}..{last}), but chapter-map has "
                    f"{len(mapped_ordinals)} numbered chapters. "
                    f"Missing: {preview}{suffix}."
                ),
            )
        )

    for entry in numbered_toc:
        if entry.ordinal in mapped_ordinals:
            continue
        has_extracted_spans = entry.href in extracted_hrefs
        if has_extracted_spans:
            findings.append(
                EpubTocAuditFinding(
                    severity="error",
                    code="epub_toc_href_extracted_but_unmapped",
                    message=(
                        f"contents link {entry.href} ({entry.title}) has "
                        f"extracted spans, but no chapter boundary covers it; "
                        f"translation workflow will skip this chapter."
                    ),
                    href=entry.href,
                    title=entry.title,
                    source_record_id=entry.source_record_id,
                )
            )
        else:
            findings.append(
                EpubTocAuditFinding(
                    severity="warning",
                    code="epub_toc_href_missing_from_extracted_spans",
                    message=(
                        f"contents link {entry.href} ({entry.title}) has no "
                        f"extracted span; source may be a preview/truncated "
                        f"EPUB or extraction skipped a spine document."
                    ),
                    href=entry.href,
                    title=entry.title,
                    source_record_id=entry.source_record_id,
                )
            )

    nav_ordinals = {
        chapter_ordinal(entry.title) for entry in navigation_refs if entry.title.strip()
    }
    nav_ordinals = {o for o in nav_ordinals if o is not None}
    signal_ordinals = heading_ordinals | numbered_toc_ordinals
    if nav_ordinals and signal_ordinals and nav_ordinals < signal_ordinals:
        extra = sorted(signal_ordinals - nav_ordinals)
        findings.append(
            EpubTocAuditFinding(
                severity="warning",
                code="epub_navigation_partial",
                message=(
                    "navigation provides fewer numbered chapters than visible "
                    f"chapter signals; navigation ordinals={sorted(nav_ordinals)}, "
                    f"extra signals={extra}."
                ),
            )
        )

    sorted_toc_ordinals = sorted(numbered_toc_ordinals)
    if len(sorted_toc_ordinals) >= 2:
        expected = set(range(sorted_toc_ordinals[0], sorted_toc_ordinals[-1] + 1))
        gaps = sorted(expected - numbered_toc_ordinals)
        if gaps:
            findings.append(
                EpubTocAuditFinding(
                    severity="warning",
                    code="epub_chapter_sequence_gap",
                    message=(
                        f"numbered TOC chapter sequence has gaps at ordinals {gaps}."
                    ),
                )
            )

    # Stable, deterministic ordering: errors first, then warnings, then info,
    # each subgroup preserved in discovery order.
    severity_rank = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: severity_rank[f.severity])

    return EpubTocAuditResult(
        toc_entries=toc_entries,
        numbered_toc_count=len(numbered_toc),
        mapped_numbered_chapter_count=len(mapped_ordinals),
        missing_numbered_titles=missing_titles,
        extracted_document_count=len(extracted_hrefs),
        findings=findings,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def write_audit_report(project: Project, result: EpubTocAuditResult):
    """Persist the audit result to ``.booktx/reports/chapter-audit.json``."""
    from booktx.io_utils import write_json_text_atomic

    reports_dir = project.booktx_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    out = reports_dir / CHAPTER_AUDIT_REPORT_NAME
    write_json_text_atomic(
        out, json.dumps(result.as_dict(), indent=2, ensure_ascii=False)
    )
    return out
