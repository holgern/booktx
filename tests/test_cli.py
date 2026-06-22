"""Smoke tests for the booktx Typer CLI."""

from __future__ import annotations

import json
from pathlib import Path

from ebooklib import epub
from typer.testing import CliRunner

from booktx.cli import app

runner = CliRunner()


MARKDOWN_DOC = """\
---
title: Demo
---

# Hello

Alice met Bob. They were happy.

```python
print("x")
```
"""


def _make_markdown_project(tmp_path: Path) -> Path:
    project_dir = tmp_path / "book"
    src = tmp_path / "novel.md"
    src.write_text(MARKDOWN_DOC, encoding="utf-8")
    res = runner.invoke(
        app,
        ["init", str(project_dir), "--target", "de", "--source-file", str(src)],
    )
    assert res.exit_code == 0, res.output
    return project_dir


def _make_epub_project(tmp_path: Path) -> Path:
    project_dir = tmp_path / "epub-book"
    src = tmp_path / "novel.epub"
    book = epub.EpubBook()
    book.set_identifier("cli-epub-id")
    book.set_title("CLI EPUB")
    book.set_language("en")
    chapter = epub.EpubHtml(title="Chapter One", file_name="ch1.xhtml", lang="en")
    chapter.content = (
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        "<head><title>Chapter One</title></head><body>"
        "<h1>Chapter One</h1>"
        "<p>Alice met Bob.</p>"
        "</body></html>"
    )
    book.add_item(chapter)
    book.add_item(epub.EpubNav())
    book.add_item(epub.EpubNcx())
    book.spine = ["nav", chapter]
    epub.write_epub(str(src), book, {})
    res = runner.invoke(
        app,
        ["init", str(project_dir), "--target", "de", "--source-file", str(src)],
    )
    assert res.exit_code == 0, res.output
    return project_dir


def test_version_flag():
    res = runner.invoke(app, ["--version"])
    assert res.exit_code == 0
    assert "0.1.0" in res.output


