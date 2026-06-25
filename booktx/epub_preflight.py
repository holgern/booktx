"""Build-grade EPUB inline-XHTML preflight shared by validate, check, and build.

``booktx build`` validates the *joined* per-span target against the EPUB span
manifest (the authority for inline XHTML), so it can catch inline-skeleton
mismatches that record-level validation misses. This module exposes that same
span assembly and sanitization as a reusable preflight so ``validate``/``check``
report the same failures *before* build, and so ``translate insert`` can reject
a bad target before it is written.

The assembly semantics (record-to-span windowing, ``records_to_span_text`` join,
``source_view`` fallback, records-unchanged short-circuit) mirror
:func:`booktx.build._build_epub` exactly. Build keeps its raise-on-first-error
behavior but routes through :func:`assemble_epub_replacements` so the two paths
cannot drift.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from booktx.epub_inline_xhtml import (
    INLINE_XHTML_CODEC,
    FragmentValidationIssue,
    InlineSkeletonToken,
    inline_skeleton,
    sanitize_target_fragment,
)

if TYPE_CHECKING:
    from booktx.config import Project
    from booktx.models import (
        Chunk,
        EpubSpanRef,
        EpubTemplateData,
        Record,
        TranslatedChunk,
    )

__all__ = [
    "AssembledSpan",
    "EpubPreflightFinding",
    "assemble_epub_replacements",
    "validate_epub_inline_preflight",
]


@dataclass(slots=True)
class EpubPreflightFinding:
    """One EPUB inline-XHTML preflight finding with full location."""

    severity: str
    rule: str
    message: str
    chapter_id: str = ""
    chapter_title: str = ""
    chunk_id: str = ""
    record_id: str = ""
    record_ids: list[str] = field(default_factory=list)
    span_index: int | None = None
    block_id: str = ""
    document_href: str = ""
    source: str = ""
    target: str = ""
    source_inline_skeleton: list[dict[str, object]] = field(default_factory=list)
    target_inline_skeleton: list[dict[str, object]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "severity": self.severity,
            "rule": self.rule,
            "message": self.message,
            "chapter_id": self.chapter_id,
            "chapter_title": self.chapter_title,
            "chunk_id": self.chunk_id,
            "record_id": self.record_id,
            "record_ids": list(self.record_ids),
            "span_index": self.span_index,
            "block_id": self.block_id,
            "document_href": self.document_href,
            "source": self.source,
            "target": self.target,
            "source_inline_skeleton": list(self.source_inline_skeleton),
            "target_inline_skeleton": list(self.target_inline_skeleton),
        }


@dataclass(slots=True)
class AssembledSpan:
    """The assembled replacement data for one EPUB span.

    Mirrors what :func:`booktx.build._build_epub` computes per span, so build
    and preflight share one assembly path.
    """

    span_ref: EpubSpanRef
    records: list[Record]
    joined_source: str
    joined_target: str
    replacement_text: str
    allow_inline_xhtml: bool
    chunk_id: str
    chapter_id: str
    chapter_title: str
    sanitized_issues: list[FragmentValidationIssue] = field(default_factory=list)
    sanitized_xhtml: str = ""


def _skeleton_dicts(tokens: list[InlineSkeletonToken]) -> list[dict[str, object]]:
    return [
        {
            "kind": token.kind,
            "tag": token.tag,
            "attrs": list(token.attrs),
            "opaque": token.opaque,
        }
        for token in tokens
    ]


def _record_chapter_index(
    project: Project,
    flat_records: list[Record],
) -> tuple[dict[str, str], dict[str, str]]:
    """Return (record_id -> chapter_id, chapter_id -> title) from the chapter map.

    Falls back to empty mappings when no chapter map is available, so the
    preflight still runs (without chapter scoping/location) for degenerate
    projects.
    """
    from booktx.chapters import load_chapter_map

    chapter_id_by_record: dict[str, str] = {}
    title_by_chapter: dict[str, str] = {}
    try:
        chapter_map = load_chapter_map(project)
    except Exception:  # noqa: BLE001 - preflight must not crash on a bad map
        chapter_map = None
    if chapter_map is None:
        return chapter_id_by_record, title_by_chapter
    ordered = [record.id for record in flat_records]
    index_by_id = {record_id: idx for idx, record_id in enumerate(ordered)}
    for chapter in chapter_map.chapters:
        title_by_chapter[chapter.chapter_id] = chapter.title
        start = index_by_id.get(chapter.start_record_id)
        end = index_by_id.get(chapter.end_record_id)
        if start is None or end is None or end < start:
            continue
        for record_id in ordered[start : end + 1]:
            chapter_id_by_record[record_id] = chapter.chapter_id
    return chapter_id_by_record, title_by_chapter


def _load_span_inputs(
    project: Project,
    *,
    source_chunks: dict[str, Chunk] | None,
    effective_chunks: dict[str, TranslatedChunk] | None,
    require_complete: bool,
) -> (
    tuple[
        list[Record],
        dict[str, TranslatedChunk],
        EpubTemplateData,
        dict[str, str],
        dict[str, str],
    ]
    | None
):
    """Load the chunks, effective translations, template, and chapter index."""
    from booktx.build import _load_chunks
    from booktx.epub_manifest import load_epub_template_from_manifest
    from booktx.validate import load_effective_translated_chunks

    if source_chunks is None or effective_chunks is None:
        effective = load_effective_translated_chunks(
            project, source_chunks=source_chunks
        )
        if effective_chunks is None:
            effective_chunks = effective.chunks
        if source_chunks is None:
            source_chunks = {chunk.chunk_id: chunk for chunk in _load_chunks(project)}

    chunks_sorted = sorted(source_chunks.values(), key=lambda c: c.chunk_id)
    flat_records = [record for chunk in chunks_sorted for record in chunk.records]
    chapter_id_by_record, title_by_chapter = _record_chapter_index(
        project, flat_records
    )
    from booktx.config import load_manifest

    manifest = load_manifest(project)
    if manifest is None:
        return None
    template = load_epub_template_from_manifest(manifest)
    return (
        flat_records,
        effective_chunks,
        template,
        chapter_id_by_record,
        title_by_chapter,
    )


def assemble_epub_replacements(
    project: Project,
    *,
    source_chunks: dict[str, Chunk] | None = None,
    effective_chunks: dict[str, TranslatedChunk] | None = None,
    require_complete: bool = False,
) -> list[AssembledSpan]:
    """Assemble the per-span replacement data exactly like ``_build_epub``.

    Build uses this to produce ``Replacement`` objects; preflight uses this to
    know which spans need inline-XHTML sanitization. ``require_complete=True``
    mirrors build's behavior of erroring when a record is untranslated; when
    ``False`` (the validate default), the source text is used as the target for
    untranslated records.
    """
    from booktx.build import BuildError, records_to_span_text
    from booktx.chunking import ProseSpan

    inputs = _load_span_inputs(
        project,
        source_chunks=source_chunks,
        effective_chunks=effective_chunks,
        require_complete=require_complete,
    )
    if inputs is None:
        return []
    flat_records, effective_chunks, template, chapter_id_by_record, title_by_chapter = (
        inputs
    )
    translated_by_id = {
        record.id: record
        for chunk in effective_chunks.values()
        for record in chunk.records
    }

    assembled: list[AssembledSpan] = []
    for idx, span_ref in enumerate(template.spans):
        next_span_index = (
            template.spans[idx + 1].span_index
            if idx + 1 < len(template.spans)
            else None
        )
        source_records = [
            record
            for record in flat_records
            if record.span_index is not None
            and record.span_index >= span_ref.span_index
            and (next_span_index is None or record.span_index < next_span_index)
        ]
        if not source_records:
            raise BuildError(
                "Stored EPUB spans no longer align with the extracted chunk stream. "
                "Re-run `booktx extract`."
            )
        chunk_targets: list[str] = []
        source_fragments: list[str] = []
        for record in source_records:
            translated_record = translated_by_id.get(record.id)
            if translated_record is None and require_complete:
                raise BuildError("build requires complete translations")
            chunk_targets.append(
                translated_record.target if translated_record else record.source
            )
            source_fragments.append(record.source)
        span = ProseSpan(
            text=" ".join(source_fragments),
            placeholders=[p for record in source_records for p in record.placeholders],
            protected_terms=[
                term for record in source_records for term in record.protected_terms
            ],
        )
        joined_target = records_to_span_text(span, chunk_targets)
        joined_source = records_to_span_text(span, source_fragments)
        source_view = span_ref.source_view_text or span_ref.source_text
        replacement_text = joined_target
        allow_inline_xhtml = False
        records_unchanged = all(
            target == source
            for target, source in zip(chunk_targets, source_fragments, strict=True)
        )
        sanitized_issues: list[FragmentValidationIssue] = []
        sanitized_xhtml = ""
        if records_unchanged:
            replacement_text = span_ref.source_text
        elif joined_target == source_view:
            replacement_text = span_ref.source_text
        elif span_ref.source_markup == INLINE_XHTML_CODEC:
            sanitized = sanitize_target_fragment(joined_target, joined_source)
            sanitized_issues = sanitized.issues
            sanitized_xhtml = sanitized.xhtml
            errors = [issue for issue in sanitized.issues if issue.severity == "error"]
            if not errors:
                replacement_text = sanitized.xhtml
                allow_inline_xhtml = True

        chunk_id = source_records[0].id.split("-", 1)[0] if source_records else ""
        first_record = source_records[0] if source_records else None
        span_chapter = (
            chapter_id_by_record.get(first_record.id, "") if first_record else ""
        )
        span_chapter_title = title_by_chapter.get(span_chapter, "")
        assembled.append(
            AssembledSpan(
                span_ref=span_ref,
                records=source_records,
                joined_source=joined_source,
                joined_target=joined_target,
                replacement_text=replacement_text,
                allow_inline_xhtml=allow_inline_xhtml,
                chunk_id=chunk_id,
                chapter_id=span_chapter,
                chapter_title=span_chapter_title,
                sanitized_issues=sanitized_issues,
                sanitized_xhtml=sanitized_xhtml,
            )
        )
    return assembled


def _attribute_span_mismatch_record(
    records: list[Record],
    translated_by_id: Mapping[str, Any],
) -> tuple[str, list[str]]:
    """Pick the most likely offending record for a span-level skeleton mismatch.

    Compares each record's per-record inline skeleton (source vs target). The
    first record whose target skeleton differs from its source skeleton is the
    likely culprit. When no single record is identifiable, return all involved
    record ids so the finding lists them.
    """
    record_ids = [record.id for record in records]
    for record in records:
        translated = translated_by_id.get(record.id)
        if translated is None:
            continue
        target = getattr(translated, "target", None)
        if target is None:
            continue
        source_skeleton = inline_skeleton(record.source)
        target_skeleton = inline_skeleton(target)
        if source_skeleton != target_skeleton:
            return record.id, record_ids
    return "", record_ids


def validate_epub_inline_preflight(
    project: Project,
    *,
    chapter_id: str | None = None,
    record_ids: set[str] | None = None,
    require_complete: bool = False,
    source_chunks: dict[str, Chunk] | None = None,
    effective_chunks: dict[str, TranslatedChunk] | None = None,
) -> list[EpubPreflightFinding]:
    """Run the build-grade EPUB inline-XHTML preflight and return findings.

    ``chapter_id`` restricts to spans touching that chapter; ``record_ids``
    restricts to spans touching any of those records. Either filter keeps
    unrelated chapters from blocking a bounded/scoped run. When neither is set,
    every span is checked (whole-project preflight).

    Findings carry exact record/span/chapter/block/href location. For a
    span-level skeleton mismatch, the most likely record is attributed; if none
    is identifiable, all involved record ids are listed.
    """
    record_id_filter = record_ids if record_ids is not None else None
    assembled_spans = assemble_epub_replacements(
        project,
        source_chunks=source_chunks,
        effective_chunks=effective_chunks,
        require_complete=require_complete,
    )
    # Re-derive the effective translation index for record attribution.
    inputs = _load_span_inputs(
        project,
        source_chunks=source_chunks,
        effective_chunks=effective_chunks,
        require_complete=require_complete,
    )
    translated_by_id: Mapping[str, Any] = {}
    if inputs is not None:
        _, effective_chunks_loaded, _, _, _ = inputs
        translated_by_id = {
            record.id: record
            for chunk in effective_chunks_loaded.values()
            for record in chunk.records
        }

    findings: list[EpubPreflightFinding] = []
    for span in assembled_spans:
        span_ref = span.span_ref
        span_records = span.records
        span_record_ids = {record.id for record in span_records}
        # Chapter scope: the assembled span already carries its chapter id.
        if chapter_id is not None and chapter_id != span.chapter_id:
            continue
        if record_id_filter is not None and not span_record_ids.intersection(
            record_id_filter
        ):
            continue

        # require_complete handling for untranslated records in this span.
        if require_complete:
            for record in span_records:
                if record.id not in translated_by_id:
                    findings.append(
                        EpubPreflightFinding(
                            severity="error",
                            rule="missing_translation",
                            message=f"record {record.id} has no accepted translation",
                            chunk_id=record.id.split("-", 1)[0],
                            record_id=record.id,
                            record_ids=[record.id],
                            span_index=span_ref.span_index,
                            block_id=span_ref.block_id,
                            document_href=span_ref.document_href,
                            chapter_id=span.chapter_id,
                            chapter_title=span.chapter_title,
                        )
                    )

        offender_id, involved_ids = _attribute_span_mismatch_record(
            span_records, translated_by_id
        )
        for issue in span.sanitized_issues:
            if issue.severity == "warn" and require_complete:
                continue
            findings.append(
                EpubPreflightFinding(
                    severity=issue.severity,
                    rule=issue.rule,
                    message=issue.message,
                    chapter_id=span.chapter_id,
                    chapter_title=span.chapter_title,
                    chunk_id=span.chunk_id,
                    record_id=offender_id,
                    record_ids=involved_ids,
                    span_index=span_ref.span_index,
                    block_id=span_ref.block_id,
                    document_href=span_ref.document_href,
                    source=span.joined_source,
                    target=span.joined_target,
                    source_inline_skeleton=_skeleton_dicts(
                        inline_skeleton(span.joined_source)
                    ),
                    target_inline_skeleton=_skeleton_dicts(
                        inline_skeleton(span.joined_target)
                    ),
                )
            )
    return findings
