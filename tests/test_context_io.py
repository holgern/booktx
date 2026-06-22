"""Tests for booktx.context IO and rendering."""

from __future__ import annotations

from pathlib import Path

from booktx.config import init_project, load_project
from booktx.context import (
    context_markdown_path,
    context_path,
    default_context,
    load_context,
    render_context_markdown,
    seed_glossary,
    write_context,
    write_context_markdown,
)


def _project(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de", source_language="en")
    return proj


def test_default_context_uses_config_languages(tmp_path: Path):
    proj = _project(tmp_path)
    ctx = default_context(proj)
    assert ctx.source_language == "en"
    assert ctx.target_language == "de"
    assert ctx.ready is False
    assert ctx.style.target_locale == "de"
    assert any(g.source == "Lowlands" for g in ctx.glossary)


def test_write_context_creates_file(tmp_path: Path):
    proj = _project(tmp_path)
    ctx = default_context(proj, source_sha256="abc123")
    write_context(proj, ctx)
    assert context_path(proj).is_file()
    loaded = load_context(proj)
    assert loaded is not None
    assert loaded.source_sha256 == "abc123"
    assert loaded.ready is False


def test_load_context_returns_none_when_missing(tmp_path: Path):
    proj = _project(tmp_path)
    assert load_context(proj) is None


def test_write_context_markdown_renders(tmp_path: Path):
    proj = _project(tmp_path)
    ctx = default_context(proj)
    write_context_markdown(proj, ctx)
    md = context_markdown_path(proj).read_text("utf-8")
    assert "booktx translation context" in md
    assert "NOT READY" in md
    # Style section
    assert "Formality: neutral" in md
    # Glossary table with Lowlands forbidden targets
    assert "Lowlands" in md
    assert "Niederlande" in md
    # Open questions section
    assert "Open questions" in md
    assert "Q001" in md


def test_render_context_markdown_ready_status(tmp_path: Path):
    proj = _project(tmp_path)
    ctx = default_context(proj)
    ctx.ready = True
    md = render_context_markdown(ctx)
    assert "Status: READY" in md


def test_render_context_markdown_includes_answered_questions(tmp_path: Path):
    proj = _project(tmp_path)
    ctx = default_context(proj)
    q = ctx.questions[0]
    q.status = "answered"
    q.answer = "de-DE"
    md = render_context_markdown(ctx)
    assert "Answered questions" in md
    assert "de-DE" in md


def test_render_context_markdown_handles_empty_glossary(tmp_path: Path):
    proj = _project(tmp_path)
    ctx = default_context(proj)
    ctx.glossary = []
    md = render_context_markdown(ctx)
    assert "no glossary entries yet" in md


def test_context_files_round_trip(tmp_path: Path):
    proj = _project(tmp_path)
    ctx = default_context(proj)
    # Add a custom glossary entry to prove persistence beyond seeds.
    from booktx.context import GlossaryEntry

    ctx.glossary.append(
        GlossaryEntry(
            source="snapbow",
            target=None,
            forbidden_targets=[],
            category="object",
        )
    )
    write_context(proj, ctx)
    loaded = load_context(proj)
    assert loaded is not None
    sources = {g.source for g in loaded.glossary}
    assert {"Lowlands", "Lowlander", "snapbow"} <= sources


def test_seed_glossary_entries_are_open_with_forbidden(tmp_path: Path):
    proj = _project(tmp_path)
    ctx = default_context(proj)
    write_context(proj, ctx)
    loaded = load_context(proj)
    assert loaded is not None
    low = next(g for g in loaded.glossary if g.source == "Lowlands")
    assert low.status == "open"
    assert "Niederlande" in low.forbidden_targets
    assert seed_glossary()  # smoke: deterministic factory callable


def test_load_project_still_works_after_context_added(tmp_path: Path):
    # Ensure adding context files does not break project loading.
    proj = _project(tmp_path)
    ctx = default_context(proj)
    write_context(proj, ctx)
    write_context_markdown(proj, ctx)
    again = load_project(proj.root)
    assert again.root == proj.root
