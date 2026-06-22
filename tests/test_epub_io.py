"""Tests for booktx.epub_io: EPUB extraction and rebuild."""

from __future__ import annotations

import zipfile

import pytest
from ebooklib import epub
from text2epub.validation import sha256_path

from booktx.epub_io import build_epub, extract_epub, read_epub
from booktx.placeholders import restore


def _make_epub(path: str) -> None:
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
        "<p>Alice met <strong>Bob</strong>. A second sentence.</p>"
        "<pre>do_not_translate();</pre>"
        "<p>Run <code>pip install</code> now.</p>"
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
    book.spine = ["nav", ch1, ch2]
    nav = epub.EpubNav()
    book.add_item(nav)
    book.add_item(epub.EpubNcx())
    book.toc = (ch1, ch2)
    epub.write_epub(str(path), book, {})


def _make_raw_title_epub(path) -> None:
    title_xhtml = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
  <head><title>Title</title></head>
  <body>
    <section epub:type="title">
      <h1 class='book-title'>Blood&#160;of&#160;the&#160;Mantis</h1>
      <p class='flush-centered'>ADRIAN TCHAIKOVSKY</p>
    </section>
  </body>
</html>
"""
    container_xml = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
    content_opf = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">raw-title-book</dc:identifier>
    <dc:title>Raw Title Book</dc:title>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item
      id="nav"
      href="nav.xhtml"
      media-type="application/xhtml+xml"
      properties="nav"
    />
    <item id="title" href="title.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="nav"/>
    <itemref idref="title"/>
  </spine>
</package>
"""
    nav_xhtml = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
  <head><title>Navigation</title></head>
  <body>
    <nav epub:type="toc" id="toc">
      <ol>
        <li><a href="title.xhtml">Title</a></li>
      </ol>
    </nav>
  </body>
