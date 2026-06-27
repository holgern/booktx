"""Regression tests for the EPUB visible-TOC chapter-map audit.

Covers the five cases described in ``booktx_chapter_map_review.md``:

1. TOC promises more numbered chapters than were extracted (preview/truncated).
2. Navigation is partial but body headings complete the numbered sequence.
3. Navigation is partial, no headings, but TOC targets are extracted documents.
4. TOC promises 26 chapters while the spine only includes the first 10.
5. The uploaded shape: TOC ONE..TWENTY-SIX with a map ending at TEN.

Plus unit tests for the pure helpers: ordinal parsing, href normalization,
TOC-link extraction, and TOC-derived document-start boundaries.
"""

from __future__ import annotations

import json
from pathlib import Path

from ebooklib import epub
from typer.testing import CliRunner

from booktx.chapters import detect_chapters
from booktx.cli import app
from booktx.config import init_project, load_project
from booktx.epub_toc_audit import (
    audit_epub_chapter_map,
    chapter_ordinal,
    extract_toc_entries,
    normalize_href,
    toc_document_start_boundaries,
)

runner = CliRunner()

_WORDS = [
    "ONE",
    "TWO",
    "THREE",
    "FOUR",
    "FIVE",
    "SIX",
    "SEVEN",
    "EIGHT",
    "NINE",
    "TEN",
    "ELEVEN",
    "TWELVE",
    "THIRTEEN",
    "FOURTEEN",
    "FIFTEEN",
    "SIXTEEN",
    "SEVENTEEN",
    "EIGHTEEN",
    "NINETEEN",
    "TWENTY",
    "TWENTY-ONE",
    "TWENTY-TWO",
    "TWENTY-THREE",
    "TWENTY-FOUR",
    "TWENTY-FIVE",
    "TWENTY-SIX",
]


def _word(n: int) -> str:
    return _WORDS[n - 1]


def _build_epub(
    path: Path,
    *,
    toc_count: int,
    spine_count: int,
    contents: bool = True,
    headings: bool = True,
) -> None:
    """Build a synthetic EPUB for chapter-audit regression cases.

    ``toc_count`` is the number of chapters the visible contents page promises.
    ``spine_count`` is the number of chapter documents actually present and in
    the spine. The EPUB ``book.toc`` always lists every spine chapter so
    navigation can be compared against the contents page.
    """
    book = epub.EpubBook()
    book.set_identifier(f"audit-{toc_count}-{spine_count}")
    book.set_title("Audit Fixture")
    book.set_language("en")
    book.add_author("Test")

    spine: list[object] = ["nav"]
    chapter_items: list[epub.EpubHtml] = []

    if contents:
        anchors = " ".join(
            f'<a class="toc-link" href="ch{n:02d}.xhtml">{_word(n)}</a>'
            for n in range(1, toc_count + 1)
        )
        contents_doc = epub.EpubHtml(
            title="Contents", file_name="contents.xhtml", lang="en"
        )
        contents_doc.content = (
            '<html xmlns="http://www.w3.org/1999/xhtml">'
            "<head><title>Contents</title></head><body>"
            f"<p>{anchors}</p></body></html>"
        )
        book.add_item(contents_doc)
        spine.append(contents_doc)

    for n in range(1, spine_count + 1):
        file_name = f"ch{n:02d}.xhtml"
        title = _word(n)
        heading = f"<h1>{title}</h1>" if headings else ""
        chapter = epub.EpubHtml(title=title, file_name=file_name, lang="en")
        chapter.content = (
            '<html xmlns="http://www.w3.org/1999/xhtml">'
            f"<head><title>{title}</title></head><body>"
            f"{heading}"
            "<p>First sentence. Second sentence.</p></body></html>"
        )
        book.add_item(chapter)
        spine.append(chapter)
        chapter_items.append(chapter)

    book.spine = spine
    book.add_item(epub.EpubNav())
    book.add_item(epub.EpubNcx())
    book.toc = tuple(chapter_items)
    epub.write_epub(str(path), book, {})


