"""Unit tests for booktx.qa_scan pure functions and finding construction."""

from __future__ import annotations

from booktx.qa_scan import (
    QaScanFinding,
    QaScanResult,
    _contains_term,
    build_language_leftover_words,
)

# --- _contains_term ---------------------------------------------------------


def test_contains_term_case_insensitive():
    assert _contains_term("Hello World", "hello", case_sensitive=False) is True
    assert _contains_term("Hello World", "HELLO", case_sensitive=False) is True
    assert _contains_term("Hello World", "goodbye", case_sensitive=False) is False


def test_contains_term_case_sensitive():
    assert _contains_term("Hello World", "Hello", case_sensitive=True) is True
    assert _contains_term("Hello World", "hello", case_sensitive=True) is False
    assert _contains_term("Hello World", "world", case_sensitive=True) is False
    assert _contains_term("Hello World", "World", case_sensitive=True) is True


def test_contains_term_empty():
    assert _contains_term("", "", case_sensitive=False) is True
    assert _contains_term("text", "", case_sensitive=False) is True


def test_contains_term_substring():
    assert _contains_term("Wespenartigen", "Wespenart", case_sensitive=False) is True


# --- QaScanFinding ----------------------------------------------------------


def test_qa_scan_finding_construction():
    f = QaScanFinding(
        id="0011-000001",
        chapter_id="0005",
        rule="forbidden_target",
        term="Wespenartigen",
        source="wasp-kinden",
        target="Wespenartigen",
    )
    d = f.as_dict()
    assert d["id"] == "0011-000001"
    assert d["rule"] == "forbidden_target"
    assert d["term"] == "Wespenartigen"
    assert d["source"] == "wasp-kinden"
    assert d["target"] == "Wespenartigen"


def test_qa_scan_finding_as_dict_serializable():
    import json

    f = QaScanFinding(
        id="0001-000001",
        chapter_id="01",
        rule="pattern_match",
        term="[A-Za-z]+-Artig",
    )
    data = json.dumps(f.as_dict())
    parsed = json.loads(data)
    assert parsed["rule"] == "pattern_match"


# --- QaScanResult -----------------------------------------------------------


def test_qa_scan_result_empty():
    r = QaScanResult()
    assert r.findings == []
    assert r.records_scanned == 0
    assert r.findings_count == 0


def test_qa_scan_result_with_findings():
    r = QaScanResult(
        findings=[
            QaScanFinding(id="01", chapter_id="01", rule="forbidden_target"),
            QaScanFinding(id="02", chapter_id="01", rule="glossary_mismatch"),
        ],
        records_scanned=100,
        findings_count=2,
    )
    assert len(r.findings) == 2
    assert r.records_scanned == 100


# --- build_language_leftover_words ------------------------------------------


def test_build_language_leftover_words_defaults():
    words = build_language_leftover_words()
    assert "the" in words
    assert "and" in words
    assert "for" in words
    assert "with" in words
    assert "wasp" not in words  # not in default set


def test_build_language_leftover_words_custom():
    words = build_language_leftover_words(["wasp", "KINDEN"])
    assert "the" in words  # still has defaults
    assert "wasp" in words
    assert "kinden" in words  # lowercased


# --- _qa_scan integration (lightweight) -------------------------------------
# Full integration tests require a project setup with store; those belong in
# test_cli_review.py. Here we test the pure function logic.


def test_qa_scan_finding_rule_values_are_consistent():
    """Ensure finding.as_dict() produces valid JSON for all rule types."""
    import json

    for rule in (
        "forbidden_target",
        "glossary_mismatch",
        "target_contains",
        "pattern_match",
        "language_leftover",
    ):
        f = QaScanFinding(id="01", chapter_id="01", rule=rule, term="test")
        data = json.dumps(f.as_dict())
        parsed = json.loads(data)
        assert parsed["rule"] == rule
