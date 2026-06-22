"""Tests for booktx.config: project layout, config read/write, source resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from booktx.config import (
    BooktxError,
    detect_format,
    find_source_file,
    init_project,
    load_names,
    load_project,
    load_translation_store,
    load_translation_task,
    project_source_sha256,
    translation_ingest_block_path,
    translation_ingest_path,
    translation_store_path,
    translation_task_path,
    translation_task_source_block_path,
    write_translation_store,
    write_translation_task,
)
from booktx.models import StoredTranslationRecord, TranslationStore, TranslationTask


def test_detect_format():
    assert detect_format("book.md") == "markdown"
    assert detect_format("book.epub") == "epub"
    with pytest.raises(BooktxError):
        detect_format("book.pdf")


def test_init_creates_full_layout(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    for d in (
        proj.source_dir,
        proj.booktx_dir,
        proj.chunks_dir,
        proj.translated_dir,
        proj.tasks_dir,
        proj.ingest_dir,
        proj.reports_dir,
        proj.output_dir,
    ):
        assert d.is_dir(), f"{d} missing"
    assert proj.config_path.is_file()
    assert proj.names_path.is_file()
    assert proj.config.target_language == "de"
    assert proj.config.source_language == "en"
    # empty names.json
    assert load_names(proj).protected_terms == []


def test_init_copies_supplied_source(tmp_path: Path, monkeypatch):
    src = tmp_path / "novel.md"
    src.write_text("# Title\n", encoding="utf-8")
    proj = init_project(
        tmp_path / "book",
        target_language="de",
        source_file=src,
    )
    assert (proj.source_dir / "novel.md").is_file()
    assert proj.config.source_file == "novel.md"
    assert proj.config.format == "markdown"


def test_init_rejects_unsupported_source(tmp_path: Path):
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"%PDF-1.4")
    with pytest.raises(BooktxError):
        init_project(tmp_path / "book", target_language="de", source_file=src)

    from booktx.config import tomllib

    proj = init_project(tmp_path / "book", target_language="fr", source_language="en")
    cfg_path = proj.config_path
    # rewrite chunk_size and reload via TOML round-trip
    with cfg_path.open("rb") as fh:
        data = tomllib.load(fh)
    data["chunk_size"] = 25
    import tomli_w

    cfg_path.write_bytes(tomli_w.dumps(data).encode("utf-8"))
    proj2 = load_project(tmp_path / "book")
    assert proj2.config.chunk_size == 25
    assert proj2.config.target_language == "fr"


def test_load_project_rejects_non_project(tmp_path: Path):
    with pytest.raises(BooktxError):
        load_project(tmp_path / "nope")


def test_find_source_file_requires_exactly_one(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    # No source file -> error
    with pytest.raises(BooktxError):
        find_source_file(proj)
    # Two sources -> ambiguous
    (proj.source_dir / "a.md").write_text("a", encoding="utf-8")
    (proj.source_dir / "b.md").write_text("b", encoding="utf-8")
    with pytest.raises(BooktxError):
        find_source_file(proj)


def test_find_source_file_syncs_config(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    (proj.source_dir / "story.md").write_text("# Hi\n", encoding="utf-8")
    found = find_source_file(proj)
    assert found.name == "story.md"
    # config should now reflect the discovered file
    proj2 = load_project(tmp_path / "book")
    assert proj2.config.source_file == "story.md"
    assert proj2.config.format == "markdown"


def test_translation_store_helpers_roundtrip(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    (proj.source_dir / "story.md").write_text("# Hi\n", encoding="utf-8")
    proj = load_project(proj.root)

    assert translation_store_path(proj).name == "translation-store.json"

    empty = load_translation_store(proj)
    assert empty.records == {}

    store = TranslationStore(
        source_sha256=project_source_sha256(proj),
        records={
            "0001-000001": StoredTranslationRecord(
                chunk_id="0001",
                source_sha256="abc123",
                target="Hallo.",
                updated_at="2026-06-22T12:00:00Z",
            )
        },
    )
    write_translation_store(proj, store)

    loaded = load_translation_store(proj)
    assert loaded == store


def test_translation_task_helpers_roundtrip(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    task = TranslationTask(
        task_id="bt-task-1",
        unit="batch",
        source_language="en",
        target_language="de",
    )

    assert translation_task_path(proj, "bt-task-1").name == "bt-task-1.json"
    assert translation_ingest_path(proj, "bt-task-1").name == "bt-task-1.json"
    assert (
        translation_ingest_block_path(proj, "bt-task-1").name == "bt-task-1.block.txt"
    )
    assert (
        translation_task_source_block_path(proj, "bt-task-1").name
        == "bt-task-1.source.block.txt"
    )
    assert (
        translation_task_source_block_path(proj, "bt-task-1").parent == proj.tasks_dir
    )
    with pytest.raises(BooktxError):
        translation_task_source_block_path(proj, "bt-task/1")
    assert load_translation_task(proj, "bt-task-1") is None

    write_translation_task(proj, task)

    loaded = load_translation_task(proj, "bt-task-1")
    assert loaded == task
