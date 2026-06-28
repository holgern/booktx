# ruff: noqa: E501

"""Tests for booktx chapter detection."""

from __future__ import annotations

import json
from pathlib import Path

from ebooklib import epub
from typer.testing import CliRunner

from booktx.chapters import detect_chapters, load_chapter_map, write_chapter_map
from booktx.cli import app
from booktx.config import init_project, load_project, project_source_sha256

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
    manifest_path = project_dir / ".booktx" / "source-manifest.json"
    payload = json.loads(manifest_path.read_text("utf-8"))
    transform(payload)
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _clear_manifest_navigation(payload: dict[str, object]) -> None:
    payload["template"].update({"navigation": []})


def _to_legacy_manifest(payload: dict[str, object]) -> None:
    template = payload["template"]
    template["chapter_mapping"] = "legacy"
    for span in template.get("spans", []):
        span["chapter_id"] = None
        span["chapter_title"] = None


def test_detect_markdown_headings_and_chunk_ranges(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    project = load_project(project_dir)
    chapter_map = detect_chapters(project)
    assert [chapter.title for chapter in chapter_map.chapters] == ["One", "Two"]
    assert chapter_map.source_sha256 == project_source_sha256(project)
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
    _rewrite_manifest(project_dir, _to_legacy_manifest)

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


def _make_epub_with_links(
    path: Path,
    *,
    chapters: list[tuple[str, str, str]],
    toc_links: list[tuple[str, str]],
    headings: bool = False,
) -> None:
    """Build an EPUB whose toc is explicit ``(href, title)`` links.

    ``chapters`` is a list of ``(file_name, title, body_html)``. ``toc_links`` is a
    list of ``(href, title)`` rendered as ``epub.Link`` entries, so hrefs may
    include fragments or point anywhere.
    """
    book = epub.EpubBook()
    book.set_identifier("links-id")
    book.set_title("Links Book")
    book.set_language("en")
    book.add_author("Test")
    items = []
    for file_name, title, body in chapters:
        heading = f"<h1>{title}</h1>" if headings else ""
        ch = epub.EpubHtml(title=title, file_name=file_name, lang="en")
        ch.content = (
            '<html xmlns="http://www.w3.org/1999/xhtml">'
            f"<head><title>{title}</title></head><body>{heading}{body}</body></html>"
        )
        book.add_item(ch)
        items.append(ch)
    book.spine = ["nav", *items]
    book.add_item(epub.EpubNav())
    book.add_item(epub.EpubNcx())
    book.toc = tuple(
        epub.Link(href, title, f"link-{i}")
        for i, (href, title) in enumerate(toc_links)
    )
    epub.write_epub(str(path), book, {})


def test_extract_persists_block_chapter_annotations(tmp_path: Path):
    project_dir = _make_epub_project(tmp_path)
    payload = json.loads(
        (project_dir / ".booktx" / "source-manifest.json").read_text("utf-8")
    )
    template = payload["template"]
    spans = template["spans"]
    assert template["chapter_mapping"] == "epub2text-block-v1"
    assert any(span.get("chapter_id") for span in spans)
    assert any(span.get("chapter_title") == "Chapter One" for span in spans)


def test_detect_uses_annotations_when_navigation_removed(tmp_path: Path):
    project_dir = _make_epub_project(tmp_path)
    _rewrite_manifest(project_dir, _clear_manifest_navigation)
    payload = json.loads(
        (project_dir / ".booktx" / "source-manifest.json").read_text("utf-8")
    )
    assert payload["template"]["chapter_mapping"] == "epub2text-block-v1"
    chapter_map = detect_chapters(load_project(project_dir))
    assert [c.title for c in chapter_map.chapters] == ["Chapter One", "Chapter Two"]


def test_whole_document_toc_href_maps_to_document_start(tmp_path: Path):
    # headings=False so the only chapter signal is the whole-document toc links.
    project_dir = _make_epub_project(tmp_path, headings=False)
    chapter_map = detect_chapters(load_project(project_dir))
    assert [c.title for c in chapter_map.chapters] == ["Chapter One", "Chapter Two"]


def test_unresolved_fragment_is_not_annotated(tmp_path: Path):
    source = tmp_path / "frag.epub"
    _make_epub_with_links(
        source,
        chapters=[("ch1.xhtml", "Chapter One", "<p>Alice met Bob. A second sentence.</p>")],
        toc_links=[("ch1.xhtml#missing", "Chapter One")],
        headings=False,
    )
    project = init_project(
        tmp_path / "frag", target_language="de", source_file=source, chunk_size=2
    )
    res = runner.invoke(app, ["extract", str(project.root)])
    assert res.exit_code == 0, res.output
    payload = json.loads(
        (project.root / ".booktx" / "source-manifest.json").read_text("utf-8")
    )
    spans = payload["template"]["spans"]
    # Upstream _effective_start rejects the unresolved fragment, so no block is
    # annotated with a chapter id.
    assert not any(span.get("chapter_id") for span in spans)


def test_v1_all_null_annotations_do_not_use_legacy_navigation():
    from booktx.chapters import _epub_boundaries_from_refs
    from booktx.models import EpubNavigationRef, EpubSpanRef

    spans = [
        EpubSpanRef(
            span_index=0,
            block_id="b0",
            document_href="ch1.xhtml",
            spine_index=1,
            tag_name="p",
            source_text="Body text with no links.",
            source_text_sha256="h",
        )
    ]
    fallback_nav = [
        EpubNavigationRef(
            id="n1",
            title="Chapter One",
            document_href="ch1.xhtml",
            spine_index=1,
            source="fallback",
        )
    ]
    # v1 with all-None annotations is authoritative "no assignment": legacy
    # navigation is not consulted.
    assert _epub_boundaries_from_refs(
        spans, fallback_nav, chapter_mapping="epub2text-block-v1"
    ) == []
    # The legacy path does consult a whole-document (non-fallback) entry.
    whole_doc_nav = [
        EpubNavigationRef(
            id="n1", title="Chapter One", document_href="ch1.xhtml", spine_index=1
        )
    ]
    legacy = _epub_boundaries_from_refs(
        spans, whole_doc_nav, chapter_mapping="legacy"
    )
    assert any(b.title == "Chapter One" for b in legacy)


def test_legacy_navigation_mapper_is_conservative():
    from booktx.chapters import _navigation_span_index
    from booktx.models import EpubNavigationRef, EpubSpanRef

    spans = [
        EpubSpanRef(span_index=0, block_id="b0", document_href="ch1.xhtml", spine_index=1, tag_name="p", source_text="x", source_text_sha256="h", source_char_start=0),
        EpubSpanRef(span_index=2, block_id="b1", document_href="ch1.xhtml", spine_index=1, tag_name="p", source_text="y", source_text_sha256="h", source_char_start=50),
    ]
    # fallback entry ignored
    assert _navigation_span_index(
        EpubNavigationRef(id="fb", title="FB", document_href="ch1.xhtml", spine_index=1, source="fallback"),
        spans,
    ) is None
    # unresolved fragment ignored
    assert _navigation_span_index(
        EpubNavigationRef(id="uf", title="UF", document_href="ch1.xhtml", fragment="missing", source_char_start=None, spine_index=1),
        spans,
    ) is None
    # whole-document href with known doc+spine -> document start span_index
    assert _navigation_span_index(
        EpubNavigationRef(id="wd", title="WD", document_href="ch1.xhtml", fragment=None, source_char_start=None, spine_index=1),
        spans,
    ) == 0
    # resolved offset beyond all matching spans -> None (not matches[0])
    assert _navigation_span_index(
        EpubNavigationRef(id="be", title="BE", document_href="ch1.xhtml", source_char_start=999, spine_index=1),
        spans,
    ) is None


def test_missing_record_mapping_raises():
    from booktx.chapters import _Boundary, _build_epub_chapter_map
    from booktx.models import Record

    records = [Record(id=f"0001-00000{i}", source="x", span_index=i) for i in range(3)]
    cm = _build_epub_chapter_map([_Boundary(0, "A"), _Boundary(2, "B")], records)
    assert [c.title for c in cm.chapters] == ["A", "B"]
    raised = False
    try:
        _build_epub_chapter_map([_Boundary(50, "Ghost")], records)
    except ValueError as exc:
        raised = True
        assert "span_index=50" in str(exc)
    assert raised, "expected ValueError for missing record mapping"


def test_chapter_map_version_invalidation_regenerates(tmp_path: Path):
    from booktx.chapters import (
        CURRENT_CHAPTER_MAP_VERSION,
        ChapterMap,
        ensure_chapter_map,
    )

    project_dir = _make_epub_project(tmp_path)
    project = load_project(project_dir)
    source_sha = project_source_sha256(project)
    # Stale-version map with the current source SHA.
    write_chapter_map(project, ChapterMap(version=1, source_sha256=source_sha, chapters=[]))
    regenerated = ensure_chapter_map(project)
    assert regenerated.version == CURRENT_CHAPTER_MAP_VERSION
    assert regenerated.chapters  # regenerated, not the empty stale map
    reloaded = load_chapter_map(project)
    assert reloaded is not None and reloaded.version == CURRENT_CHAPTER_MAP_VERSION
