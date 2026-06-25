"""Tests for booktx.build: rebuild markdown and epub from translated chunks."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from text2epub.validation import sha256_path
from typer.testing import CliRunner

import tests.test_epub_io as epub_fixtures
from booktx.build import BuildError, build_project, records_to_span_text
from booktx.chunking import ProseSpan, spans_to_chunks
from booktx.cli import app
from booktx.config import (
    find_source_file,
    init_project,
    load_project,
    write_profile_config,
    write_translation_store,
    write_translation_version_ledger,
)
from booktx.markdown_io import extract_markdown
from booktx.models import (
    QualityReviewConfig,
    ReviewPassConfig,
    StoredTranslationRecord,
    StoredTranslationRecordV2,
    TranslationCandidate,
    TranslationStore,
    TranslationStoreV2,
    TranslationSubversionLedgerEntry,
    TranslationTrackLedgerEntry,
    TranslationVersionLedger,
)
from booktx.progress import source_record_sha256
from booktx.translation_store import sha256_text

runner = CliRunner()

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


def _write_source_chunks_markdown(proj, doc: str = MARKDOWN_DOC):
    """Extract markdown, segment, and write source chunk files."""
    names = json.loads(proj.names_path.read_text("utf-8")).get("protected_terms", [])
    extraction = extract_markdown(doc, protected_terms=names)
    chunks = spans_to_chunks(
        extraction.spans,
        source_language=proj.config.source_language,
        target_language=proj.config.target_language,
        chunk_size=proj.config.chunk_size,
    )
    proj.chunks_dir.mkdir(parents=True, exist_ok=True)
    for chunk in chunks:
        (proj.chunks_dir / f"{chunk.chunk_id}.json").write_text(
            chunk.model_dump_json(), encoding="utf-8"
        )
    return chunks


def _identity_translation(chunks) -> dict[str, object]:
    out: dict[str, object] = {}
    for chunk in chunks:
        out[chunk.chunk_id] = {
            "chunk_id": chunk.chunk_id,
            "records": [
                {"id": record.id, "target": record.source} for record in chunk.records
            ],
        }
    return out


def _write_translations(proj, translations: dict[str, object]) -> None:
    proj.translated_dir.mkdir(parents=True, exist_ok=True)
    for chunk_id, payload in translations.items():
        (proj.translated_dir / f"{chunk_id}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )


def _write_store_identity_translation(proj, chunks) -> None:
    records: dict[str, StoredTranslationRecord] = {}
    for chunk in chunks:
        for record in chunk.records:
            records[record.id] = StoredTranslationRecord(
                chunk_id=chunk.chunk_id,
                source_sha256=source_record_sha256(record.source),
                target=record.source,
                updated_at="2026-06-22T12:00:00Z",
            )
    write_translation_store(proj, TranslationStore(records=records))


def _extract_epub_project(project_root: Path) -> None:
    result = runner.invoke(app, ["extract", str(project_root)])
    assert result.exit_code == 0, result.output


def _load_chunk_payloads(proj) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for chunk_path in proj.chunks():
        payloads.append(json.loads(chunk_path.read_text("utf-8")))
    return payloads


def test_records_to_span_text_joins_and_restores():
    span = ProseSpan(
        text="__NAME_001__ ran `code`.",
        placeholders=[],
        protected_terms=[],
    )
    assert records_to_span_text(span, ["__NAME_001__ ran", "`code`."]).strip()


def test_build_markdown_identity_roundtrip(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    (proj.source_dir / "book.md").write_text(MARKDOWN_DOC, encoding="utf-8")
    find_source_file(proj)
    proj = load_project(proj.root)

    chunks = _write_source_chunks_markdown(proj)
    _write_translations(proj, _identity_translation(chunks))

    result = build_project(proj)
    assert result.format == "markdown"
    assert result.output_path.is_file()

    out = result.output_path.read_text("utf-8")
    assert "Alice" in out and "Bob" in out and "Baker Street" in out
    assert "`print(1)`" in out
    assert "https://example.com" in out
    assert "```python" in out
    assert 'print("never translated")' in out
    for token in ("__NAME_", "__TAG_", "__SPANTX_"):
        assert token not in out


def test_build_markdown_translates_text(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    doc = "# Hello\n\nAlice ran fast.\n"
    proj.names_path.write_text(
        json.dumps({"protected_terms": ["Alice"]}), encoding="utf-8"
    )
    (proj.source_dir / "book.md").write_text(doc, encoding="utf-8")
    find_source_file(proj)
    proj = load_project(proj.root)
    chunks = _write_source_chunks_markdown(proj, doc)

    translated = {}
    for chunk in chunks:
        translated[chunk.chunk_id] = {
            "chunk_id": chunk.chunk_id,
            "records": [
                {"id": record.id, "target": record.source.upper()}
                for record in chunk.records
            ],
        }
    _write_translations(proj, translated)

    result = build_project(proj)
    out = result.output_path.read_text("utf-8")
    assert "HELLO" in out
    assert "Alice" in out


def test_build_markdown_uses_translation_store(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    doc = "# Hello\n\nAlice ran fast.\n"
    proj.names_path.write_text(
        json.dumps({"protected_terms": ["Alice"]}),
        encoding="utf-8",
    )
    (proj.source_dir / "book.md").write_text(doc, encoding="utf-8")
    find_source_file(proj)
    proj = load_project(proj.root)
    chunks = _write_source_chunks_markdown(proj, doc)

    _write_store_identity_translation(proj, chunks)

    result = build_project(proj)
    out = result.output_path.read_text("utf-8")
    assert "Hello" in out
    assert "Alice ran fast." in out


def test_build_require_complete_fails_when_records_missing(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    (proj.source_dir / "book.md").write_text(
        "# Hello\n\nAlice ran fast.\n", encoding="utf-8"
    )
    find_source_file(proj)
    proj = load_project(proj.root)
    _write_source_chunks_markdown(proj, "# Hello\n\nAlice ran fast.\n")

    with pytest.raises(BuildError, match="build requires complete translations"):
        build_project(proj, require_complete=True)


def test_build_epub_without_translations_is_byte_identical(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="en")
    epub_path = proj.source_dir / "book.epub"
    epub_fixtures._make_epub(epub_path)
    find_source_file(proj)
    _extract_epub_project(proj.root)
    proj = load_project(proj.root)

    result = build_project(proj)

    assert result.format == "epub"
    assert result.output_path.is_file()
    assert sha256_path(result.output_path) == sha256_path(epub_path)


def test_build_epub_identity_translations_are_byte_identical(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="en")
    epub_path = proj.source_dir / "book.epub"
    epub_fixtures._make_epub(epub_path)
    find_source_file(proj)
    _extract_epub_project(proj.root)
    proj = load_project(proj.root)

    translations: dict[str, object] = {}
    for chunk in _load_chunk_payloads(proj):
        translations[str(chunk["chunk_id"])] = {
            "chunk_id": chunk["chunk_id"],
            "records": [
                {"id": record["id"], "target": record["source"]}
                for record in chunk["records"]
            ],
        }
    _write_translations(proj, translations)

    result = build_project(proj)

    assert sha256_path(result.output_path) == sha256_path(epub_path)


def test_build_epub_changed_translation_has_no_token_leaks(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    epub_path = proj.source_dir / "book.epub"
    epub_fixtures._make_epub(epub_path)
    find_source_file(proj)
    _extract_epub_project(proj.root)
    proj = load_project(proj.root)

    translations: dict[str, object] = {}
    for chunk in _load_chunk_payloads(proj):
        records = []
        for record in chunk["records"]:
            source = record["source"]
            if source == "Alice met <strong>Bob</strong>.":
                target = "Hallo <strong>Welt</strong>."
            elif source == "A second sentence.":
                target = "Noch ein Satz."
            else:
                target = source
            records.append({"id": record["id"], "target": target})
        translations[str(chunk["chunk_id"])] = {
            "chunk_id": chunk["chunk_id"],
            "records": records,
        }
    _write_translations(proj, translations)

    result = build_project(proj)

    with zipfile.ZipFile(result.output_path) as archive:
        ch1_name = next(
            name for name in archive.namelist() if name.endswith("ch1.xhtml")
        )
        ch1 = archive.read(ch1_name).decode("utf-8")

    assert "Hallo <strong>Welt</strong>. Noch ein Satz." in ch1
    assert "<strong>Bob</strong>" not in ch1
    for token in ("__TAG_", "__NAME_", "__SPANTX_"):
        assert token not in ch1


def test_build_epub_fails_on_unresolved_placeholder_token(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    epub_path = proj.source_dir / "book.epub"
    epub_fixtures._make_epub(epub_path)
    find_source_file(proj)
    _extract_epub_project(proj.root)
    proj = load_project(proj.root)

    translations: dict[str, object] = {}
    for chunk in _load_chunk_payloads(proj):
        translations[str(chunk["chunk_id"])] = {
            "chunk_id": chunk["chunk_id"],
            "records": [
                {"id": record["id"], "target": "__TAG_999__"}
                for record in chunk["records"]
            ],
        }
    _write_translations(proj, translations)

    with pytest.raises(BuildError, match="placeholder_added"):
        build_project(proj)


def test_build_epub_fails_on_source_sha_mismatch(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    epub_path = proj.source_dir / "book.epub"
    epub_fixtures._make_epub(epub_path)
    find_source_file(proj)
    _extract_epub_project(proj.root)
    proj = load_project(proj.root)

    different_source = proj.source_dir / "replacement.epub"
    epub_fixtures._make_epub(different_source)
    with zipfile.ZipFile(different_source, "a") as archive:
        archive.writestr("extra.txt", "different")
    epub_path.write_bytes(different_source.read_bytes())

    with pytest.raises(BuildError, match="Source EPUB SHA256 mismatch"):
        build_project(proj)


# --- build --require-reviewed ------------------------------------------------


def _write_v2_store_identity(proj, chunks, *, reviews=None, active_review=None):
    """Write a v2 identity store.

    ``reviews`` and ``active_review`` are mappings keyed by record id so a
    review can be attached to a single record. A scalar ``active_review``
    string is applied to every record for backward compatibility.
    """
    records = {}
    for chunk in chunks:
        for record in chunk.records:
            rec_reviews = (
                reviews.get(record.id, [])
                if isinstance(reviews, dict)
                else (reviews or [])
            )
            rec_active_review = (
                active_review.get(record.id)
                if isinstance(active_review, dict)
                else active_review
            )
            records[record.id] = StoredTranslationRecordV2(
                chunk_id=int(chunk.chunk_id),
                part_id=int(record.id.split("-")[1]),
                source_sha256=source_record_sha256(record.source),
                source=record.source,
                active_version="1.1",
                active_review=rec_active_review,
                versions=[
                    TranslationCandidate(
                        version=1,
                        subversion=1,
                        version_ref="1.1",
                        target=record.source,
                        created_at="2026-06-22T12:00:00Z",
                        updated_at="2026-06-22T12:00:00Z",
                    )
                ],
                reviews=rec_reviews,
            )
    write_translation_store(proj, TranslationStoreV2(records=records))
    write_translation_version_ledger(
        proj,
        TranslationVersionLedger(
            active_version="1.1",
            tracks={
                "1": TranslationTrackLedgerEntry(
                    version=1,
                    actor="user:test",
                    harness="pi",
                    model="human",
                    created_at="2026-06-22T12:00:00Z",
                    updated_at="2026-06-22T12:00:00Z",
                    subversions={
                        "1": TranslationSubversionLedgerEntry(
                            version=1,
                            subversion=1,
                            version_ref="1.1",
                            context_sha256="a" * 64,
                            created_at="2026-06-22T12:00:00Z",
                            updated_at="2026-06-22T12:00:00Z",
                        )
                    },
                )
            },
        ),
    )


def _enable_quality_review(proj, *, active_passes=(1,), enforce="warn"):
    cfg = proj.profile_config.model_copy(
        update={
            "quality_review": QualityReviewConfig(
                enabled=True,
                active_passes=list(active_passes),
                passes=[
                    ReviewPassConfig(pass_number=p, enforce=enforce)
                    for p in active_passes
                ],
            )
        }
    )
    write_profile_config(proj, cfg)
    return load_project(proj.root)


def test_build_require_reviewed_fails_on_missing_review(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    (proj.source_dir / "book.md").write_text(
        "# Hello\n\nAlice ran fast.\n", encoding="utf-8"
    )
    find_source_file(proj)
    proj = load_project(proj.root)
    chunks = _write_source_chunks_markdown(proj, "# Hello\n\nAlice ran fast.\n")
    _write_v2_store_identity(proj, chunks)
    proj = _enable_quality_review(proj, enforce="warn")
    # require_reviewed treats the missing pass-1 review as an error even when
    # the pass is configured as a warning.
    with pytest.raises(BuildError, match="missing_review_candidate"):
        build_project(proj, require_reviewed=True)


def test_build_without_require_reviewed_succeeds_with_warn_coverage_gap(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    (proj.source_dir / "book.md").write_text(
        "# Hello\n\nAlice ran fast.\n", encoding="utf-8"
    )
    find_source_file(proj)
    proj = load_project(proj.root)
    chunks = _write_source_chunks_markdown(proj, "# Hello\n\nAlice ran fast.\n")
    _write_v2_store_identity(proj, chunks)
    proj = _enable_quality_review(proj, enforce="warn")
    # A warn-level coverage gap does not fail an ordinary build.
    result = build_project(proj)
    assert result.output_path.is_file()


def test_build_require_reviewed_fails_on_stale_active_review(tmp_path: Path):
    from booktx.models import TranslationReviewCandidate

    proj = init_project(tmp_path / "book", target_language="de")
    (proj.source_dir / "book.md").write_text(
        "# Hello\n\nAlice ran fast.\n", encoding="utf-8"
    )
    find_source_file(proj)
    proj = load_project(proj.root)
    chunks = _write_source_chunks_markdown(proj, "# Hello\n\nAlice ran fast.\n")
    record = chunks[0].records[0]
    review = TranslationReviewCandidate(
        pass_number=1,
        run_number=1,
        review_ref="R1.1",
        base_kind="translation",
        base_ref="1.1",
        base_target_sha256=sha256_text("different-baseline"),
        target=record.source,
        target_sha256=sha256_text(record.source),
        created_at="2026-06-22T12:00:00Z",
        updated_at="2026-06-22T12:00:00Z",
    )
    _write_v2_store_identity(
        proj, chunks, reviews={record.id: [review]}, active_review={record.id: "R1.1"}
    )
    proj = _enable_quality_review(proj, enforce="warn")
    with pytest.raises(BuildError, match="active_review_base_drift"):
        build_project(proj, require_reviewed=True)


def test_build_uses_active_review_output_when_valid(tmp_path: Path):
    from booktx.models import TranslationReviewCandidate

    proj = init_project(tmp_path / "book", target_language="de")
    (proj.source_dir / "book.md").write_text(
        "# Hello\n\nAlice ran fast.\n", encoding="utf-8"
    )
    find_source_file(proj)
    proj = load_project(proj.root)
    chunks = _write_source_chunks_markdown(proj, "# Hello\n\nAlice ran fast.\n")
    record = next(r for c in chunks for r in c.records if "ran fast" in r.source)
    polished = record.source.replace("ran fast", "ran sehr schnell")
    review = TranslationReviewCandidate(
        pass_number=1,
        run_number=1,
        review_ref="R1.1",
        base_kind="translation",
        base_ref="1.1",
        base_target_sha256=sha256_text(record.source),
        target=polished,
        target_sha256=sha256_text(polished),
        created_at="2026-06-22T12:00:00Z",
        updated_at="2026-06-22T12:00:00Z",
    )
    _write_v2_store_identity(
        proj, chunks, reviews={record.id: [review]}, active_review={record.id: "R1.1"}
    )
    result = build_project(proj)
    out = result.output_path.read_text("utf-8")
    assert "ran sehr schnell" in out
