"""Tests for booktx.progress helper primitives."""

from __future__ import annotations

from pathlib import Path

from booktx.chunking import ProseSpan, spans_to_chunks
from booktx.config import init_project, load_project
from booktx.progress import count_words, load_source_records, source_record_sha256


def _make_project(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de", chunk_size=2)
    (proj.source_dir / "book.md").write_text("# One\n\nAlice met Bob. Then they ran.\n")
    return load_project(proj.root)


def test_count_words_is_deterministic():
    assert count_words("Hello, world! 42 times.") == 4
    assert count_words("rock-and-roll won't stop") == 3


def test_source_record_sha256_is_stable():
    digest = source_record_sha256("Hello world.")
    assert digest == source_record_sha256("Hello world.")
    assert digest != source_record_sha256("Hello again.")


def test_load_source_records_flattens_chunks(tmp_path: Path):
    proj = _make_project(tmp_path)
    spans = [
        ProseSpan(
            text="Alice met Bob. Then they ran.",
            placeholders=[],
            protected_terms=[],
        )
    ]
    chunks = spans_to_chunks(
        spans,
        source_language="en",
        target_language="de",
        chunk_size=2,
    )
    for chunk in chunks:
        (proj.chunks_dir / f"{chunk.chunk_id}.json").write_text(
            chunk.model_dump_json(),
            encoding="utf-8",
        )

    records = load_source_records(proj)

    assert [record.record_id for record in records] == ["0001-000001", "0001-000002"]
    assert records[0].chunk_id == "0001"
    assert records[0].source_words == 3
    assert records[0].source_sha256
