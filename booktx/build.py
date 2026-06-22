"""Rebuild the final translated document from validated translated chunks."""

from __future__ import annotations

import re
from pathlib import Path

from booktx.chunking import ProseSpan
from booktx.config import Project, find_source_file, load_manifest
from booktx.epub_manifest import assert_source_sha, load_epub_template_from_manifest
from booktx.markdown_io import build_markdown, extract_markdown
from booktx.models import Chunk, EpubSpanRef, TranslatedChunk
from booktx.placeholders import restore

__all__ = [
    "BuildResult",
    "BuildError",
    "build_project",
    "records_to_span_text",
]


class BuildError(Exception):
    """User-facing error during build."""


def _load_chunks(project: Project) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in project.chunks():
        chunks.append(Chunk.model_validate_json(path.read_text("utf-8")))
    chunks.sort(key=lambda chunk: chunk.chunk_id)
    return chunks


def _load_translated(project: Project) -> dict[str, TranslatedChunk]:
    out: dict[str, TranslatedChunk] = {}
    for path in project.translated():
        try:
            translated_chunk = TranslatedChunk.model_validate_json(
                path.read_text("utf-8")
            )
        except Exception as exc:  # noqa: BLE001
            raise BuildError(
                f"translated file {path.name} is not valid: {exc}"
            ) from exc
        out[translated_chunk.chunk_id] = translated_chunk
    return out


def records_to_span_text(span: ProseSpan, targets: list[str]) -> str:
    """Join translated record targets back into one span string."""
    joined = " ".join(target.strip() for target in targets if target and target.strip())
    return restore(joined, span.placeholders)


def _build_target_stream(
    chunks: list[Chunk], translated: dict[str, TranslatedChunk]
) -> list[str]:
    target_stream: list[str] = []
    for chunk in chunks:
        translated_chunk = translated.get(chunk.chunk_id)
        if translated_chunk is None:
            target_stream.extend(record.source for record in chunk.records)
            continue
        by_id = {record.id: record for record in translated_chunk.records}
        for record in chunk.records:
            translated_record = by_id.get(record.id)
            target_stream.append(
                translated_record.target if translated_record else record.source
            )
    return target_stream


def _prose_span_from_ref(span_ref: EpubSpanRef) -> ProseSpan:
    return ProseSpan(
        text=span_ref.source_text,
        placeholders=span_ref.placeholders,
        protected_terms=span_ref.protected_terms,
    )


def _build_markdown(project: Project) -> BuildResult:
    source = find_source_file(project)
    text = source.read_text("utf-8")
    names = _load_names(project)
    extraction = extract_markdown(text, protected_terms=names)

    spans = extraction.spans
    span_texts: list[str | None] = [None] * len(spans)
    chunks = _load_chunks(project)
    translated = _load_translated(project)

    from booktx.chunking import segment_spans

    seg_counts = [
        len(segment_spans([span], language=project.config.source_language))
        for span in spans
    ]
    target_stream = _build_target_stream(chunks, translated)

    pos = 0
    for idx, span in enumerate(spans):
        count = seg_counts[idx]
        chunk_targets = target_stream[pos : pos + count]
        pos += count
        span_texts[idx] = records_to_span_text(span, chunk_targets)

    replacements = [text or "" for text in span_texts]
    output = build_markdown(extraction.template, replacements)

    out_path = _output_path(project, source, suffix=".md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(output, encoding="utf-8")
    return BuildResult(output_path=out_path, format="markdown", span_count=len(spans))


def _build_epub(project: Project) -> BuildResult:
    from text2epub import Replacement, ReplacementPlan, rebuild_epub
    from text2epub.errors import ReplacementError, ValidationError
    from text2epub.validation import scan_epub_for_unresolved_tokens

    source = find_source_file(project)
    manifest = load_manifest(project)
    if manifest is None:
        raise BuildError(
            "EPUB extraction manifest is missing. Run `booktx extract` first."
        )

    try:
        epub_template = load_epub_template_from_manifest(manifest)
        assert_source_sha(source, manifest.source.sha256)
        assert_source_sha(
            source,
            str(epub_template.text2epub_manifest.get("source_sha256", "")),
        )
    except ValueError as exc:
        raise BuildError(str(exc)) from exc

    chunks = _load_chunks(project)
    translated = _load_translated(project)
    target_stream = _build_target_stream(chunks, translated)

    from booktx.chunking import segment_spans

    replacements: list[Replacement] = []
    pos = 0
    for span_ref in epub_template.spans:
        span = _prose_span_from_ref(span_ref)
        count = len(segment_spans([span], language=manifest.source.source_language))
        chunk_targets = target_stream[pos : pos + count]
        if len(chunk_targets) != count:
            raise BuildError(
                "Stored EPUB spans no longer align with the extracted chunk stream. "
                "Re-run `booktx extract`."
            )
        pos += count
        replacements.append(
            Replacement(
                block_id=span_ref.block_id,
                text=records_to_span_text(span, chunk_targets),
                expected_source=span_ref.source_text,
                allow_inline_xhtml=False,
            )
        )

    if pos != len(target_stream):
        raise BuildError(
            "Chunk records do not align with the stored EPUB span order. "
            "Re-run `booktx extract`."
        )

    out_path = _output_path(project, source, suffix=".epub")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        report = rebuild_epub(
            ReplacementPlan(
                source_epub=source,
                extraction_manifest=epub_template.text2epub_manifest,
                replacements=replacements,
            ),
            out_path,
        )
    except ValidationError as exc:
        match = re.search(r"token '([^']+)'.*entry '([^']+)'", str(exc))
        if match is not None:
            token, entry_name = match.groups()
            raise BuildError(
                f"built EPUB contains unresolved placeholder {token} in {entry_name}"
            ) from exc
        raise BuildError(str(exc)) from exc
    except ReplacementError as exc:
        raise BuildError(str(exc)) from exc

    findings = scan_epub_for_unresolved_tokens(out_path)
    if findings:
        entry_name, token = findings[0]
        raise BuildError(
            f"built EPUB contains unresolved placeholder {token} in {entry_name}"
        )

    return BuildResult(
        output_path=out_path,
        format="epub",
        span_count=len(epub_template.spans),
        report={
            "changed_entries": report.changed_entries,
            "replacement_count": report.replacement_count,
            "unresolved_token_count": report.unresolved_token_count,
        },
    )


def _load_names(project: Project) -> list[str]:
    from booktx.config import load_names

    return load_names(project).protected_terms


def _output_path(project: Project, source: Path, *, suffix: str) -> Path:
    stem = source.stem
    target = project.config.target_language
    return project.output_dir / f"{stem}.{target}{suffix}"


class BuildResult:
    """Outcome of a build run."""

    def __init__(
        self,
        *,
        output_path: Path,
        format: str,
        span_count: int,
        report: dict[str, object] | None = None,
    ) -> None:
        self.output_path = output_path
        self.format = format
        self.span_count = span_count
        self.report = report or {}

    def as_dict(self) -> dict[str, object]:
        return {
            "output_path": str(self.output_path),
            "format": self.format,
            "span_count": self.span_count,
            "report": self.report,
        }


def build_project(project: Project) -> BuildResult:
    """Build the translated output document for ``project``."""
    source = find_source_file(project)
    if project.config.format == "markdown" or source.suffix.lower() in (
        ".md",
        ".markdown",
    ):
        return _build_markdown(project)
    if project.config.format == "epub" or source.suffix.lower() == ".epub":
        return _build_epub(project)
    raise BuildError(
        f"Cannot build format {project.config.format!r}; "
        "booktx v1 supports only markdown and epub."
    )
