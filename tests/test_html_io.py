"""Tests for booktx.html_io: XHTML extraction and rebuild."""

from __future__ import annotations

from booktx.html_io import build_xhtml, extract_xhtml
from booktx.placeholders import restore

XHTML = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Title</title></head>
<body>
<h1>Chapter One</h1>
<p>Hello <strong>world</strong>. A second sentence here.</p>
<p>Visit <a href="https://x.org">the site</a> now.</p>
<pre>do_not_translate();</pre>
<p>Run <code>pip install</code> please.</p>
</body></html>"""


def test_extract_skips_pre_and_inline_code():
    ext = extract_xhtml(XHTML)
    joined = " ".join(s.text for s in ext.spans)
    assert "do_not_translate" not in joined
    assert "pip install" not in joined  # inline code is opaque, not translated
    # Inline code became a TAG placeholder
    assert any(
        p.kind == "tag" and "code" in p.original and "pip install" in p.original
        for span in ext.spans
        for p in span.placeholders
    )


def test_extract_inline_strong_becomes_tag():
    ext = extract_xhtml(XHTML)
    # "world" stays translatable prose; only the tags become placeholders.
    span = next(s for s in ext.spans if "Hello" in s.text)
    assert "world" in span.text
    assert "<strong>world</strong>" not in span.text
    assert any(p.original == "<strong>" for p in span.placeholders)
    assert any(p.original == "</strong>" for p in span.placeholders)


def test_extract_link_text_kept_url_hidden_in_attribute():
    ext = extract_xhtml(XHTML)
    span = next(s for s in ext.spans if "Visit" in s.text)
    assert "the site" in span.text  # link text is translatable prose
    # URL stays inside the <a> tag fragment, not in span text
    assert "https://x.org" not in span.text
    assert any(
        "href" in p.original and "https://x.org" in p.original
        for p in span.placeholders
    )


def test_extract_protects_names_globally():
    md = XHTML.replace("Hello <strong>world</strong>.", "Alice met Bob. Alice ran.")
    ext = extract_xhtml(md, protected_terms=["Alice", "Bob"])
    alice_tokens = {
        p.token
        for span in ext.spans
        for p in span.placeholders
        if p.original == "Alice"
    }
    assert len(alice_tokens) == 1


def test_template_has_spantx_markers_and_preserves_structure():
    ext = extract_xhtml(XHTML)
    assert "__SPANTX_0001__" in ext.template
    # head/title and pre survive untouched
    assert "<title>Title</title>" in ext.template
    assert "do_not_translate();" in ext.template


def test_roundtrip_identity_rebuild():
    ext = extract_xhtml(XHTML)
    restored = [restore(s.text, s.placeholders) for s in ext.spans]
    rebuilt = build_xhtml(ext.template, restored)
    # Re-parse and verify key content survived and structure intact
    assert "<title>Title</title>" in rebuilt
    assert "do_not_translate();" in rebuilt
    assert "<strong>world</strong>" in rebuilt
    assert 'href="https://x.org"' in rebuilt
    assert "Hello " in rebuilt and "A second sentence here." in rebuilt


def test_extract_headings_list_items_table_cells():
    x = """<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h2>A heading</h2>
<ul><li>First item.</li><li>Second item.</li></ul>
<table><tr><th>Col</th></tr><tr><td>Cell text.</td></tr></table>
</body></html>"""
    ext = extract_xhtml(x)
    texts = " ".join(s.text for s in ext.spans)
    assert "A heading" in texts
    assert "First item." in texts and "Second item." in texts
    assert "Cell text." in texts
