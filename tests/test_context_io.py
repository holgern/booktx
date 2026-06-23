"""Tests for booktx.context IO and rendering."""

from __future__ import annotations

from pathlib import Path

from booktx.config import init_project, load_project
from booktx.context import (
    ChapterContext,
    TranslationContext,
    analyze_context_markdown_drift,
    chapter_contexts_equivalent,
    chapter_map_path,
    context_markdown_path,
    context_path,
    default_context,
    ensure_context_markdown_safe_to_overwrite,
    hydrate_chapter_contexts_from_chapter_map,
    load_context,
    merge_chapter_contexts,
    parse_context_markdown_chapter_notes,
    render_context_markdown,
    seed_glossary,
    upsert_chapter_context,
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
    # Default glossary is empty (book-specific seeds loaded via --seed).
    assert ctx.glossary == []


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
    # Default glossary is empty (no book-specific terms).
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
    # Default glossary is empty; only the custom entry persists.
    assert {"snapbow"} <= sources


def test_seed_glossary_entries_are_open_with_forbidden(tmp_path: Path):
    from booktx.context import load_seed_template

    proj = _project(tmp_path)
    ctx = default_context(proj)
    # Load the Shadows-of-Apt template to get the book-specific seeds.
    extra_q, extra_g = load_seed_template("shadows_of_apt")
    ctx.questions.extend(extra_q)
    ctx.glossary.extend(extra_g)
    write_context(proj, ctx)
    loaded = load_context(proj)
    assert loaded is not None
    low = next(g for g in loaded.glossary if g.source == "Lowlands")
    assert low.status == "open"
    assert "Niederlande" in low.forbidden_targets
    assert seed_glossary() == []  # smoke: default is empty


def test_load_project_still_works_after_context_added(tmp_path: Path):
    # Ensure adding context files does not break project loading.
    proj = _project(tmp_path)
    ctx = default_context(proj)
    write_context(proj, ctx)
    write_context_markdown(proj, ctx)
    again = load_project(proj.root)
    assert again.root == proj.root


# --- chapter note parsing ---------------------------------------------------

CHAPTER_NOTES_MD = """\
# booktx translation context

## Chapter notes

### 0006 — TWO
- Source summary: Apt fights in the Lowlands.
- Translation summary: Apt kämpft in den Tieflanden.
- Decision: keep "Apt" untranslated
- Decision: voices stay gritty
- Open issue: register for Beetle

### 0007
- Source summary: Plain chapter.

## Rules for agents

- Read this file.
"""


def test_parse_chapter_notes_id_only_and_with_title():
    notes = parse_context_markdown_chapter_notes(CHAPTER_NOTES_MD)
    assert [n.chapter_id for n in notes] == ["0006", "0007"]
    assert notes[0].title == "TWO"
    assert notes[1].title == ""


def test_parse_chapter_notes_summaries_decisions_issues_and_ordering():
    notes = parse_context_markdown_chapter_notes(CHAPTER_NOTES_MD)
    first = notes[0]
    assert first.source_summary == "Apt fights in the Lowlands."
    assert first.translation_summary == "Apt kämpft in den Tieflanden."
    assert first.decisions_added == [
        'keep "Apt" untranslated',
        "voices stay gritty",
    ]
    assert first.open_issues == ["register for Beetle"]
    assert notes[1].source_summary == "Plain chapter."


def test_parse_chapter_notes_preserves_non_ascii():
    notes = parse_context_markdown_chapter_notes(CHAPTER_NOTES_MD)
    assert "kämpft" in notes[0].translation_summary
    assert "Tieflanden" in notes[0].translation_summary


def test_parse_chapter_notes_accepts_dash_like_separators():
    for sep in ("\u2014", "\u2013", "-"):
        md = f"## Chapter notes\n\n### 0006 {sep} Title\n- Decision: d\n"
        note = parse_context_markdown_chapter_notes(md)[0]
        assert note.chapter_id == "0006"
        assert note.title == "Title"
        assert note.decisions_added == ["d"]


def test_parse_chapter_notes_stops_at_next_level_two_heading():
    notes = parse_context_markdown_chapter_notes(CHAPTER_NOTES_MD)
    # Only the two chapters inside the section; nothing from "Rules for agents".
    assert len(notes) == 2
    assert all(n.chapter_id.startswith("000") for n in notes)


def test_parse_chapter_notes_missing_section_returns_empty():
    assert parse_context_markdown_chapter_notes("# Other\n\nbody\n") == []


def test_parse_chapter_notes_fails_on_unknown_bullet():
    md = "## Chapter notes\n\n### 0006\n- Wat?: something\n"
    try:
        parse_context_markdown_chapter_notes(md)
    except ValueError as exc:
        assert "0006" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown bullet")


def test_parse_chapter_notes_fails_on_malformed_heading():
    md = "## Chapter notes\n\n### 0006 no separator title\n- Decision: d\n"
    try:
        parse_context_markdown_chapter_notes(md)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for malformed heading")


# --- equivalence ------------------------------------------------------------


def test_chapter_contexts_equivalent_ignores_chunk_ids():
    left = ChapterContext(chapter_id="0006", title="TWO", chunk_ids=["0006"])
    right = ChapterContext(chapter_id="0006", title="TWO", chunk_ids=[])
    assert chapter_contexts_equivalent(left, right)


def test_chapter_contexts_equivalent_detects_decision_difference():
    left = ChapterContext(chapter_id="0006", decisions_added=["a"])
    right = ChapterContext(chapter_id="0006", decisions_added=["a", "b"])
    assert not chapter_contexts_equivalent(left, right)


# --- drift analysis ---------------------------------------------------------


def _project_with_context(
    tmp_path: Path,
    *,
    chapters: list[ChapterContext] | None = None,
    md_text: str | None = None,
):
    proj = _project(tmp_path)
    ctx = default_context(proj)
    if chapters is not None:
        ctx.chapter_contexts = list(chapters)
    write_context(proj, ctx)
    if md_text is not None:
        context_markdown_path(proj).write_text(md_text, encoding="utf-8")
    else:
        write_context_markdown(proj, ctx)
    return proj, ctx


def test_drift_reports_markdown_chapter_missing_from_json(tmp_path: Path):
    proj, ctx = _project_with_context(tmp_path, chapters=[], md_text=CHAPTER_NOTES_MD)
    drift = analyze_context_markdown_drift(proj, ctx)
    assert drift.missing_in_json == ["0006", "0007"]
    assert drift.conflicting == []
    assert drift.unsafe_to_overwrite is True


def test_drift_reports_conflicting_existing_chapter(tmp_path: Path):
    json_chapter = ChapterContext(
        chapter_id="0006",
        title="TWO",
        decisions_added=['keep "Apt" untranslated'],
    )
    proj, ctx = _project_with_context(
        tmp_path, chapters=[json_chapter], md_text=CHAPTER_NOTES_MD
    )
    drift = analyze_context_markdown_drift(proj, ctx)
    # 0006 has an extra Markdown decision; 0007 is missing entirely.
    assert drift.conflicting == ["0006"]
    assert drift.missing_in_json == ["0007"]
    assert drift.unsafe_to_overwrite is True


def test_drift_no_findings_when_equivalent(tmp_path: Path):
    proj, ctx = _project_with_context(tmp_path)  # md rendered from ctx
    drift = analyze_context_markdown_drift(proj, ctx)
    assert drift.unsafe_to_overwrite is False
    assert drift.missing_in_json == []
    assert drift.conflicting == []


def test_drift_chunk_ids_only_is_not_conflicting(tmp_path: Path):
    chapter = ChapterContext(chapter_id="0006", title="TWO", chunk_ids=["0006", "0007"])
    proj, ctx = _project_with_context(tmp_path, chapters=[chapter])
    drift = analyze_context_markdown_drift(proj, ctx)
    assert drift.conflicting == []
    assert drift.unsafe_to_overwrite is False


def test_drift_parse_errors_make_overwrite_unsafe(tmp_path: Path):
    bad_md = "## Chapter notes\n\n### 0006\n- Unknown bullet: x\n"
    proj, ctx = _project_with_context(tmp_path, chapters=[], md_text=bad_md)
    drift = analyze_context_markdown_drift(proj, ctx)
    assert drift.parse_errors
    assert drift.unsafe_to_overwrite is True


def test_drift_no_markdown_file_is_safe(tmp_path: Path):
    proj, ctx = _project_with_context(tmp_path)
    context_markdown_path(proj).unlink()
    drift = analyze_context_markdown_drift(proj, ctx)
    assert drift.unsafe_to_overwrite is False


def test_ensure_safe_to_overwrite_raises_and_can_be_forced(tmp_path: Path):
    proj, ctx = _project_with_context(tmp_path, chapters=[], md_text=CHAPTER_NOTES_MD)
    try:
        ensure_context_markdown_safe_to_overwrite(proj, ctx)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on unsafe overwrite")
    # Force flag allows the operation to proceed.
    ensure_context_markdown_safe_to_overwrite(proj, ctx, allow_discard_md_only=True)


# --- hydration from chapter map --------------------------------------------


def test_hydrate_fills_title_and_chunk_ids_when_absent(tmp_path: Path):
    proj, ctx = _project_with_context(
        tmp_path, chapters=[ChapterContext(chapter_id="0006")]
    )
    chapter_map_path(proj).write_text(
        '{"version": 1, "source_sha256": "", '
        '"chapters": [{"chapter_id": "0006", "title": "TWO", '
        '"chunk_ids": ["0006"]}]}',
        encoding="utf-8",
    )
    hydrate_chapter_contexts_from_chapter_map(proj, ctx.chapter_contexts)
    note = ctx.chapter_contexts[0]
    assert note.title == "TWO"
    assert note.chunk_ids == ["0006"]


def test_hydrate_does_not_overwrite_existing_values(tmp_path: Path):
    proj, ctx = _project_with_context(
        tmp_path,
        chapters=[ChapterContext(chapter_id="0006", title="Kept", chunk_ids=["0009"])],
    )
    chapter_map_path(proj).write_text(
        '{"version": 1, "source_sha256": "", '
        '"chapters": [{"chapter_id": "0006", "title": "Mapped", '
        '"chunk_ids": ["0006"]}]}',
        encoding="utf-8",
    )
    hydrate_chapter_contexts_from_chapter_map(proj, ctx.chapter_contexts)
    note = ctx.chapter_contexts[0]
    assert note.title == "Kept"
    assert note.chunk_ids == ["0009"]


# --- merge and upsert -------------------------------------------------------


def _ctx(chapters: list[ChapterContext] | None = None) -> TranslationContext:
    return TranslationContext(
        source_language="en",
        target_language="de",
        chapter_contexts=list(chapters or []),
    )


def test_merge_adds_absent_notes():
    ctx = _ctx()
    changed = merge_chapter_contexts(
        ctx, [ChapterContext(chapter_id="0006", title="TWO")]
    )
    assert changed == ["0006"]
    assert ctx.chapter_contexts[0].title == "TWO"


def test_merge_default_refuses_conflicting():
    ctx = _ctx([ChapterContext(chapter_id="0006", decisions_added=["a"])])
    try:
        merge_chapter_contexts(
            ctx, [ChapterContext(chapter_id="0006", decisions_added=["b"])]
        )
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on conflicting merge")


def test_merge_replace_existing_replaces_durable_fields():
    ctx = _ctx(
        [ChapterContext(chapter_id="0006", decisions_added=["a"], chunk_ids=["0006"])]
    )
    changed = merge_chapter_contexts(
        ctx,
        [ChapterContext(chapter_id="0006", decisions_added=["b"])],
        replace_existing=True,
    )
    assert changed == ["0006"]
    note = ctx.chapter_contexts[0]
    assert note.decisions_added == ["b"]
    # chunk ids preserved from existing.
    assert note.chunk_ids == ["0006"]


def test_merge_append_lists_dedupes_and_keeps_summary():
    ctx = _ctx(
        [
            ChapterContext(
                chapter_id="0006",
                source_summary="old",
                decisions_added=["a"],
            )
        ]
    )
    changed = merge_chapter_contexts(
        ctx,
        [
            ChapterContext(
                chapter_id="0006",
                source_summary="new",
                decisions_added=["a", "b"],
            )
        ],
        append_existing_lists=True,
    )
    assert changed == ["0006"]
    note = ctx.chapter_contexts[0]
    assert note.source_summary == "old"  # existing kept
    assert note.decisions_added == ["a", "b"]


def test_upsert_creates_then_appends_and_dedupes():
    ctx = _ctx()
    upsert_chapter_context(
        ctx, ChapterContext(chapter_id="0006", decisions_added=["a"])
    )
    upsert_chapter_context(
        ctx, ChapterContext(chapter_id="0006", decisions_added=["a", "b"])
    )
    assert ctx.chapter_contexts[0].decisions_added == ["a", "b"]


def test_upsert_replace_decisions_replaces_list():
    ctx = _ctx([ChapterContext(chapter_id="0006", decisions_added=["a", "b"])])
    upsert_chapter_context(
        ctx,
        ChapterContext(chapter_id="0006", decisions_added=["c"]),
        replace_decisions=True,
    )
    assert ctx.chapter_contexts[0].decisions_added == ["c"]


def test_upsert_replaces_open_issues_with_flag():
    ctx = _ctx([ChapterContext(chapter_id="0006", open_issues=["x"])])
    upsert_chapter_context(
        ctx,
        ChapterContext(chapter_id="0006", open_issues=["y"]),
        replace_open_issues=True,
    )
    assert ctx.chapter_contexts[0].open_issues == ["y"]
