"""Tests for booktx chapter detection."""

from __future__ import annotations

from pathlib import Path

from ebooklib import epub
from typer.testing import CliRunner

from booktx.chapters import detect_chapters, load_chapter_map, write_chapter_map
from booktx.cli import app
from booktx.config import init_project, load_project

runner = CliRunner()


def _make_markdown_project(tmp_path: Path) -> Path:
    src = tmp_path / "book.md"
    src.write_text(
        """\
# One

First sentence. Second sentence.

# Two

Third sentence. Fourth sentence.
""",
        encoding="utf-8",
    )
    project_dir = tmp_path / "book"
    res = runner.invoke(
        app,
        [
            "init",
            str(project_dir),
            "--target",
            "de",
            "--source-file",
            str(src),
            "--chunk-size",
            "2",
        ],
    )
    assert res.exit_code == 0, res.output
    ext = runner.invoke(app, ["extract", str(project_dir)])
    assert ext.exit_code == 0, ext.output
    return project_dir


def _make_epub(path: Path) -> None:
    book = epub.EpubBook()
    book.set_identifier("test-id-001")
    book.set_title("Test Book")
    book.set_language("en")
    book.add_author("Test Author")
    ch1 = epub.EpubHtml(title="Chapter One", file_name="ch1.xhtml", lang="en")
    ch1.content = (
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        "<head><title>Chapter One</title></head><body>"
        "<h1>Chapter One</h1>"
        "<p>Alice met Bob. A second sentence.</p>"
        "</body></html>"
    )
    ch2 = epub.EpubHtml(title="Chapter Two", file_name="ch2.xhtml", lang="en")
    ch2.content = (
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        "<head><title>Chapter Two</title></head><body>"
        "<h1>Chapter Two</h1>"
        "<p>The end.</p>"
        "</body></html>"
    )
    book.add_item(ch1)
    book.add_item(ch2)
    book.spine = [ch1, ch2]
    book.add_item(epub.EpubNav())
    book.add_item(epub.EpubNcx())
    book.toc = (ch1, ch2)
    epub.write_epub(str(path), book, {})


def _make_epub_project(tmp_path: Path) -> Path:
    source = tmp_path / "book.epub"
    _make_epub(source)
    project = init_project(
        tmp_path / "epub_book",
        target_language="de",
        source_file=source,
        chunk_size=2,
    )
    res = runner.invoke(app, ["extract", str(project.root)])
    assert res.exit_code == 0, res.output
    return project.root


def test_detect_markdown_headings_and_chunk_ranges(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    chapter_map = detect_chapters(load_project(project_dir))
    assert [ch.title for ch in chapter_map.chapters] == ["One", "Two"]
    assert chapter_map.chapters[0].chapter_id == "0001"
    assert chapter_map.chapters[0].chunk_ids == ["0001", "0002"]
    assert chapter_map.chapters[0].start_record_id == "0001-000001"
    assert chapter_map.chapters[0].end_record_id == "0002-000001"
    assert chapter_map.chapters[1].chunk_ids == ["0002", "0003"]


def test_detect_epub_spine_documents_and_headings(tmp_path: Path):
    project_dir = _make_epub_project(tmp_path)
    chapter_map = detect_chapters(load_project(project_dir))
    titles = [ch.title for ch in chapter_map.chapters]
    assert titles == ["Chapter One", "Chapter Two"]
    assert chapter_map.chapters[0].chunk_ids
    assert chapter_map.chapters[1].chunk_ids


def test_chapter_map_round_trips(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    project = load_project(project_dir)
    chapter_map = detect_chapters(project)
    write_chapter_map(project, chapter_map)
    loaded = load_chapter_map(project)
    assert loaded == chapter_map


def test_fallback_single_chapter_when_no_headings(tmp_path: Path):
    src = tmp_path / "plain.md"
    src.write_text("First sentence. Second sentence.", encoding="utf-8")
    project = init_project(
        tmp_path / "plain_book",
        target_language="de",
        source_file=src,
        chunk_size=1,
    )
    res = runner.invoke(app, ["extract", str(project.root)])
    assert res.exit_code == 0, res.output
    chapter_map = detect_chapters(load_project(project.root))
    assert len(chapter_map.chapters) == 1
    assert chapter_map.chapters[0].chapter_id == "0001"
    assert chapter_map.chapters[0].chunk_ids == ["0001", "0002"]
