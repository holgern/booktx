"""Tests for booktx.context models."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from booktx.context import (
    ContextQuestion,
    GlossaryEntry,
    StyleProfile,
    TranslationContext,
    seed_glossary,
    seed_questions,
)


def _minimal_context() -> TranslationContext:
    return TranslationContext(
        source_language="en",
        target_language="de",
    )


def test_translation_context_roundtrips_through_json():
    ctx = _minimal_context()
    ctx.glossary.append(
        GlossaryEntry(
            source="Lowlands",
            forbidden_targets=["Niederlande"],
            category="place",
            enforce="error",
        )
    )
    ctx.questions.append(
        ContextQuestion(id="Q001", topic="locale", question="Which locale?")
    )
    js = ctx.model_dump_json()
    back = TranslationContext.model_validate_json(js)
    assert back == ctx
    assert back.glossary[0].forbidden_targets == ["Niederlande"]
    assert back.questions[0].id == "Q001"


def test_extra_fields_are_forbidden():
    with pytest.raises(ValidationError):
        GlossaryEntry.model_validate({"source": "X", "unexpected": True})
    with pytest.raises(ValidationError):
        StyleProfile.model_validate({"target_locale": "de-DE", "x": 1})
    with pytest.raises(ValidationError):
        ContextQuestion.model_validate(
            {"id": "Q1", "topic": "t", "question": "q", "z": 0}
        )
    with pytest.raises(ValidationError):
        TranslationContext.model_validate(
            {"source_language": "en", "target_language": "de", "z": 0}
        )


def test_ready_defaults_to_false():
    assert _minimal_context().ready is False


def test_glossary_entry_supports_forbidden_targets():
    entry = GlossaryEntry(
        source="Lowlands",
        forbidden_targets=["Niederlande", "die Niederlande", "Holland"],
        enforce="error",
    )
    assert entry.forbidden_targets == ["Niederlande", "die Niederlande", "Holland"]
    assert entry.enforce == "error"
    assert entry.target is None
    assert entry.status == "open"


def test_glossary_entry_enforce_default_is_warn():
    entry = GlossaryEntry(source="X")
    assert entry.enforce == "warn"
    assert entry.forbidden_targets == []


def test_context_question_required_defaults_to_true():
    q = ContextQuestion(id="Q1", topic="t", question="q")
    assert q.required is True
    assert q.status == "open"
    assert q.answer is None


def test_seed_questions_has_twelve_entries_and_required_marking():
    qs = seed_questions()
    assert len(qs) == 12
    required_ids = {q.id for q in qs if q.required}
    # locale, overall style, register, dialogue, names, world terms, kinden,
    # honorifics, place/lowlands, glossary enforcement are required.
    assert {
        "Q001",
        "Q002",
        "Q003",
        "Q004",
        "Q005",
        "Q006",
        "Q007",
        "Q008",
        "Q009",
        "Q012",
    } <= required_ids
    # typography (Q010) and units (Q011) are optional.
    optional_ids = {q.id for q in qs if not q.required}
    assert optional_ids == {"Q010", "Q011"}
    assert all(q.status == "open" for q in qs)


def test_seed_glossary_marks_lowlands_and_lowlander_open():
    glossary = seed_glossary()
    by_source = {g.source: g for g in glossary}
    assert set(by_source) == {"Lowlands", "Lowlander"}
    low = by_source["Lowlands"]
    assert low.target is None
    assert low.status == "open"
    assert "Niederlande" in low.forbidden_targets
    assert low.enforce == "error"
    lowr = by_source["Lowlander"]
    assert "Niederländer" in lowr.forbidden_targets
    assert lowr.enforce == "error"


def test_context_json_is_valid_json_object():
    ctx = _minimal_context()
    data = json.loads(ctx.model_dump_json())
    assert isinstance(data, dict)
    # Authoritative fields are present.
    for key in (
        "version",
        "source_language",
        "target_language",
        "ready",
        "style",
        "global_rules",
        "glossary",
        "questions",
        "chapter_contexts",
    ):
        assert key in data