def _make_project(
    tmp_path: Path,
    *,
    toc_count: int,
    spine_count: int,
    contents: bool = True,
    headings: bool = True,
) -> Path:
    source = tmp_path / "book.epub"
    _build_epub(
        source,
        toc_count=toc_count,
        spine_count=spine_count,
        contents=contents,
        headings=headings,
    )
    project = init_project(
        tmp_path / "book",
        target_language="de",
        source_file=source,
        chunk_size=2,
    )
    res = runner.invoke(app, ["extract", str(project.root)])
    assert res.exit_code == 0, res.output
    return project.root


def _clear_manifest_navigation(payload: dict[str, object]) -> None:
    payload["template"].update({"navigation": []})


# --- pure helpers -----------------------------------------------------------


def test_chapter_ordinal_recognizes_number_forms():
    assert chapter_ordinal("ONE") == 1
    assert chapter_ordinal("TEN") == 10
    assert chapter_ordinal("ELEVEN") == 11
    assert chapter_ordinal("TWENTY-SIX") == 26
    assert chapter_ordinal("Chapter 12") == 12
    assert chapter_ordinal("Part Two") == 2
    assert chapter_ordinal("XII") == 12
    assert chapter_ordinal("xii") == 12
    assert chapter_ordinal("3") == 3
    assert chapter_ordinal("Front matter") is None
    assert chapter_ordinal("THE STORY SO FAR") is None
    assert chapter_ordinal("Dedication page") is None


def test_normalize_href_strips_fragments_and_decodes():
    assert normalize_href("OEBPS/Text/ch1.xhtml#sec1") == "OEBPS/Text/ch1.xhtml"
    assert normalize_href("chapter011.xhtml") == "chapter011.xhtml"
    assert normalize_href("ch%201.xhtml#x") == "ch 1.xhtml"
    assert normalize_href("#fragment") == ""
    assert normalize_href("") == ""
    assert normalize_href("mailto:x@y") == "mailto:x@y"


def test_extract_toc_entries_parses_anchors_with_nested_tags():
    html = (
        '<p><a class="toc-link" href="chapter011.xhtml">ELEVEN</a> and '
        '<a href="ch12.xhtml#x"><em>TWELVE</em></a></p>'
    )
    assert extract_toc_entries(html) == [
        ("chapter011.xhtml", "ELEVEN"),
        ("ch12.xhtml", "TWELVE"),
    ]


def test_toc_document_start_boundaries_uses_extracted_spans_only(tmp_path):
    # Build a project whose contents page promises three chapters but only
    # chapter one was extracted; TOC-derived boundaries must not include the
    # missing documents.
    project_dir = _make_project(
        tmp_path, toc_count=3, spine_count=1, contents=True, headings=False
    )
    project = load_project(project_dir)
    from booktx.config import load_manifest
    from booktx.epub_manifest import load_epub_template_from_manifest

    template = load_epub_template_from_manifest(load_manifest(project))
    boundaries = toc_document_start_boundaries(template.spans)
    titles = [title for _, title in boundaries]
    assert "ONE" in titles
    assert "TWO" not in titles
    assert "THREE" not in titles


# --- case 1: TOC promises more chapters than extracted ----------------------


def test_audit_reports_missing_extracted_toc_targets(tmp_path):
    project_dir = _make_project(
        tmp_path, toc_count=3, spine_count=1, contents=True, headings=True
    )
    project = load_project(project_dir)
    chapter_map = detect_chapters(project)
    # Only chapter one was extracted, so the map must not invent empty chapters
    # and must contain exactly one numbered story chapter.
    assert all(chapter.record_count > 0 for chapter in chapter_map.chapters)
    numbered = [
        chapter
        for chapter in chapter_map.chapters
        if chapter_ordinal(chapter.title) is not None
    ]
    assert [chapter.title for chapter in numbered] == ["ONE"]

    result = audit_epub_chapter_map(project, chapter_map=chapter_map)
    codes = {finding.code for finding in result.findings}
    assert "epub_toc_chapter_missing_from_map" in codes
    missing_href_findings = [
        finding
        for finding in result.findings
        if finding.code == "epub_toc_href_missing_from_extracted_spans"
    ]
    missing_titles = {finding.title for finding in missing_href_findings}
    assert {"TWO", "THREE"} <= missing_titles
    # No empty-chapter / extracted-but-unmapped error, because the targets were
    # not extracted at all.
    assert not any(
        finding.code == "epub_toc_href_extracted_but_unmapped"
        for finding in result.findings
    )


