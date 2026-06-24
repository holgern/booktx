from __future__ import annotations

from booktx.epub_inline_xhtml import (
    inline_skeleton,
    protect_names_in_xhtml_text_nodes,
    sanitize_target_fragment,
    strip_inline_xhtml,
)
from booktx.inline_audit import safe_migrated_target


def test_protect_names_in_xhtml_text_nodes_does_not_tokenize_href():
    text, placeholders = protect_names_in_xhtml_text_nodes(
        '<a href="https://example.invalid/Alice">Alice</a>', ["Alice"]
    )

    assert 'href="https://example.invalid/Alice"' in text
    assert ">__NAME_001__<" in text
    assert placeholders[0].original == "Alice"


def test_sanitize_target_fragment_rejects_script():
    sanitized = sanitize_target_fragment("<script>alert(1)</script>", "<em>Title</em>")

    assert any(issue.rule == "inline_xhtml_no_block_tags" for issue in sanitized.issues)


def test_strip_inline_xhtml_returns_visible_text():
    assert (
        strip_inline_xhtml("Use <code>pip install booktx</code> first.")
        == "Use pip install booktx first."
    )


def test_inline_skeleton_preserves_attrs():
    skeleton = inline_skeleton('<span class="smallcaps">Alice</span>')

    assert skeleton[0].tag == "span"
    assert skeleton[0].attrs == (("class", "smallcaps"),)


def test_migrate_inline_xhtml_wraps_full_record_emphasis_safe_case():
    assert (
        safe_migrated_target(
            "<em>Running down again – always at the worst possible moment!</em>",
            "Schon wieder am Ablaufen – immer im denkbar schlechtesten Moment!",
        )
        == "<em>Schon wieder am Ablaufen – immer im denkbar schlechtesten Moment!</em>"
    )


def test_migrate_inline_xhtml_wraps_exact_title_safe_case():
    assert (
        safe_migrated_target(
            "the <em>Esca Volenti</em> shuddered", "die Esca Volenti erbebte"
        )
        == "die <em>Esca Volenti</em> erbebte"
    )


def test_migrate_inline_xhtml_refuses_ambiguous_translated_phrase():
    assert safe_migrated_target("the <em>red ship</em>", "das rote Schiff") is None