</html>
"""

    with zipfile.ZipFile(path, "w") as archive:
        mimetype = zipfile.ZipInfo("mimetype")
        mimetype.compress_type = zipfile.ZIP_STORED
        archive.writestr(mimetype, "application/epub+zip")
        archive.writestr("META-INF/container.xml", container_xml)
        archive.writestr("OEBPS/content.opf", content_opf)
        archive.writestr("OEBPS/nav.xhtml", nav_xhtml)
        archive.writestr("OEBPS/title.xhtml", title_xhtml)


def test_extract_reads_spine_documents_without_tag_placeholders(tmp_path):
    epub_path = tmp_path / "book.epub"
    _make_epub(epub_path)

    extraction = extract_epub(str(epub_path))

    joined = " ".join(span.text for span in extraction.spans)
    assert "Chapter One" in joined
    assert "Chapter Two" in joined
    assert "Alice met Bob. A second sentence." in joined
    assert "__TAG_" not in joined
    assert "__SPANTX_" not in joined
    assert "do_not_translate" not in joined

    entries = extraction.text2epub_manifest["entries"]
    hrefs = {entry["href"] for entry in entries}
    assert any(href.endswith("ch1.xhtml") for href in hrefs)
    assert any(href.endswith("ch2.xhtml") for href in hrefs)


def test_extract_protects_names(tmp_path):
    epub_path = tmp_path / "book.epub"
    _make_epub(epub_path)

    extraction = extract_epub(str(epub_path), protected_terms=["Alice", "Bob"])

    alice_tokens = {
        placeholder.token
        for span in extraction.spans
        for placeholder in span.placeholders
        if placeholder.original == "Alice"
    }
    assert len(alice_tokens) == 1
    assert "__TAG_" not in " ".join(span.text for span in extraction.spans)


def test_extract_title_like_xhtml_uses_document_order(tmp_path):
    book = epub.EpubBook()
    book.set_identifier("test-id-title")
    book.set_title("Title Order")
    book.set_language("en")
    title = epub.EpubHtml(title="Title", file_name="title.xhtml", lang="en")
    title.content = (
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        "<head><title>Title</title></head><body>"
        '<section epub:type="title">'
        '<h3 class="chapter-subtitle1">Subtitle</h3>'
        '<h2 class="book-author">Author</h2>'
        '<p class="flush-centered">Centered</p>'
        '<h1 class="book-title" id="title">Book <i>Three</i></h1>'
        '<p class="publisher-logo">Logo</p>'
        "</section></body></html>"
    )
    book.add_item(title)
    book.add_item(epub.EpubNav())
    book.add_item(epub.EpubNcx())
    book.spine = ["nav", title]
    epub_path = tmp_path / "title.epub"
    epub.write_epub(str(epub_path), book, {})

    extraction = extract_epub(str(epub_path))

    assert [span.text for span in extraction.spans][-5:] == [
        "Subtitle",
        "Author",
        "Centered",
        "Book Three",
        "Logo",
    ]


def test_extract_epub_uses_offsets_not_reparsed_xhtml(tmp_path):
    epub_path = tmp_path / "raw-title.epub"
    _make_raw_title_epub(epub_path)

    extraction = extract_epub(str(epub_path))

    texts = [span.text for span in extraction.spans]
    assert any("Blood" in text and "Mantis" in text for text in texts)
    assert any("ADRIAN TCHAIKOVSKY" in text for text in texts)

    title_entry = next(
        entry
        for entry in extraction.text2epub_manifest["entries"]
        if str(entry["href"]).endswith("title.xhtml")
    )
    with read_epub(str(epub_path)) as archive:
        raw = archive.read(title_entry["href"]).decode("utf-8")
    for block in title_entry["blocks"]:
        assert (
            block["source_fragment"]
            == raw[block["body_source_start"] : block["body_source_end"]]
        )


def test_extract_title_page_does_not_raise_raw_block_mapping_error(tmp_path):
    epub_path = tmp_path / "title-page.epub"
    _make_raw_title_epub(epub_path)

    extraction = extract_epub(str(epub_path))

    texts = [span.text.replace("\xa0", " ") for span in extraction.spans]
    assert texts == ["Blood of the Mantis", "ADRIAN TCHAIKOVSKY"]


def test_extract_builds_text2epub_manifest_with_inner_source_fragment(tmp_path):
    epub_path = tmp_path / "book.epub"
    _make_epub(epub_path)

    extraction = extract_epub(str(epub_path))
    chapter_entry = next(
        entry
        for entry in extraction.text2epub_manifest["entries"]
        if str(entry["href"]).endswith("ch1.xhtml")
    )
    block = next(
        block for block in chapter_entry["blocks"] if block["text"] == "Chapter One"
    )

    with read_epub(str(epub_path)) as archive:
        raw = archive.read(chapter_entry["href"]).decode("utf-8")

    assert block["replacement_mode"] == "whole_block_body"
    assert (
        block["source_fragment"]
        == raw[block["body_source_start"] : block["body_source_end"]]
    )


def test_build_identity_is_byte_identical(tmp_path):
    epub_path = tmp_path / "book.epub"
    out_path = tmp_path / "book.en.epub"
    _make_epub(epub_path)

    extraction = extract_epub(str(epub_path), protected_terms=["Alice", "Bob"])
    restored = [restore(span.text, span.placeholders) for span in extraction.spans]

    build_epub(str(epub_path), str(out_path), extraction, restored)

    assert sha256_path(out_path) == sha256_path(epub_path)


def test_build_changed_translation_has_no_token_leaks(tmp_path):
    epub_path = tmp_path / "book.epub"
    out_path = tmp_path / "book.de.epub"
    _make_epub(epub_path)

    extraction = extract_epub(str(epub_path))
    replacements = [restore(span.text, span.placeholders) for span in extraction.spans]
    paragraph_index = next(
        idx
        for idx, span_ref in enumerate(extraction.span_refs)
        if span_ref.source_text == "Alice met Bob. A second sentence."
    )
    replacements[paragraph_index] = "Hallo Welt."

    build_epub(str(epub_path), str(out_path), extraction, replacements)

    with read_epub(str(out_path)) as archive:
        ch1_name = next(
            name for name in archive.namelist() if name.endswith("ch1.xhtml")
        )
        ch1 = archive.read(ch1_name).decode("utf-8")

    assert "Hallo Welt." in ch1
    assert "<strong>Bob</strong>" not in ch1
    assert "__TAG_" not in ch1
    assert "__NAME_" not in ch1
    assert "__SPANTX_" not in ch1


def test_extract_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        extract_epub(str(tmp_path / "nope.epub"))
