"""Tests for booktx.markdown_io: extraction and rebuild."""

from __future__ import annotations

from booktx.markdown_io import (
    build_markdown,
    extract_markdown,
    split_front_matter,
)


def test_split_front_matter_present():
    fm, body = split_front_matter("---\ntitle: T\nauthor: A\n---\n\n# H\n")
    assert fm.startswith("---") and fm.rstrip().endswith("---")
    assert body.strip().startswith("# H")


def test_split_front_matter_absent():
    fm, body = split_front_matter("# No front matter\n")
    assert fm == ""
    assert body == "# No front matter\n"


def test_extract_skips_fenced_code():
    md = "Hello world.\n\n```python\nprint('hi')\n```\n\nAnother sentence here.\n"
    ext = extract_markdown(md)
    # Code must not appear inside any span
    for span in ext.spans:
        assert "print" not in span.text
    # Template still contains the code block verbatim
    assert "```python" in ext.template
    assert "print('hi')" in ext.template


def test_extract_hides_inline_code_as_tag():
    md = "Run `pip install` now.\n"
    ext = extract_markdown(md)
    assert len(ext.spans) == 1
    span = ext.spans[0]
    assert "`pip install`" not in span.text
    assert any(
        p.original == "`pip install`" and p.kind == "tag" for p in span.placeholders
    )


def test_extract_hides_link_url_keeps_text():
    md = "Visit [the site](https://example.com) today.\n"
    ext = extract_markdown(md)
    span = ext.spans[0]
    assert "https://example.com" not in span.text
    assert "the site" in span.text  # link text is translatable prose
    assert any(
        p.original == "https://example.com" and p.kind == "tag"
        for p in span.placeholders
    )


def test_extract_protects_names_globally_consistent():
    md = "Alice ran home. Then Alice slept.\n\nBob met Alice.\n"
    ext = extract_markdown(md, protected_terms=["Alice", "Bob"])
    # Alice should have the SAME token across both paragraphs.
    alice_tokens = {
        p.token
        for span in ext.spans
        for p in span.placeholders
        if p.original == "Alice"
    }
    assert len(alice_tokens) == 1
    # And the token is present in each span where Alice appears
    spans_with_alice = [
        s for s in ext.spans if "Alice" not in s.text and "__NAME_" in s.text
    ]
    assert len(spans_with_alice) == 2


def test_extract_front_matter_not_translated_but_preserved():
    md = "---\ntitle: My Book\n---\n\n# Real heading\n\nSome prose here.\n"
    ext = extract_markdown(md)
    assert ext.front_matter.startswith("---")
    # The title key/value is NOT extracted as a span
    assert all("My Book" not in s.text for s in ext.spans)
    # But it survives in the template
    assert "title: My Book" in ext.template


def test_template_has_spantx_placeholders():
    md = "First paragraph.\n\nSecond paragraph.\n"
    ext = extract_markdown(md)
    assert "__SPANTX_0001__" in ext.template
    assert "__SPANTX_0002__" in ext.template
    assert len(ext.spans) == 2


def test_roundtrip_identity_rebuild():
    md = (
        "# Title\n\n"
        "Hello **world**. Visit [us](https://x.org) and run `code`.\n\n"
        "- Item one.\n"
        "- Item two has more.\n\n"
        "```py\npass\n```\n"
    )
    ext = extract_markdown(md, protected_terms=[])
    # Identity translation = restore placeholders into each span text before rebuild.
    from booktx.placeholders import restore

    restored = [restore(s.text, s.placeholders) for s in ext.spans]
    rebuilt = build_markdown(ext.template, restored)
    assert rebuilt == md


def test_roundtrip_with_names_and_tags_restored():
    # Names/tags are restored by build.py; here we feed restored text in.
    md = "Alice ran `code` quickly.\n"
    ext = extract_markdown(md, protected_terms=["Alice"])
    span = ext.spans[0]
    # Restore placeholders into the tagged text (identity translation)
    from booktx.placeholders import restore

    restored = restore(span.text, span.placeholders)
    rebuilt = build_markdown(ext.template, [restored])
    assert rebuilt == md


def test_extract_tables_and_blockquotes():
    md = "> A quoted sentence here.\n\n| H1 | H2 |\n|----|----|\n| c1 | c2 |\n"
    ext = extract_markdown(md)
    texts = " ".join(s.text for s in ext.spans)
    assert "quoted sentence" in texts
    assert "c1" in texts and "c2" in texts