def test_init_creates_layout(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    from booktx.config import tomllib

    with (project_dir / ".booktx" / "config.toml").open("rb") as fh:
        cfg = tomllib.load(fh)
    assert cfg["target_language"] == "de"
    assert cfg["source_language"] == "en"
    assert cfg["format"] == "markdown"
    for sub in (
        "source",
        ".booktx",
        ".booktx/chunks",
        ".booktx/translated",
        "output",
    ):
        assert (project_dir / sub).is_dir()


def test_inspect_prints_summary(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    res = runner.invoke(app, ["inspect", str(project_dir)])
    assert res.exit_code == 0, res.output
    assert "markdown" in res.output
    assert "estimated_records" in res.output


def test_inspect_epub_prints_spine_document_details(tmp_path: Path):
    project_dir = _make_epub_project(tmp_path)
    res = runner.invoke(app, ["inspect", str(project_dir)])
    assert res.exit_code == 0, res.output
    assert "epub" in res.output
    assert "spine document(s) with text blocks" in res.output


def test_extract_writes_chunks(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    res = runner.invoke(app, ["extract", str(project_dir)])
    assert res.exit_code == 0, res.output
    chunks = list((project_dir / ".booktx" / "chunks").glob("*.json"))
    assert chunks, "no chunks written"
    first = json.loads(chunks[0].read_text("utf-8"))
    assert set(first.keys()) == {
        "chunk_id",
        "source_language",
        "target_language",
        "records",
    }
    assert first["records"][0]["id"].count("-") == 1


def test_extract_is_idempotent_and_preserves_translated(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    runner.invoke(app, ["extract", str(project_dir)])
    # Pretend a translation exists
    translated_dir = project_dir / ".booktx" / "translated"
    translated_dir.mkdir(parents=True, exist_ok=True)
    (translated_dir / "0001.json").write_text(
        '{"chunk_id": "0001", "records": []}', encoding="utf-8"
    )
    before = (project_dir / ".booktx" / "chunks" / "0001.json").read_text("utf-8")
    # Re-extract
    res = runner.invoke(app, ["extract", str(project_dir)])
    assert res.exit_code == 0, res.output
    after = (project_dir / ".booktx" / "chunks" / "0001.json").read_text("utf-8")
    assert before == after  # deterministic
    # translated file survives
    assert (translated_dir / "0001.json").is_file()


def test_next_prints_first_untranslated_then_exits_nonzero_when_done(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    runner.invoke(app, ["extract", str(project_dir)])
    # First untranslated
    res = runner.invoke(app, ["next", str(project_dir), "--allow-missing-context"])
    assert res.exit_code == 0, res.output
    assert "0001" in res.output
    # Provide a translation for every chunk
    translated_dir = project_dir / ".booktx" / "translated"
    for chunk_file in (project_dir / ".booktx" / "chunks").glob("*.json"):
        chunk = json.loads(chunk_file.read_text("utf-8"))
        payload = {
            "chunk_id": chunk["chunk_id"],
            "records": [
                {"id": r["id"], "target": r["source"]} for r in chunk["records"]
            ],
        }
        (translated_dir / chunk_file.name).write_text(
            json.dumps(payload), encoding="utf-8"
        )
    res2 = runner.invoke(app, ["next", str(project_dir), "--allow-missing-context"])
    assert res2.exit_code == 1
    assert "All" in res2.output


def test_next_requires_ready_context(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    runner.invoke(app, ["extract", str(project_dir)])
    res = runner.invoke(app, ["next", str(project_dir)])
    assert res.exit_code == 1
    assert "context" in res.output.lower()
    assert "booktx context init" in res.output


def test_next_allow_missing_context_legacy_override(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    runner.invoke(app, ["extract", str(project_dir)])
    res = runner.invoke(app, ["next", str(project_dir), "--allow-missing-context"])
    assert res.exit_code == 0
    assert "0001" in res.output


def test_next_without_chunks_tells_user_to_extract(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    res = runner.invoke(app, ["next", str(project_dir), "--allow-missing-context"])
    assert res.exit_code == 1
    assert "booktx extract" in res.output


def test_next_unit_chapter_without_chunks_tells_user_to_extract(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    res = runner.invoke(
        app,
        ["next", str(project_dir), "--unit", "chapter", "--allow-missing-context"],
    )
    assert res.exit_code == 1
    assert "booktx extract" in res.output


def test_next_prints_context_path_when_context_ready(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    runner.invoke(app, ["extract", str(project_dir)])
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    runner.invoke(app, ["context", "mark-ready", str(project_dir), "--force"])
    res = runner.invoke(app, ["next", str(project_dir)])
    assert res.exit_code == 0, res.output
    assert "context:" in res.output
    assert "context.md" in res.output
    assert "0001" in res.output


def test_validate_passes_with_identity_translation(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    runner.invoke(app, ["extract", str(project_dir)])
    translated_dir = project_dir / ".booktx" / "translated"
    for chunk_file in (project_dir / ".booktx" / "chunks").glob("*.json"):
        chunk = json.loads(chunk_file.read_text("utf-8"))
        payload = {
            "chunk_id": chunk["chunk_id"],
            "records": [
                {"id": r["id"], "target": r["source"]} for r in chunk["records"]
            ],
        }
        (translated_dir / chunk_file.name).write_text(
            json.dumps(payload), encoding="utf-8"
        )
    res = runner.invoke(app, ["validate", str(project_dir)])
    assert res.exit_code == 0, res.output
    assert "errors=0" in res.output


def test_validate_fails_on_empty_target(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    runner.invoke(app, ["extract", str(project_dir)])
    translated_dir = project_dir / ".booktx" / "translated"
    chunk_file = next((project_dir / ".booktx" / "chunks").glob("*.json"))
    chunk = json.loads(chunk_file.read_text("utf-8"))
    payload = {
        "chunk_id": chunk["chunk_id"],
        "records": [{"id": r["id"], "target": "   "} for r in chunk["records"]],
    }
    (translated_dir / chunk_file.name).write_text(json.dumps(payload), encoding="utf-8")
    res = runner.invoke(app, ["validate", str(project_dir)])
    assert res.exit_code == 1
    assert "empty_target" in res.output


def test_build_produces_output(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    runner.invoke(app, ["extract", str(project_dir)])
    translated_dir = project_dir / ".booktx" / "translated"
    for chunk_file in (project_dir / ".booktx" / "chunks").glob("*.json"):
        chunk = json.loads(chunk_file.read_text("utf-8"))
        payload = {
            "chunk_id": chunk["chunk_id"],
            "records": [
                {"id": r["id"], "target": r["source"]} for r in chunk["records"]
            ],
        }
        (translated_dir / chunk_file.name).write_text(
            json.dumps(payload), encoding="utf-8"
        )
    res = runner.invoke(app, ["build", str(project_dir)])
    assert res.exit_code == 0, res.output
    out_file = project_dir / "output" / "novel.de.md"
    assert out_file.is_file()
    out = out_file.read_text("utf-8")
    assert "Alice" in out and "Bob" in out
    for token in ("__NAME_", "__TAG_", "__SPANTX_"):
        assert token not in out


def test_full_pipeline_end_to_end(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    # extract
    assert runner.invoke(app, ["extract", str(project_dir)]).exit_code == 0
    # next
    res_next = runner.invoke(app, ["next", str(project_dir), "--allow-missing-context"])
    assert res_next.exit_code == 0
    # translate identity
    translated_dir = project_dir / ".booktx" / "translated"
    for chunk_file in (project_dir / ".booktx" / "chunks").glob("*.json"):
        chunk = json.loads(chunk_file.read_text("utf-8"))
        payload = {
            "chunk_id": chunk["chunk_id"],
            "records": [
                {"id": r["id"], "target": r["source"]} for r in chunk["records"]
            ],
        }
        (translated_dir / chunk_file.name).write_text(
            json.dumps(payload), encoding="utf-8"
        )
    # validate + build
    assert runner.invoke(app, ["validate", str(project_dir)]).exit_code == 0
    assert runner.invoke(app, ["build", str(project_dir)]).exit_code == 0
    assert (project_dir / "output" / "novel.de.md").is_file()


def test_init_rejects_unsupported_source(tmp_path: Path):
    bad = tmp_path / "x.pdf"
    bad.write_bytes(b"%PDF-1.4")
    res = runner.invoke(
        app,
        ["init", str(tmp_path / "p"), "--target", "de", "--source-file", str(bad)],
    )
    assert res.exit_code == 1
