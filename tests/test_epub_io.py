"""Tests for booktx.epub_io: EPUB extraction and rebuild."""

from __future__ import annotations

import zipfile

import ebooklib
import pytest
from ebooklib import epub

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


def test_extract_reads_spine_documents(tmp_path):
    epub_path = tmp_path / "book.epub"
    _make_epub(epub_path)
    ext = extract_epub(str(epub_path))
    # Two spine xhtml docs (nav is also xhtml, may add a span or none)
    files = {t.file_name for t in ext.templates}
    assert "ch1.xhtml" in files and "ch2.xhtml" in files
    joined = " ".join(s.text for s in ext.spans)
    assert "Chapter One" in joined
    assert "Chapter Two" in joined
    # Inline code / pre are not translated
    assert "do_not_translate" not in joined
    assert "pip install" not in joined


def test_extract_protects_names(tmp_path):
    epub_path = tmp_path / "book.epub"
    _make_epub(epub_path)
    ext = extract_epub(str(epub_path), protected_terms=["Alice", "Bob"])
    alice_tokens = {
        p.token
        for span in ext.spans
        for p in span.placeholders
        if p.original == "Alice"
    }
    assert len(alice_tokens) == 1


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

    ext = extract_epub(str(epub_path))

    assert [span.text for span in ext.spans][-5:] == [
        "Subtitle",
        "Author",
        "Centered",
        "Book __TAG_001__Three__TAG_002__",
        "Logo",
    ]


def test_build_roundtrips_structure_and_content(tmp_path):
    epub_path = tmp_path / "book.epub"
    out_path = tmp_path / "book.de.epub"
    _make_epub(epub_path)
    ext = extract_epub(str(epub_path))
    # Identity translation: restore placeholders in each span.
    restored = [restore(s.text, s.placeholders) for s in ext.spans]
    build_epub(str(epub_path), str(out_path), ext, restored)
    assert out_path.is_file()

    # The output must be a valid zip/epub
    assert zipfile.is_zipfile(str(out_path))
    reread = read_epub(str(out_path))
    titles = [getattr(it, "file_name", "") for it in reread.get_items()]
    assert "ch1.xhtml" in titles and "ch2.xhtml" in titles

    # Metadata preserved
    assert reread.get_metadata("DC", "title")[0][0] == "Test Book"
    assert reread.get_metadata("DC", "language")[0][0] == "en"


def test_build_preserves_inline_tags_after_rebuild(tmp_path):
    epub_path = tmp_path / "book.epub"
    out_path = tmp_path / "book.de.epub"
    _make_epub(epub_path)
    ext = extract_epub(str(epub_path))
    restored = [restore(s.text, s.placeholders) for s in ext.spans]
    build_epub(str(epub_path), str(out_path), ext, restored)

    reread = read_epub(str(out_path))
    ch1 = next(
        it
        for it in reread.get_items_of_type(ebooklib.ITEM_DOCUMENT)
        if getattr(it, "file_name", "") == "ch1.xhtml"
    )
    content = ch1.get_content().decode("utf-8")
    assert "<strong>Bob</strong>" in content
    assert "<pre>do_not_translate();</pre>" in content
    assert "<code>pip install</code>" in content


def test_extract_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        extract_epub(str(tmp_path / "nope.epub"))
