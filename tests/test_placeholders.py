"""Tests for booktx.placeholders: protect/restore names and inline tags."""

from __future__ import annotations

from booktx.models import Placeholder
from booktx.placeholders import (
    collect_tokens,
    protect_names,
    protect_tags,
    restore,
)


def test_protect_names_basic():
    res = protect_names(
        "Alice looked at Mr. Smith on Baker Street.",
        ["Alice", "Mr. Smith", "Baker Street"],
    )
    assert res.text == "__NAME_001__ looked at __NAME_002__ on __NAME_003__."
    assert [p.original for p in res.placeholders] == [
        "Alice",
        "Mr. Smith",
        "Baker Street",
    ]
    assert all(p.kind == "name" for p in res.placeholders)


def test_protect_names_longest_first():
    # "Mr. Smith" must win over a hypothetical "Mr." entry.
    res = protect_names("Hello Mr. Smith.", ["Mr.", "Mr. Smith"])
    assert res.text == "Hello __NAME_001__."
    assert res.placeholders[0].original == "Mr. Smith"


def test_protect_names_reuses_token_for_repeated_term():
    res = protect_names("Alice met Alice again.", ["Alice"])
    assert res.text == "__NAME_001__ met __NAME_001__ again."
    assert len(res.placeholders) == 1


def test_protect_names_skips_missing_and_empty():
    res = protect_names("Hello world.", ["", "nowhere", "world"])
    assert res.text == "Hello __NAME_001__."
    assert [p.original for p in res.placeholders] == ["world"]


def test_protect_tags_replaces_spans():
    res = protect_tags(
        "Run `pip install` then visit https://x.org.",
        ["`pip install`", "https://x.org"],
    )
    assert "__TAG_001__" in res.text and "__TAG_002__" in res.text
    assert [p.kind == "tag" for p in res.placeholders]


def test_roundtrip_names():
    original = "Alice looked at Mr. Smith on Baker Street."
    res = protect_names(original, ["Alice", "Mr. Smith", "Baker Street"])
    assert restore(res.text, res.placeholders) == original


def test_roundtrip_tags():
    original = "Use `code` and https://example.com here."
    res = protect_tags(original, ["`code`", "https://example.com"])
    assert restore(res.text, res.placeholders) == original


def test_roundtrip_combined_then_segmented():
    # names -> tags -> identity "translation" -> restore tags -> restore names
    text = "Alice ran `print(1)` quickly."
    r1 = protect_names(text, ["Alice"])
    r2 = protect_tags(r1.text, ["`print(1)`"])
    merged = r1.placeholders + r2.placeholders
    restored = restore(r2.text, merged)
    assert restored == text


def test_restore_is_verbatim_and_token_unique():
    text = "Alice and Bob met Alice."
    res = protect_names(text, ["Alice", "Bob"])
    # The translated form keeps tokens; build restores them.
    back = restore(res.text, res.placeholders)
    assert back == text


def test_restore_leaves_unknown_tokens():
    # If the agent dropped a placeholder, restore leaves the token behind so the
    # validator can flag it (build does not silently invent text).
    res = protect_names("Alice went home.", ["Alice"])
    broken = res.text.replace("__NAME_001__", "")  # agent removed it
    assert restore(broken, res.placeholders) == " went home."


def test_collect_tokens_in_order():
    res = protect_names("a b a", ["a", "b"])
    assert collect_tokens(res.text) == ["__NAME_001__", "__NAME_002__", "__NAME_001__"]


def test_placeholder_model_fields():
    p = Placeholder(token="__NAME_001__", original="Alice", kind="name")
    assert p.token == "__NAME_001__"
    assert p.original == "Alice"
    assert p.kind == "name"
