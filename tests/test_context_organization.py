from __future__ import annotations

from booktx.context import (
    ChapterContext,
    ContextQuestion,
    GlossaryEntry,
    TranslationContext,
)
from booktx.context_organization import (
    audit_context_organization,
    compare_profile_contexts,
    render_context_organization_report,
)


def _ctx(*, glossary=None, questions=None, chapters=None) -> TranslationContext:
    return TranslationContext(
        source_language="en",
        target_language="de",
        glossary=glossary or [],
        questions=questions or [],
        chapter_contexts=chapters or [],
    )


def _codes(issues):
    return [issue.code for issue in issues]


def test_q006_arrow_terms_reported():
    ctx = _ctx(
        glossary=[
            GlossaryEntry(
                source="wasp-kinden",
                target="Wespenart",
                require_target=True,
                enforce="error",
            )
        ],
        questions=[
            ContextQuestion(
                id="Q006",
                topic="world_terms",
                question="Terms?",
                answer="Wasp-kinden -> Wespen-Artiger",
                status="answered",
            )
        ],
    )

    issues = audit_context_organization(ctx, profile="de")

    assert "question_contains_term_arrow" in _codes(issues)
    issue = next(i for i in issues if i.code == "question_contains_term_arrow")
    assert issue.evidence["pairs"] == [
        {"source": "Wasp-kinden", "target": "Wespen-Artiger"}
    ]


def test_nonbinding_target_with_error_enforcement_reported():
    ctx = _ctx(
        glossary=[GlossaryEntry(source="empire", target="Imperium", enforce="error")]
    )

    issues = audit_context_organization(ctx)

    assert "advisory_entry_looks_binding" in _codes(issues)


def test_chapter_note_arrow_and_forbidden_target_conflict_reported():
    ctx = _ctx(
        glossary=[
            GlossaryEntry(
                source="Skater-kinden",
                target="Wasserläuferart",
                forbidden_targets=["Wasserskaterart"],
                enforce="error",
            )
        ],
        chapters=[
            ChapterContext(
                chapter_id="0006",
                decisions_added=["Skater-kinden -> Wasserskaterart"],
            )
        ],
    )

    issues = audit_context_organization(ctx)

    assert "chapter_note_contains_term_arrow" in _codes(issues)
    assert "chapter_note_conflicts_with_forbidden_target" in _codes(issues)


def test_cross_profile_missing_glossary_term_reported():
    contexts = {
        "de_a": _ctx(
            glossary=[GlossaryEntry(source="wasp-kinden", target="Wespenart")]
        ),
        "de_b": _ctx(),
    }

    issues = compare_profile_contexts(contexts)

    assert "profile_missing_glossary_term" in _codes(issues)
    issue = next(i for i in issues if i.code == "profile_missing_glossary_term")
    assert issue.profile == "de_b"


def test_cross_profile_target_divergence_reported():
    contexts = {
        "de_a": _ctx(glossary=[GlossaryEntry(source="mantis", target="Gottesanbeter")]),
        "de_b": _ctx(
            glossary=[GlossaryEntry(source="mantis", target="Gottesanbeterin")]
        ),
    }

    issues = compare_profile_contexts(contexts)

    assert "profile_glossary_target_divergence" in _codes(issues)


def test_legacy_render_layout_reported_and_markdown_report_renders():
    ctx = _ctx()

    issues = audit_context_organization(
        ctx, rendered_markdown="## Answered questions\n"
    )
    report = render_context_organization_report(issues)

    assert "context_markdown_uses_legacy_layout" in _codes(issues)
    assert "# Context organization report" in report
    assert "context_markdown_uses_legacy_layout" in report