# --- case 2: partial navigation, complete headings --------------------------


def test_detect_completes_chapters_from_headings_when_nav_partial(tmp_path):
    project_dir = _make_project(
        tmp_path, toc_count=0, spine_count=3, contents=False, headings=True
    )
    # Remove navigation so detection falls through to headings. With three
    # chapter headings the map must contain all three even without navigation.
    manifest_path = Path(project_dir) / ".booktx" / "source-manifest.json"
    payload = json.loads(manifest_path.read_text("utf-8"))
    payload["template"]["navigation"] = payload["template"]["navigation"][:1]
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    project = load_project(project_dir)
    chapter_map = detect_chapters(project)
    titles = [chapter.title for chapter in chapter_map.chapters]
    assert "ONE" in titles
    assert "TWO" in titles
    assert "THREE" in titles


def test_detect_merges_partial_navigation_with_headings(tmp_path):
    # Navigation lists only the first two chapters; headings cover all three.
    project_dir = _make_project(
        tmp_path, toc_count=0, spine_count=3, contents=False, headings=True
    )
    manifest_path = Path(project_dir) / ".booktx" / "source-manifest.json"
    payload = json.loads(manifest_path.read_text("utf-8"))
    nav = payload["template"]["navigation"]
    payload["template"]["navigation"] = [
        entry for entry in nav if entry.get("title", "").startswith("Chapter")
    ][:2]
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    project = load_project(project_dir)
    chapter_map = detect_chapters(project)
    titles = [chapter.title for chapter in chapter_map.chapters]
    assert "ONE" in titles and "TWO" in titles and "THREE" in titles
    assert len(titles) == 3


# --- case 3: partial navigation, no headings, TOC targets extracted --------


def test_detect_uses_toc_document_starts_without_headings(tmp_path):
    project_dir = _make_project(
        tmp_path, toc_count=3, spine_count=3, contents=True, headings=False
    )
    # Keep navigation partial (first two chapters) so the TOC-derived start for
    # chapter three is the only thing that can complete the map.
    manifest_path = Path(project_dir) / ".booktx" / "source-manifest.json"
    payload = json.loads(manifest_path.read_text("utf-8"))
    payload["template"]["navigation"] = [
        entry
        for entry in payload["template"]["navigation"]
        if entry.get("title", "") in {"ONE", "TWO"}
    ]
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    project = load_project(project_dir)
    chapter_map = detect_chapters(project)
    titles = [chapter.title for chapter in chapter_map.chapters]
    assert "THREE" in titles
    # No empty chapters.
    assert all(chapter.record_count > 0 for chapter in chapter_map.chapters)


# --- case 4: TOC promises 26, spine has 10 ----------------------------------


def test_detect_does_not_create_empty_chapters_for_missing_targets(tmp_path):
    project_dir = _make_project(
        tmp_path, toc_count=26, spine_count=10, contents=True, headings=True
    )
    project = load_project(project_dir)
    chapter_map = detect_chapters(project)
    titles = [chapter.title for chapter in chapter_map.chapters]
    # All detected chapters must correspond to extracted content.
    assert all(chapter.record_count > 0 for chapter in chapter_map.chapters)
    assert "TEN" in titles
    assert "ELEVEN" not in titles


# --- case 5: uploaded shape (ONE..TWENTY-SIX TOC, map ends at TEN) ----------


def test_audit_reports_uploaded_shape_missing_chapters(tmp_path):
    project_dir = _make_project(
        tmp_path, toc_count=26, spine_count=10, contents=True, headings=True
    )
    project = load_project(project_dir)
    chapter_map = detect_chapters(project)
    map_titles = [chapter.title for chapter in chapter_map.chapters]
    assert "TEN" in map_titles
    assert "ELEVEN" not in map_titles

    result = audit_epub_chapter_map(project, chapter_map=chapter_map)
    assert result.numbered_toc_count == 26
    assert result.mapped_numbered_chapter_count == 10
    missing = set(result.missing_numbered_titles)
    assert {"ELEVEN", "TWELVE", "TWENTY-SIX"} <= missing
    codes = {finding.code for finding in result.findings}
    assert "epub_toc_chapter_missing_from_map" in codes
    assert "epub_toc_href_missing_from_extracted_spans" in codes
