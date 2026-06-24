"""Audit and safe migration helpers for EPUB inline XHTML targets."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from booktx.config import Project
from booktx.epub_inline_xhtml import (
    INLINE_XHTML_CODEC,
    inline_skeleton,
    sanitize_target_fragment,
    strip_inline_xhtml,
)
from booktx.io_utils import write_text_atomic
from booktx.progress import load_source_chunks
from booktx.validate import load_effective_translated_chunks, validate_record_pair


@dataclass(slots=True)
class InlineAuditResult:
    records_with_inline_source: int = 0
    valid_active_targets: int = 0
    missing_inline_tags: int = 0
    invalid_xhtml_targets: int = 0
    opaque_changed: int = 0
    needs_review: int = 0
    findings: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "records_with_inline_source": self.records_with_inline_source,
            "valid_active_targets": self.valid_active_targets,
            "missing_inline_tags": self.missing_inline_tags,
            "invalid_xhtml_targets": self.invalid_xhtml_targets,
            "opaque_changed": self.opaque_changed,
            "needs_review": self.needs_review,
            "findings": self.findings,
        }


def audit_inline_xhtml(project: Project) -> InlineAuditResult:
    chunks = load_source_chunks(project)
    effective = load_effective_translated_chunks(project)
    targets = {
        record.id: record
        for chunk in effective.chunks.values()
        for record in chunk.records
    }
    result = InlineAuditResult()
    for chunk in chunks:
        for source in chunk.records:
            if source.source_markup != INLINE_XHTML_CODEC:
                continue
            result.records_with_inline_source += 1
            target = targets.get(source.id)
            if target is None:
                result.needs_review += 1
                result.findings.append(
                    {"record_id": source.id, "rule": "missing_target"}
                )
                continue
            findings = validate_record_pair(source, target, chunk.chunk_id)
            errors = [finding for finding in findings if finding.severity == "error"]
            if not errors:
                result.valid_active_targets += 1
                continue
            result.needs_review += 1
            rules = {finding.rule for finding in errors}
            if "inline_xhtml_preserved" in rules:
                result.missing_inline_tags += 1
            if "inline_xhtml_parseable" in rules:
                result.invalid_xhtml_targets += 1
            if "inline_xhtml_opaque_preserved" in rules:
                result.opaque_changed += 1
            for finding in errors:
                result.findings.append(
                    {
                        "record_id": source.id,
                        "rule": finding.rule,
                        "message": finding.message,
                    }
                )
    return result


def _single_full_wrapper(source: str) -> tuple[str, tuple[tuple[str, str], ...]] | None:
    skeleton = inline_skeleton(source)
    if len(skeleton) != 2 or skeleton[0].kind != "start" or skeleton[1].kind != "end":
        return None
    if skeleton[0].tag != skeleton[1].tag:
        return None
    stripped = source.strip()
    if not stripped.startswith(f"<{skeleton[0].tag}") or not stripped.endswith(
        f"</{skeleton[0].tag}>"
    ):
        return None
    return skeleton[0].tag, skeleton[0].attrs


def _format_attrs(attrs: tuple[tuple[str, str], ...]) -> str:
    if not attrs:
        return ""
    return "".join(f' {name}="{value}"' for name, value in attrs)


def safe_migrated_target(source: str, target: str) -> str | None:
    wrapper = _single_full_wrapper(source)
    if wrapper is not None and "<" not in target and ">" not in target:
        tag, attrs = wrapper
        migrated = f"<{tag}{_format_attrs(attrs)}>{target}</{tag}>"
        if not [
            issue
            for issue in sanitize_target_fragment(migrated, source).issues
            if issue.severity == "error"
        ]:
            return migrated
    skeleton = inline_skeleton(source)
    if len(skeleton) == 2 and skeleton[0].kind == "start" and skeleton[1].kind == "end":
        tag, attrs = skeleton[0].tag, skeleton[0].attrs
        start = source.find(f"<{tag}")
        if start >= 0:
            start = source.find(">", start) + 1
            end = source.find(f"</{tag}>", start)
            phrase = (
                strip_inline_xhtml(source[start:end]).strip() if end >= start else ""
            )
            if phrase and target.count(phrase) == 1:
                migrated = target.replace(
                    phrase, f"<{tag}{_format_attrs(attrs)}>{phrase}</{tag}>"
                )
                if not [
                    issue
                    for issue in sanitize_target_fragment(migrated, source).issues
                    if issue.severity == "error"
                ]:
                    return migrated
    return None


def migrate_inline_xhtml(
    project: Project, *, write_safe: bool = False
) -> dict[str, object]:
    chunks = load_source_chunks(project)
    effective = load_effective_translated_chunks(project)
    translated_by_chunk = {
        chunk_id: chunk.model_copy(deep=True)
        for chunk_id, chunk in effective.chunks.items()
    }
    mapped: list[dict[str, str]] = []
    review: list[dict[str, str]] = []
    for chunk in chunks:
        translated = translated_by_chunk.get(chunk.chunk_id)
        if translated is None:
            continue
        by_id = {record.id: record for record in translated.records}
        for source in chunk.records:
            if source.source_markup != INLINE_XHTML_CODEC:
                continue
            target = by_id.get(source.id)
            if target is None:
                continue
            if not [
                issue
                for issue in sanitize_target_fragment(
                    target.target, source.source
                ).issues
                if issue.severity == "error"
            ]:
                continue
            migrated = safe_migrated_target(source.source, target.target)
            if migrated is None:
                review.append({"record_id": source.id, "reason": "unsafe_or_ambiguous"})
                continue
            mapped.append(
                {
                    "record_id": source.id,
                    "old_target": target.target,
                    "new_target": migrated,
                }
            )
            if write_safe:
                target.target = migrated
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "timestamp": timestamp,
        "mapped_records": mapped,
        "targets_requiring_review": review,
        "written": write_safe,
    }
    reports_dir = (
        project.profile_dir / "reports"
        if project.profile_dir is not None
        else project.booktx_dir / "reports"
    )
    reports_dir.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        reports_dir / f"inline-xhtml-migration-{timestamp}.json",
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
    )
    if write_safe and project.translated_dir is not None:
        if project.profile_dir is not None:
            backup = reports_dir / (
                f"translation-store.before-inline-xhtml-{timestamp}.json"
            )
            store_path = project.profile_dir / "translation-store.json"
            if store_path.is_file():
                backup.write_text(store_path.read_text("utf-8"), "utf-8")
        for chunk_id, translated in translated_by_chunk.items():
            path = project.translated_dir / f"{chunk_id}.json"
            if path.exists():
                write_text_atomic(path, translated.model_dump_json(indent=2) + "\n")
    return report
