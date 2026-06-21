"""Tests for spinetx.build: rebuild markdown and epub from translated chunks.

These are integration tests: they drive extract -> write fake translation ->
build end to end via the public chunk writers used by the CLI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spinetx.build import BuildError, build_project, records_to_span_text
from spinetx.chunking import ProseSpan, spans_to_chunks
from spinetx.config import find_source_file, init_project, load_project
from spinetx.html_io import build_xhtml  # noqa: F401  (sanity import)
from spinetx.markdown_io import extract_markdown
from spinetx.models import Chunk

MARKDOWN_DOC = """\
---
title: Demo Book
---

# Chapter One

Alice met Bob on Baker Street. They were happy.

Run `print(1)` and visit [the docs](https://example.com) soon.

```python
print("never translated")
```
"""


def _write_source_chunks_markdown(proj, doc: str = MARKDOWN_DOC) -> list[Chunk]:
    """Extract markdown, segment, write source chunk files; return Chunk list."""
    names = json.loads(proj.names_path.read_text("utf-8")).get("protected_terms", [])
    ext = extract_markdown(doc, protected_terms=names)
    chunks = spans_to_chunks(
        ext.spans,
        source_language=proj.config.source_language,
        target_language=proj.config.target_language,
        chunk_size=proj.config.chunk_size,
    )
    proj.chunks_dir.mkdir(parents=True, exist_ok=True)
    for c in chunks:
        (proj.chunks_dir / f"{c.chunk_id}.json").write_text(
            c.model_dump_json(), encoding="utf-8"
        )
    return chunks


def _identity_translation(chunks: list[Chunk]) -> dict[str, object]:
    """Translation that returns source text unchanged (placeholders intact)."""
    out: dict[str, object] = {}
    for c in chunks:
        out[c.chunk_id] = {
            "chunk_id": c.chunk_id,
            "records": [{"id": r.id, "target": r.source} for r in c.records],
        }
    return out


def _write_translations(proj, translations: dict[str, object]) -> None:
    proj.translated_dir.mkdir(parents=True, exist_ok=True)
    for cid, payload in translations.items():
        (proj.translated_dir / f"{cid}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )


def test_records_to_span_text_joins_and_restores():
    span = ProseSpan(
        text="__NAME_001__ ran `code`.",
        placeholders=[
            # placeholder tokens from a hypothetical extraction
        ],
        protected_terms=[],
    )
    # No placeholders to restore; just join.
    assert records_to_span_text(span, ["__NAME_001__ ran", "`code`."]).strip()


def test_build_markdown_identity_roundtrip(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    (proj.source_dir / "book.md").write_text(MARKDOWN_DOC, encoding="utf-8")
    find_source_file(proj)  # sync config
    proj = load_project(proj.root)

    chunks = _write_source_chunks_markdown(proj)
    _write_translations(proj, _identity_translation(chunks))

    result = build_project(proj)
    assert result.format == "markdown"
    assert result.output_path.is_file()

    out = result.output_path.read_text("utf-8")
    # Names/code/links restored; structure preserved
    assert "Alice" in out and "Bob" in out and "Baker Street" in out
    assert "`print(1)`" in out
    assert "https://example.com" in out
    assert "```python" in out
    assert 'print("never translated")' in out
    # No leftover placeholders
    for token in ("__NAME_", "__TAG_", "__SPANTX_"):
        assert token not in out


def test_build_markdown_translates_text(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    doc = "# Hello\n\nAlice ran fast.\n"
    # Mark Alice as protected so it survives translation verbatim.
    proj.names_path.write_text(
        json.dumps({"protected_terms": ["Alice"]}), encoding="utf-8"
    )
    (proj.source_dir / "book.md").write_text(doc, encoding="utf-8")
    find_source_file(proj)
    proj = load_project(proj.root)
    chunks = _write_source_chunks_markdown(proj, doc)

    # Translate: uppercase every record (keeps __NAME_001__ intact).
    translated = {}
    for c in chunks:
        translated[c.chunk_id] = {
            "chunk_id": c.chunk_id,
            "records": [{"id": r.id, "target": r.source.upper()} for r in c.records],
        }
    _write_translations(proj, translated)

    result = build_project(proj)
    out = result.output_path.read_text("utf-8")
    # The heading "Hello" became "HELLO"; name placeholder restored to "Alice".
    assert "HELLO" in out
    assert "Alice" in out


def test_build_epub_identity_roundtrip(tmp_path: Path):
    import warnings

    from ebooklib import epub as epubmod

    import tests.test_epub_io as epub_fixtures
    from spinetx.epub_io import extract_epub

    warnings.filterwarnings("ignore")
    proj = init_project(tmp_path / "book", target_language="de")
    epub_path = proj.source_dir / "book.epub"
    epub_fixtures._make_epub(epub_path)
    find_source_file(proj)
    proj = load_project(proj.root)

    # Extract and write source chunks
    names = json.loads(proj.names_path.read_text("utf-8")).get("protected_terms", [])
    extraction = extract_epub(str(epub_path), protected_terms=names)
    chunks = spans_to_chunks(
        extraction.spans,
        source_language=proj.config.source_language,
        target_language=proj.config.target_language,
        chunk_size=proj.config.chunk_size,
    )
    proj.chunks_dir.mkdir(parents=True, exist_ok=True)
    for c in chunks:
        (proj.chunks_dir / f"{c.chunk_id}.json").write_text(
            c.model_dump_json(), encoding="utf-8"
        )
    _write_translations(proj, _identity_translation(chunks))

    result = build_project(proj)
    assert result.format == "epub"
    assert result.output_path.is_file()

    # Read back and check structure preserved
    reread = epubmod.read_epub(str(result.output_path), options={})
    ch1 = next(
        it
        for it in reread.get_items_of_type(9)  # ITEM_DOCUMENT
        if getattr(it, "file_name", "") == "ch1.xhtml"
    )
    content = ch1.get_content().decode("utf-8")
    assert "<strong>Bob</strong>" in content
    assert "do_not_translate();" in content
    assert "<code>pip install</code>" in content


def test_build_epub_fails_on_unresolved_placeholder_token(tmp_path: Path):
    import warnings

    import tests.test_epub_io as epub_fixtures
    from spinetx.epub_io import extract_epub

    warnings.filterwarnings("ignore")
    proj = init_project(tmp_path / "book", target_language="de")
    epub_path = proj.source_dir / "book.epub"
    epub_fixtures._make_epub(epub_path)
    find_source_file(proj)
    proj = load_project(proj.root)

    extraction = extract_epub(str(epub_path))
    chunks = spans_to_chunks(
        extraction.spans,
        source_language=proj.config.source_language,
        target_language=proj.config.target_language,
        chunk_size=proj.config.chunk_size,
    )
    proj.chunks_dir.mkdir(parents=True, exist_ok=True)
    for c in chunks:
        (proj.chunks_dir / f"{c.chunk_id}.json").write_text(
            c.model_dump_json(), encoding="utf-8"
        )

    translations = _identity_translation(chunks)
    for chunk_payload in translations.values():
        for record in chunk_payload["records"]:
            record["target"] = "__TAG_999__"
    _write_translations(proj, translations)

    with pytest.raises(BuildError, match="unresolved placeholder __TAG_999__"):
        build_project(proj)
