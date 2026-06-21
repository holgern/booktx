"""Rebuild the final translated document from validated translated chunks.

build reads the source document via the format-specific extractor (markdown or
epub), reads the agent-translated chunks in order, maps each translated record
back onto its parent prose span, restores protected names and inline tags, and
writes the output file to ``output/<stem>.<target>.<ext>``.

build never invents translation text. If a chunk lacks a validated translation
file, the corresponding records are skipped and their spans keep their source
text (with placeholders restored) so the output is still well-formed; the
caller is expected to run :func:`spinetx.validate.validate_project` first.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from spinetx.chunking import ProseSpan
from spinetx.config import Project, find_source_file
from spinetx.epub_io import EpubExtraction, build_epub, extract_epub
from spinetx.markdown_io import build_markdown, extract_markdown
from spinetx.models import Chunk, TranslatedChunk
from spinetx.placeholders import restore

__all__ = [
    "BuildResult",
    "build_project",
    "records_to_span_text",
]

_UNRESOLVED_TOKEN_RE = re.compile(rb"__(?:TAG|NAME)_\d+__|__SPANTX_\d+__")


class BuildError(Exception):
    """User-facing error during build."""


def _load_chunks(project: Project) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in project.chunks():
        chunks.append(Chunk.model_validate_json(path.read_text("utf-8")))
    chunks.sort(key=lambda c: c.chunk_id)
    return chunks


def _load_translated(project: Project) -> dict[str, TranslatedChunk]:
    out: dict[str, TranslatedChunk] = {}
    for path in project.translated():
        try:
            tc = TranslatedChunk.model_validate_json(path.read_text("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise BuildError(
                f"translated file {path.name} is not valid: {exc}"
            ) from exc
        out[tc.chunk_id] = tc
    return out


def _assert_no_unresolved_tokens_in_epub(path: Path) -> None:
    """Raise BuildError if generated EPUB XHTML/HTML still has internal tokens."""
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if not name.lower().endswith((".xhtml", ".html")):
                continue
            data = zf.read(name)
            match = _UNRESOLVED_TOKEN_RE.search(data)
            if match is None:
                continue
            token = match.group(0).decode("ascii", "replace")
            raise BuildError(
                f"built EPUB contains unresolved placeholder {token} in {name}"
            )


def records_to_span_text(span: ProseSpan, targets: list[str]) -> str:
    """Join translated record targets back into one span string.

    ``targets`` is the per-record translated text for the records derived from
    ``span``, in order. The original span was segmented into sentences; rebuild
    joins them with a single space, then restores placeholders.
    """
    joined = " ".join(t.strip() for t in targets if t and t.strip())
    return restore(joined, span.placeholders)


def _build_markdown(project: Project) -> BuildResult:
    source = find_source_file(project)
    text = source.read_text("utf-8")
    names = _load_names(project)
    extraction = extract_markdown(text, protected_terms=names)

    spans = extraction.spans
    span_texts: list[str | None] = [None] * len(spans)
    chunks = _load_chunks(project)
    translated = _load_translated(project)

    # Map chunk records back onto spans in order. Records were produced by
    # segmenting spans in order, so re-segment each span to know how many
    # records it produced, then consume that many targets per span.
    from spinetx.chunking import segment_spans

    seg_counts = [
        len(segment_spans([span], language=project.config.source_language))
        for span in spans
    ]

    target_stream: list[str] = []
    for chunk in chunks:
        tc = translated.get(chunk.chunk_id)
        if tc is None:
            # No translation: fall back to source text for these records.
            for rec in chunk.records:
                target_stream.append(rec.source)
            continue
        by_id = {r.id: r for r in tc.records}
        for rec in chunk.records:
            trec = by_id.get(rec.id)
            target_stream.append(trec.target if trec else rec.source)

    # Walk spans, consuming seg_counts[i] targets each.
    pos = 0
    for idx, span in enumerate(spans):
        count = seg_counts[idx]
        chunk_targets = target_stream[pos : pos + count]
        pos += count
        span_texts[idx] = records_to_span_text(span, chunk_targets)

    replacements = [t or "" for t in span_texts]
    output = build_markdown(extraction.template, replacements)

    out_path = _output_path(project, source, suffix=".md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(output, encoding="utf-8")
    return BuildResult(output_path=out_path, format="markdown", span_count=len(spans))


def _build_epub(project: Project) -> BuildResult:
    source = find_source_file(project)
    names = _load_names(project)
    extraction: EpubExtraction = extract_epub(str(source), protected_terms=names)

    spans = extraction.spans
    chunks = _load_chunks(project)
    translated = _load_translated(project)

    # Build the ordered target stream (same logic as markdown).
    target_stream: list[str] = []
    for chunk in chunks:
        tc = translated.get(chunk.chunk_id)
        if tc is None:
            target_stream.extend(rec.source for rec in chunk.records)
            continue
        by_id = {r.id: r for r in tc.records}
        for rec in chunk.records:
            trec = by_id.get(rec.id)
            target_stream.append(trec.target if trec else rec.source)

    # Consume per-span target counts.
    from spinetx.chunking import segment_spans

    replacements: list[str] = []
    pos = 0
    for span in spans:
        count = len(segment_spans([span], language=project.config.source_language))
        chunk_targets = target_stream[pos : pos + count]
        pos += count
        replacements.append(records_to_span_text(span, chunk_targets))

    out_path = _output_path(project, source, suffix=".epub")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    build_epub(str(source), str(out_path), extraction, replacements)
    _assert_no_unresolved_tokens_in_epub(out_path)
    return BuildResult(output_path=out_path, format="epub", span_count=len(spans))


def _load_names(project: Project) -> list[str]:
    from spinetx.config import load_names

    return load_names(project).protected_terms


def _output_path(project: Project, source: Path, *, suffix: str) -> Path:
    stem = source.stem
    target = project.config.target_language
    return project.output_dir / f"{stem}.{target}{suffix}"


class BuildResult:
    """Outcome of a build run."""

    def __init__(self, *, output_path: Path, format: str, span_count: int) -> None:
        self.output_path = output_path
        self.format = format
        self.span_count = span_count

    def as_dict(self) -> dict[str, object]:
        return {
            "output_path": str(self.output_path),
            "format": self.format,
            "span_count": self.span_count,
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
        "spinetx v1 supports only markdown and epub."
    )
