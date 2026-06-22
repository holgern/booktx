"""Tests for booktx chapter detection."""

from __future__ import annotations

import json
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


def _make_epub(path: Path, *, headings: bool = True) -> None:
    book = epub.EpubBook()
    book.set_identifier("test-id-001")
    book.set_title("Test Book")
    book.set_language("en")
    book.add_author("Test Author")
    heading1 = "<h1>Chapter One</h1>" if headings else ""
    heading2 = "<h1>Chapter Two</h1>" if headings else ""
    ch1 = epub.EpubHtml(title="Chapter One", file_name="ch1.xhtml", lang="en")
    ch1.content = (
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        "<head><title>Chapter One</title></head><body>"
        f"{heading1}"
        "<p>Alice met Bob. A second sentence.</p>"
        "</body></html>"
    )
    ch2 = epub.EpubHtml(title="Chapter Two", file_name="ch2.xhtml", lang="en")
    ch2.content = (
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        "<head><title>Chapter Two</title></head><body>"
        f"{heading2}"
        "<p>The end.</p>"
        "</body></html>"
    )
    book.add_item(ch1)
    book.add_item(ch2)
    book.spine = ["nav", ch1, ch2]
    book.add_item(epub.EpubNav())
    book.add_item(epub.EpubNcx())
    book.toc = (ch1, ch2)
    epub.write_epub(str(path), book, {})


def _make_epub_project(tmp_path: Path, *, headings: bool = True) -> Path:
    source = tmp_path / "book.epub"
    _make_epub(source, headings=headings)
    project = init_project(
        tmp_path / "epub_book",
        target_language="de",
        source_file=source,
        chunk_size=2,
    )
    res = runner.invoke(app, ["extract", str(project.root)])
    assert res.exit_code == 0, res.output
    return project.root


def _rewrite_manifest(project_dir: Path, transform) -> None:
    manifest_path = project_dir / ".booktx" / "manifest.json"
    payload = json.loads(manifest_path.read_text("utf-8"))
    transform(payload)
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _clear_manifest_navigation(payload: dict[str, object]) -> None:
    payload["template"].update({"navigation": []})


def test_detect_markdown_headings_and_chunk_ranges(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    chapter_map = detect_chapters(load_project(project_dir))
    assert [chapter.title for chapter in chapter_map.chapters] == ["One", "Two"]
    assert chapter_map.chapters[0].chapter_id == "0001"
    assert chapter_map.chapters[0].chunk_ids == ["0001", "0002"]
    assert chapter_map.chapters[0].start_record_id == "0001-000001"
    assert chapter_map.chapters[0].end_record_id == "0002-000001"
    assert chapter_map.chapters[1].chunk_ids == ["0002", "0003"]


def test_detect_epub_uses_navigation_entries(tmp_path: Path):
    project_dir = _make_epub_project(tmp_path)
    chapter_map = detect_chapters(load_project(project_dir))
    titles = [chapter.title for chapter in chapter_map.chapters]
    assert titles == ["Chapter One", "Chapter Two"]


def test_detect_epub_uses_heading_fallback_when_navigation_missing(tmp_path: Path):
    project_dir = _make_epub_project(tmp_path)
    _rewrite_manifest(project_dir, _clear_manifest_navigation)

    chapter_map = detect_chapters(load_project(project_dir))

    assert [chapter.title for chapter in chapter_map.chapters] == [
        "Chapter One",
        "Chapter Two",
    ]


def test_detect_epub_falls_back_to_single_chapter_without_nav_or_headings(
    tmp_path: Path,
):
    project_dir = _make_epub_project(tmp_path, headings=False)
    _rewrite_manifest(project_dir, _clear_manifest_navigation)

    chapter_map = detect_chapters(load_project(project_dir))

    assert len(chapter_map.chapters) == 1
    assert chapter_map.chapters[0].chapter_id == "0001"
    assert chapter_map.chapters[0].chunk_ids


def test_chapter_map_round_trips(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    project = load_project(project_dir)
    chapter_map = detect_chapters(project)
    write_chapter_map(project, chapter_map)
    loaded = load_chapter_map(project)
    assert loaded == chapter_map
