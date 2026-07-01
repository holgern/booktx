"""CLI tests for `booktx context ...`."""

from __future__ import annotations

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from booktx.cli import app
from booktx.config import create_profile, load_profile_project, load_project
from booktx.context import context_markdown_path, context_path, load_context

runner = CliRunner(env={"COLUMNS": "120"})


def _ansi_clean(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


MARKDOWN_DOC = """\
# One

The Wasp Empire has commenced its great war against the Lowlands.
"""


def _make_project(tmp_path: Path) -> Path:
    project_dir = tmp_path / "book"
    src = tmp_path / "novel.md"
    src.write_text(MARKDOWN_DOC, encoding="utf-8")
    res = runner.invoke(
        app,
        ["init", str(project_dir), "--target", "de", "--source-file", str(src)],
    )
    assert res.exit_code == 0, res.output
    return project_dir


def _ctx_json(project_dir: Path) -> Path:
    return context_path(load_project(project_dir))


def _ctx_md(project_dir: Path) -> Path:
    return context_markdown_path(load_project(project_dir))


def test_context_init_non_interactive_creates_files(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    res = runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    assert res.exit_code == 0, res.output
    ctx_path = _ctx_json(project_dir)
    md_path = _ctx_md(project_dir)
    assert ctx_path.is_file()
    assert md_path.is_file()
    data = json.loads(ctx_path.read_text("utf-8"))
    assert data["ready"] is False
    # 9 generic questions (Q007-Q009 moved to --seed template)
    assert len(data["questions"]) == 9
    # Default glossary is empty (book-specific seeds loaded via --seed).
    assert data["glossary"] == []


def test_context_status_reports_not_ready_when_required_open(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    res = runner.invoke(app, ["context", "status", str(project_dir)])
    assert res.exit_code == 0, res.output
    assert "NOT READY" in res.output
    assert "open_required=7" in res.output


def test_context_add_term_persists_glossary_entry(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    res = runner.invoke(
        app,
        [
            "context",
            "add-term",
            str(project_dir),
            "Lowlands",
            "--forbid",
            "Niederlande",
            "--forbid",
            "Niederländer",
            "--enforce",
            "error",
        ],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(_ctx_json(project_dir).read_text("utf-8"))
    low = next(g for g in data["glossary"] if g["source"] == "Lowlands")
    assert "Niederländer" in low["forbidden_targets"]
    assert low["enforce"] == "error"
    assert "Niederländer" in _ctx_md(project_dir).read_text("utf-8")


def test_context_mark_ready_fails_until_required_answers_exist(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    res = runner.invoke(app, ["context", "mark-ready", str(project_dir)])
    assert res.exit_code == 1
    assert "required questions" in res.output

    # Only generic required questions (Q007-Q009 are in --seed template).
    required_ids = (
        "Q001",
        "Q002",
        "Q003",
        "Q004",
        "Q005",
        "Q006",
        "Q012",
    )
    for qid in required_ids:
        ans = runner.invoke(
            app,
            ["context", "answer", str(project_dir), qid, "--text", f"answer {qid}"],
        )
        assert ans.exit_code == 0, ans.output
    res2 = runner.invoke(app, ["context", "mark-ready", str(project_dir)])
    assert res2.exit_code == 0, res2.output
    data = json.loads(_ctx_json(project_dir).read_text("utf-8"))
    assert data["ready"] is True
    assert "READY" in _ctx_md(project_dir).read_text("utf-8")


def test_context_mark_ready_force_allows_open_questions(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    res = runner.invoke(
        app,
        [
            "context",
            "mark-ready",
            str(project_dir),
            "--force",
            "--reason",
            "test override",
        ],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(_ctx_json(project_dir).read_text("utf-8"))
    assert data["ready"] is True


def test_context_render_regenerates_markdown(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    md_path = _ctx_md(project_dir)
    md_path.write_text("stale", encoding="utf-8")
    res = runner.invoke(app, ["context", "render", str(project_dir), "--write"])
    assert res.exit_code == 0, res.output
    assert "booktx translation context" in md_path.read_text("utf-8")


def test_context_answers_hydrate_style_profile_in_markdown(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])

    answers = [
        ("Q001", "de-DE"),
        ("Q002", "balanced"),
        ("Q003", "neutral"),
        ("Q004", "natural dialogue; keep meaning"),
        ("Q010", "German quotation marks; preserve dashes and italics"),
        ("Q011", "feet -> Fuß, miles -> Meilen"),
    ]
    for qid, text in answers:
        res = runner.invoke(
            app,
            ["context", "answer", str(project_dir), qid, "--text", text],
        )
        assert res.exit_code == 0, res.output

    render = runner.invoke(app, ["context", "render", str(project_dir), "--write"])
    assert render.exit_code == 0, render.output

    markdown = _ctx_md(project_dir).read_text("utf-8")
    assert "Target locale: de-DE" in markdown
    assert "- Prose style: balanced" in markdown
    assert "- Register: neutral" in markdown
    assert "- Dialogue: natural dialogue; keep meaning" in markdown
    assert (
        "- Punctuation: German quotation marks; preserve dashes and italics" in markdown
    )
    assert "- Units: feet -> Fuß, miles -> Meilen" in markdown


# --- context render (dry run / --stdout / guarded --write) ------------------


def _stale_markdown(project_dir: Path) -> None:
    _ctx_md(project_dir).write_text("stale content", encoding="utf-8")


def _inject_md_only_note(
    project_dir: Path,
    chapter_id: str,
    *,
    title: str = "",
    decisions: list[str] | None = None,
    issues: list[str] | None = None,
) -> None:
    md_path = _ctx_md(project_dir)
    md = md_path.read_text("utf-8")
    heading = f"### {chapter_id}"
    if title:
        heading += f" \u2014 {title}"
    block_lines = [heading]
    for dec in decisions or []:
        block_lines.append(f"- Decision: {dec}")
    for issue in issues or []:
        block_lines.append(f"- Open issue: {issue}")
    block = "\n".join(block_lines) + "\n"
    if "## Chapter notes" in md:
        # Insert before the next level-2 heading that ends the section.
        idx = md.index("## Chapter notes")
        tail = md[idx:]
        next_heading = tail.find("\n## ", 1)
        insert_at = idx + next_heading + 1 if next_heading != -1 else len(md)
        md = md[:insert_at] + "\n" + block + md[insert_at:]
    else:
        section = "## Chapter notes\n\n" + block + "\n"
        md = md.replace("## Rules for agents", section + "## Rules for agents", 1)
    md_path.write_text(md, encoding="utf-8")


def test_context_render_help_includes_write_flag():
    res = runner.invoke(app, ["context", "render", "--help"])
    assert res.exit_code == 0, res.output
    clean = _ansi_clean(res.output)
    assert "--write" in clean
    assert "--stdout" in clean
    assert "--force-discard-md-only" in clean


def test_context_render_dry_run_does_not_overwrite_stale(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    _stale_markdown(project_dir)
    res = runner.invoke(app, ["context", "render", str(project_dir)])
    assert res.exit_code == 0, res.output
    assert _ctx_md(project_dir).read_text("utf-8") == "stale content"


def test_context_render_stdout_does_not_overwrite(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    _stale_markdown(project_dir)
    res = runner.invoke(app, ["context", "render", str(project_dir), "--stdout"])
    assert res.exit_code == 0, res.output
    assert "booktx translation context" in res.output
    assert _ctx_md(project_dir).read_text("utf-8") == "stale content"


def test_context_render_write_updates_when_safe(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    _stale_markdown(project_dir)
    res = runner.invoke(app, ["context", "render", str(project_dir), "--write"])
    assert res.exit_code == 0, res.output
    assert "booktx translation context" in (_ctx_md(project_dir)).read_text("utf-8")


def test_context_render_write_refuses_missing_md_only_note(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    _inject_md_only_note(project_dir, "0099", decisions=["md-only"])
    res = runner.invoke(app, ["context", "render", str(project_dir), "--write"])
    assert res.exit_code == 1, res.output
    assert "0099" in res.output
    # Markdown-only note survives the refusal.
    assert "0099" in _ctx_md(project_dir).read_text("utf-8")


def test_context_render_write_refuses_conflicting_chapter(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    _chapter_note(project_dir, "0006", "json-only")
    _inject_md_only_note(project_dir, "0006", title="TWO", decisions=["md-extra"])
    res = runner.invoke(app, ["context", "render", str(project_dir), "--write"])
    assert res.exit_code == 1, res.output
    assert "0006" in res.output


def test_context_render_write_force_discard_overwrites(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    _inject_md_only_note(project_dir, "0099", decisions=["md-only"])
    res = runner.invoke(
        app,
        [
            "context",
            "render",
            str(project_dir),
            "--write",
            "--force-discard-md-only",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "0099" not in _ctx_md(project_dir).read_text("utf-8")


# --- context import-md ------------------------------------------------------


def _load_chapters(project_dir: Path) -> list:
    data = json.loads(_ctx_json(project_dir).read_text("utf-8"))
    return data["chapter_contexts"]


def _ctx_md(project_dir: Path) -> Path:
    return context_markdown_path(load_project(project_dir))


def _chapter_note(project_dir: Path, chapter_id: str, *decisions: str):
    args = ["context", "chapter-note", str(project_dir), chapter_id]
    for dec in decisions:
        args += ["--decision", dec]
    return runner.invoke(app, args)


def test_import_md_dry_run_reports_and_does_not_write(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    _inject_md_only_note(project_dir, "0099", decisions=["md-only"])
    before = _ctx_json(project_dir).read_text("utf-8")
    res = runner.invoke(app, ["context", "import-md", str(project_dir)])
    assert res.exit_code == 0, res.output
    assert "0099" in res.output
    assert _ctx_json(project_dir).read_text("utf-8") == before


def test_import_md_write_adds_missing_chapter(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    _inject_md_only_note(project_dir, "0099", title="Rogue", decisions=["md-only"])
    res = runner.invoke(app, ["context", "import-md", str(project_dir), "--write"])
    assert res.exit_code == 0, res.output
    chapters = _load_chapters(project_dir)
    note = next(c for c in chapters if c["chapter_id"] == "0099")
    assert note["title"] == "Rogue"
    assert note["decisions_added"] == ["md-only"]


def test_import_md_hydrates_from_chapter_map(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    _inject_md_only_note(project_dir, "0006", decisions=["d"])
    (project_dir / ".booktx" / "chapter-map.json").write_text(
        '{"version": 1, "source_sha256": "", '
        '"chapters": [{"chapter_id": "0006", "title": "Mapped", '
        '"chunk_ids": ["0006"]}]}',
        encoding="utf-8",
    )
    res = runner.invoke(app, ["context", "import-md", str(project_dir), "--write"])
    assert res.exit_code == 0, res.output
    note = next(c for c in _load_chapters(project_dir) if c["chapter_id"] == "0006")
    assert note["title"] == "Mapped"
    assert note["chunk_ids"] == ["0006"]


def test_import_md_refuses_conflicting_default(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    _chapter_note(project_dir, "0006", "json-only")
    _inject_md_only_note(project_dir, "0006", title="TWO", decisions=["md-extra"])
    res = runner.invoke(app, ["context", "import-md", str(project_dir), "--write"])
    assert res.exit_code == 1, res.output
    assert "0006" in res.output


def test_import_md_replace_existing_replaces_durable(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    _chapter_note(project_dir, "0006", "json-only")
    _inject_md_only_note(project_dir, "0006", title="TWO", decisions=["md-extra"])
    res = runner.invoke(
        app,
        [
            "context",
            "import-md",
            str(project_dir),
            "--write",
            "--replace-existing",
        ],
    )
    assert res.exit_code == 0, res.output
    note = next(c for c in _load_chapters(project_dir) if c["chapter_id"] == "0006")
    assert note["title"] == "TWO"
    assert note["decisions_added"] == ["md-extra"]


def test_import_md_append_existing_lists_appends(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    runner.invoke(
        app,
        ["context", "chapter-note", str(project_dir), "0006", "--decision", "keep"],
    )
    _inject_md_only_note(project_dir, "0006", decisions=["keep", "new"])
    res = runner.invoke(
        app,
        [
            "context",
            "import-md",
            str(project_dir),
            "--write",
            "--append-existing-lists",
        ],
    )
    assert res.exit_code == 0, res.output
    note = next(c for c in _load_chapters(project_dir) if c["chapter_id"] == "0006")
    assert note["decisions_added"] == ["keep", "new"]


def test_import_md_modes_are_mutually_exclusive(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    res = runner.invoke(
        app,
        [
            "context",
            "import-md",
            str(project_dir),
            "--replace-existing",
            "--append-existing-lists",
        ],
    )
    assert res.exit_code == 1, res.output
    assert "mutually exclusive" in res.output


# --- context chapter-note ---------------------------------------------------


def test_chapter_note_creates_and_renders(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    res = runner.invoke(
        app,
        [
            "context",
            "chapter-note",
            str(project_dir),
            "0006",
            "--title",
            "TWO",
            "--decision",
            "keep Apt",
        ],
    )
    assert res.exit_code == 0, res.output
    note = next(c for c in _load_chapters(project_dir) if c["chapter_id"] == "0006")
    assert note["title"] == "TWO"
    assert note["decisions_added"] == ["keep Apt"]
    assert "### 0006" in _ctx_md(project_dir).read_text("utf-8")


def test_chapter_note_repeated_decisions_append_in_order(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    res = runner.invoke(
        app,
        [
            "context",
            "chapter-note",
            str(project_dir),
            "0006",
            "--decision",
            "first",
            "--decision",
            "second",
        ],
    )
    assert res.exit_code == 0, res.output
    note = next(c for c in _load_chapters(project_dir) if c["chapter_id"] == "0006")
    assert note["decisions_added"] == ["first", "second"]


def test_chapter_note_duplicate_decisions_not_duplicated(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    runner.invoke(
        app,
        ["context", "chapter-note", str(project_dir), "0006", "--decision", "same"],
    )
    res = runner.invoke(
        app,
        ["context", "chapter-note", str(project_dir), "0006", "--decision", "same"],
    )
    assert res.exit_code == 0, res.output
    note = next(c for c in _load_chapters(project_dir) if c["chapter_id"] == "0006")
    assert note["decisions_added"] == ["same"]


def test_chapter_note_preserves_decisions_by_default(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    runner.invoke(
        app,
        ["context", "chapter-note", str(project_dir), "0006", "--decision", "first"],
    )
    res = runner.invoke(
        app,
        ["context", "chapter-note", str(project_dir), "0006", "--decision", "second"],
    )
    assert res.exit_code == 0, res.output
    note = next(c for c in _load_chapters(project_dir) if c["chapter_id"] == "0006")
    assert note["decisions_added"] == ["first", "second"]


def test_chapter_note_replace_decisions(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    runner.invoke(
        app,
        ["context", "chapter-note", str(project_dir), "0006", "--decision", "first"],
    )
    res = runner.invoke(
        app,
        [
            "context",
            "chapter-note",
            str(project_dir),
            "0006",
            "--decision",
            "only",
            "--replace-decisions",
        ],
    )
    assert res.exit_code == 0, res.output
    note = next(c for c in _load_chapters(project_dir) if c["chapter_id"] == "0006")
    assert note["decisions_added"] == ["only"]


def test_chapter_note_preserves_open_issues_by_default(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    runner.invoke(
        app,
        ["context", "chapter-note", str(project_dir), "0006", "--open-issue", "one"],
    )
    res = runner.invoke(
        app,
        ["context", "chapter-note", str(project_dir), "0006", "--open-issue", "two"],
    )
    assert res.exit_code == 0, res.output
    note = next(c for c in _load_chapters(project_dir) if c["chapter_id"] == "0006")
    assert note["open_issues"] == ["one", "two"]


def test_chapter_note_replace_open_issues(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    runner.invoke(
        app,
        ["context", "chapter-note", str(project_dir), "0006", "--open-issue", "one"],
    )
    res = runner.invoke(
        app,
        [
            "context",
            "chapter-note",
            str(project_dir),
            "0006",
            "--open-issue",
            "only",
            "--replace-open-issues",
        ],
    )
    assert res.exit_code == 0, res.output
    note = next(c for c in _load_chapters(project_dir) if c["chapter_id"] == "0006")
    assert note["open_issues"] == ["only"]


def test_chapter_note_refuses_unsafe_markdown_drift(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    _inject_md_only_note(project_dir, "0099", decisions=["md-only"])
    res = runner.invoke(
        app,
        ["context", "chapter-note", str(project_dir), "0006", "--decision", "x"],
    )
    assert res.exit_code == 1, res.output
    assert "0099" in res.output
    # context.json must not have been written.
    assert not any(c["chapter_id"] == "0006" for c in _load_chapters(project_dir))


def test_context_recommend_does_not_make_question_answered(tmp_path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    res = runner.invoke(
        app,
        ["context", "recommend", str(project_dir), "Q002", "--text", "Fluent literary"],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(_ctx_json(project_dir).read_text("utf-8"))
    q = next(q for q in data["questions"] if q["id"] == "Q002")
    assert q["status"] == "recommended"
    assert q["recommendation"] == "Fluent literary"
    assert q.get("answer") is None


def test_mark_ready_refuses_recommended_but_unapproved_required_question(tmp_path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    for qid in ["Q001", "Q003", "Q004", "Q005", "Q006", "Q012"]:
        runner.invoke(
            app,
            ["context", "approve", str(project_dir), qid, "--text", f"answer {qid}"],
        )
    runner.invoke(
        app,
        ["context", "recommend", str(project_dir), "Q002", "--text", "Fluent literary"],
    )
    res = runner.invoke(app, ["context", "mark-ready", str(project_dir)])
    assert res.exit_code == 1
    assert "unapproved" in res.output or "unresolved" in res.output
    assert "Q002" in res.output


def test_context_approve_use_recommendation_marks_answer_user_approved(tmp_path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    runner.invoke(
        app,
        ["context", "recommend", str(project_dir), "Q002", "--text", "Fluent literary"],
    )
    res = runner.invoke(
        app,
        [
            "context",
            "approve",
            str(project_dir),
            "Q002",
            "--use-recommendation",
            "--approved-by",
            "user:test",
        ],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(_ctx_json(project_dir).read_text("utf-8"))
    q = next(q for q in data["questions"] if q["id"] == "Q002")
    assert q["answer"] == "Fluent literary"
    assert q["answer_source"] == "user"
    assert q["approved_by"] == "user:test"


def test_context_add_question_creates_required_dynamic_question(tmp_path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    res = runner.invoke(
        app,
        [
            "context",
            "add-question",
            str(project_dir),
            "--topic",
            "poetry",
            "--question",
            "How should poems and songs be translated?",
            "--required",
            "--origin",
            "agent_review",
            "--recommendation",
            "Translate poetically, preserve meter only if natural.",
        ],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(_ctx_json(project_dir).read_text("utf-8"))
    q = next(q for q in data["questions"] if q["topic"] == "poetry")
    assert q["required"] is True
    assert q["origin"] == "agent_review"
    assert q["status"] == "recommended"


# --- add-term replacement semantics ----------------------------------------


def test_context_add_term_forbid_replaces_existing_list_and_prunes_target(
    tmp_path: Path,
):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    # First call: target Reich, forbid Imperium
    res1 = runner.invoke(
        app,
        [
            "context",
            "add-term",
            str(project_dir),
            "empire",
            "--target",
            "Reich",
            "--forbid",
            "Imperium",
        ],
    )
    assert res1.exit_code == 0, res1.output
    data1 = json.loads(_ctx_json(project_dir).read_text("utf-8"))
    entry1 = next(g for g in data1["glossary"] if g["source"] == "empire")
    assert entry1["target"] == "Reich"
    assert entry1["forbidden_targets"] == ["Imperium"]
    # Second call: target Imperium, forbid Reich and Empire.
    # Should replace forbidden list and prune Imperium (now the target).
    res2 = runner.invoke(
        app,
        [
            "context",
            "add-term",
            str(project_dir),
            "empire",
            "--target",
            "Imperium",
            "--forbid",
            "Reich",
            "--forbid",
            "Empire",
        ],
    )
    assert res2.exit_code == 0, res2.output
    data2 = json.loads(_ctx_json(project_dir).read_text("utf-8"))
    entry2 = next(g for g in data2["glossary"] if g["source"] == "empire")
    assert entry2["target"] == "Imperium"
    assert sorted(entry2["forbidden_targets"]) == ["Empire", "Reich"]


def test_context_add_term_append_forbid_is_explicit(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    # Initial: forbid A
    runner.invoke(
        app,
        [
            "context",
            "add-term",
            str(project_dir),
            "test",
            "--target",
            "T",
            "--forbid",
            "A",
        ],
    )
    # Append B without replacing A
    res = runner.invoke(
        app,
        [
            "context",
            "add-term",
            str(project_dir),
            "test",
            "--append-forbid",
            "B",
        ],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(_ctx_json(project_dir).read_text("utf-8"))
    entry = next(g for g in data["glossary"] if g["source"] == "test")
    assert sorted(entry["forbidden_targets"]) == ["A", "B"]


def test_context_add_term_clear_forbidden(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    runner.invoke(
        app,
        [
            "context",
            "add-term",
            str(project_dir),
            "test",
            "--forbid",
            "A",
            "--forbid",
            "B",
        ],
    )
    res = runner.invoke(
        app,
        [
            "context",
            "add-term",
            str(project_dir),
            "test",
            "--clear-forbidden",
        ],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(_ctx_json(project_dir).read_text("utf-8"))
    entry = next(g for g in data["glossary"] if g["source"] == "test")
    assert entry["forbidden_targets"] == []


def test_context_add_term_forbid_append_conflict(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    res = runner.invoke(
        app,
        [
            "context",
            "add-term",
            str(project_dir),
            "test",
            "--forbid",
            "A",
            "--append-forbid",
            "B",
        ],
    )
    assert res.exit_code != 0
    assert "mutually exclusive" in res.output


def test_context_add_term_clear_forbidden_conflict(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    res = runner.invoke(
        app,
        [
            "context",
            "add-term",
            str(project_dir),
            "test",
            "--clear-forbidden",
            "--forbid",
            "A",
        ],
    )
    assert res.exit_code != 0
    assert "conflicts" in res.output


def test_context_add_term_update_does_not_clobber_category_or_enforce(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    runner.invoke(
        app,
        [
            "context",
            "add-term",
            str(project_dir),
            "test",
            "--target",
            "T",
            "--category",
            "concept",
            "--enforce",
            "error",
            "--notes",
            "original note",
        ],
    )
    # Update only target; category/enforce/notes must be preserved.
    res = runner.invoke(
        app,
        [
            "context",
            "add-term",
            str(project_dir),
            "test",
            "--target",
            "T2",
        ],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(_ctx_json(project_dir).read_text("utf-8"))
    entry = next(g for g in data["glossary"] if g["source"] == "test")
    assert entry["target"] == "T2"
    assert entry["category"] == "concept"
    assert entry["enforce"] == "error"
    assert entry["notes"] == "original note"


# --- remove-term -----------------------------------------------------------


def test_context_remove_term_deletes_entry_and_renders(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    runner.invoke(
        app,
        [
            "context",
            "add-term",
            str(project_dir),
            "test",
            "--target",
            "T",
        ],
    )
    res = runner.invoke(
        app,
        ["context", "remove-term", str(project_dir), "test"],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(_ctx_json(project_dir).read_text("utf-8"))
    assert not any(g["source"] == "test" for g in data["glossary"])
    assert "test" not in _ctx_md(project_dir).read_text("utf-8")


def test_context_remove_term_missing_fails_unless_missing_ok(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    res = runner.invoke(
        app,
        ["context", "remove-term", str(project_dir), "nonexistent"],
    )
    assert res.exit_code != 0
    res_ok = runner.invoke(
        app,
        ["context", "remove-term", str(project_dir), "nonexistent", "--missing-ok"],
    )
    assert res_ok.exit_code == 0, res_ok.output


# --- reset-term ------------------------------------------------------------


def test_context_reset_term_replaces_entry(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    runner.invoke(
        app,
        [
            "context",
            "add-term",
            str(project_dir),
            "test",
            "--target",
            "Old",
            "--forbid",
            "A",
            "--category",
            "concept",
            "--notes",
            "old",
            "--enforce",
            "error",
        ],
    )
    res = runner.invoke(
        app,
        [
            "context",
            "reset-term",
            str(project_dir),
            "test",
            "--target",
            "New",
            "--forbid",
            "B",
            "--category",
            "term",
            "--notes",
            "",
            "--enforce",
            "warn",
        ],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(_ctx_json(project_dir).read_text("utf-8"))
    entry = next(g for g in data["glossary"] if g["source"] == "test")
    assert entry["target"] == "New"
    assert entry["forbidden_targets"] == ["B"]
    assert entry["category"] == "term"
    assert entry["notes"] == ""
    assert entry["enforce"] == "warn"


def test_context_reset_term_create_flag_required_for_missing(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    res = runner.invoke(
        app,
        [
            "context",
            "reset-term",
            str(project_dir),
            "nonexistent",
            "--target",
            "T",
        ],
    )
    assert res.exit_code != 0
    res_create = runner.invoke(
        app,
        [
            "context",
            "reset-term",
            str(project_dir),
            "nonexistent",
            "--target",
            "T",
            "--create",
        ],
    )
    assert res_create.exit_code == 0, res_create.output
    data = json.loads(_ctx_json(project_dir).read_text("utf-8"))
    entry = next(g for g in data["glossary"] if g["source"] == "nonexistent")
    assert entry["target"] == "T"


# --- chapter-note --replace-all --------------------------------------------


def test_chapter_note_replace_all_resets_summaries_and_lists(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    # Create initial state.
    runner.invoke(
        app,
        [
            "context",
            "chapter-note",
            str(project_dir),
            "0006",
            "--title",
            "Old",
            "--source-summary",
            "old source",
            "--translation-summary",
            "old trans",
            "--decision",
            "old d",
            "--open-issue",
            "old issue",
        ],
    )
    # Replace all.
    res = runner.invoke(
        app,
        [
            "context",
            "chapter-note",
            str(project_dir),
            "0006",
            "--replace-all",
            "--title",
            "New",
            "--source-summary",
            "new source",
            "--translation-summary",
            "new trans",
            "--decision",
            "new d",
            "--open-issue",
            "new issue",
        ],
    )
    assert res.exit_code == 0, res.output
    note = next(c for c in _load_chapters(project_dir) if c["chapter_id"] == "0006")
    assert note["title"] == "New"
    assert note["source_summary"] == "new source"
    assert note["translation_summary"] == "new trans"
    assert note["decisions_added"] == ["new d"]
    assert note["open_issues"] == ["new issue"]


def test_chapter_note_replace_all_rejects_other_replace_flags(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    res = runner.invoke(
        app,
        [
            "context",
            "chapter-note",
            str(project_dir),
            "0006",
            "--replace-all",
            "--replace-decisions",
            "--title",
            "X",
        ],
    )
    assert res.exit_code != 0, res.output
    assert "conflicts" in res.output


def test_chapter_note_replace_all_preserves_chunk_ids(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    # Create with chapter-map to hydrate chunk_ids.
    (project_dir / ".booktx" / "chapter-map.json").write_text(
        '{"version": 1, "source_sha256": "", '
        '"chapters": [{"chapter_id": "0006", "title": "Mapped", '
        '"chunk_ids": ["0006"]}]}',
        encoding="utf-8",
    )
    res = runner.invoke(
        app,
        [
            "context",
            "chapter-note",
            str(project_dir),
            "0006",
            "--replace-all",
            "--title",
            "New",
        ],
    )
    assert res.exit_code == 0, res.output
    note = next(c for c in _load_chapters(project_dir) if c["chapter_id"] == "0006")
    assert note["chunk_ids"] == ["0006"]
    assert note["title"] == "New"


def _make_sync_project(tmp_path: Path) -> Path:
    project_dir = tmp_path / "sync-book"
    src = tmp_path / "sync.md"
    src.write_text(MARKDOWN_DOC, encoding="utf-8")
    res = runner.invoke(
        app,
        [
            "init",
            str(project_dir),
            "--source-file",
            str(src),
            "--source-lang",
            "en",
        ],
    )
    assert res.exit_code == 0, res.output
    create_profile(project_dir, "de_source", target_language="de")
    create_profile(project_dir, "de_flash", target_language="de")
    create_profile(project_dir, "de_mimo", target_language="de")
    create_profile(
        project_dir, "passthrough_en", target_language="en", kind="pass-through"
    )
    return project_dir


def _ready_profile_context(project_dir: Path, profile: str) -> None:
    res = runner.invoke(
        app,
        [
            "context",
            "init",
            str(project_dir),
            "--profile",
            profile,
            "--non-interactive",
        ],
    )
    assert res.exit_code == 0, res.output
    for qid, text in (
        ("Q001", "de-DE"),
        ("Q002", "balanced"),
        ("Q003", "neutral"),
        ("Q004", "natural dialogue"),
        ("Q005", "keep Apt names"),
        ("Q006", "translate world terms"),
        ("Q012", "error"),
    ):
        res = runner.invoke(
            app,
            [
                "context",
                "answer",
                str(project_dir),
                qid,
                "--profile",
                profile,
                "--text",
                text,
            ],
        )
        assert res.exit_code == 0, res.output
    res = runner.invoke(
        app, ["context", "mark-ready", str(project_dir), "--profile", profile]
    )
    assert res.exit_code == 0, res.output


def _reset_term(project_dir: Path, profile: str, source: str, target: str) -> None:
    res = runner.invoke(
        app,
        [
            "context",
            "reset-term",
            str(project_dir),
            source,
            "--profile",
            profile,
            "--target",
            target,
            "--create",
            "--enforce",
            "error",
        ],
    )
    assert res.exit_code == 0, res.output


def test_context_sync_json_payload_is_stable(tmp_path: Path):
    project_dir = _make_sync_project(tmp_path)
    _ready_profile_context(project_dir, "de_source")
    _ready_profile_context(project_dir, "de_flash")
    _reset_term(project_dir, "de_source", "empire", "Imperium")

    res = runner.invoke(
        app,
        [
            "context",
            "sync",
            str(project_dir),
            "--from",
            "de_source",
            "--to",
            "de_flash",
            "--section",
            "glossary",
            "--term",
            "empire",
            "--json",
        ],
    )

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["version"] == 1
    assert payload["source"]["profile"] == "de_source"
    assert payload["sections"] == ["glossary"]
    assert payload["glossary_terms"] == ["empire"]
    assert payload["write"] is False
    assert payload["blocked"] is False
    assert payload["targets"][0]["profile"] == "de_flash"
    assert "would_write_profiles" in payload


def test_context_sync_write_blocks_on_conflict_without_mutation(tmp_path: Path):
    project_dir = _make_sync_project(tmp_path)
    for profile in ("de_source", "de_flash", "de_mimo"):
        _ready_profile_context(project_dir, profile)
    _reset_term(project_dir, "de_source", "empire", "Imperium")
    _reset_term(project_dir, "de_flash", "empire", "Kaiserreich")

    before = load_context(load_profile_project(project_dir, "de_mimo")).model_dump(
        mode="json"
    )
    res = runner.invoke(
        app,
        [
            "context",
            "sync",
            str(project_dir),
            "--from",
            "de_source",
            "--to",
            "de_flash",
            "--to",
            "de_mimo",
            "--section",
            "glossary",
            "--term",
            "empire",
            "--write",
        ],
    )

    assert res.exit_code == 1
    assert "Blocked by conflicts or errors. Nothing written." in res.output
    after = load_context(load_profile_project(project_dir, "de_mimo")).model_dump(
        mode="json"
    )
    assert after == before


def test_context_doctor_json_reports_single_profile_issue(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    res = runner.invoke(
        app,
        [
            "context",
            "add-term",
            str(project_dir),
            "empire",
            "--target",
            "Imperium",
            "--enforce",
            "error",
        ],
    )
    assert res.exit_code == 0, res.output

    doctor = runner.invoke(app, ["context", "doctor", str(project_dir), "--json"])

    assert doctor.exit_code == 0, doctor.output
    payload = json.loads(doctor.output)
    assert payload["summary"]["warning"] >= 1
    assert any(
        issue["code"] == "advisory_entry_looks_binding" for issue in payload["issues"]
    )


def test_context_doctor_compare_profiles_json_reports_divergence(tmp_path: Path):
    project_dir = _make_sync_project(tmp_path)
    for profile in ("de_source", "de_flash"):
        runner.invoke(
            app,
            [
                "context",
                "init",
                str(project_dir),
                "--profile",
                profile,
                "--non-interactive",
            ],
        )
    for profile, target in (
        ("de_source", "Gottesanbeter"),
        ("de_flash", "Gottesanbeterin"),
    ):
        res = runner.invoke(
            app,
            [
                "context",
                "add-term",
                str(project_dir),
                "mantis",
                "--profile",
                profile,
                "--target",
                target,
            ],
        )
        assert res.exit_code == 0, res.output

    doctor = runner.invoke(
        app, ["context", "doctor", str(project_dir), "--compare-profiles", "--json"]
    )

    assert doctor.exit_code == 0, doctor.output
    payload = json.loads(doctor.output)
    assert any(
        issue["code"] == "profile_glossary_target_divergence"
        for issue in payload["issues"]
    )


def test_context_doctor_compare_profiles_rejected_in_isolated_mode(
    tmp_path: Path, monkeypatch
):
    project_dir = _make_sync_project(tmp_path)
    profile = "de_source"
    runner.invoke(
        app,
        [
            "context",
            "init",
            str(project_dir),
            "--profile",
            profile,
            "--non-interactive",
        ],
    )
    profile_dir = project_dir / "translations" / profile
    monkeypatch.chdir(profile_dir)

    res = runner.invoke(app, ["context", "doctor", ".", "--compare-profiles"])

    assert res.exit_code == 1
    assert "isolated profile-root mode" in res.output


def test_context_doctor_write_report_uses_safe_location(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    report = Path.cwd() / ".context-organization-report-test.md"
    report.unlink(missing_ok=True)
    try:
        res = runner.invoke(
            app,
            [
                "context",
                "doctor",
                str(project_dir),
                "--write-report",
                str(report),
            ],
        )

        assert res.exit_code == 0, res.output
        assert report.is_file()
        assert "Context organization report" in report.read_text("utf-8")
    finally:
        report.unlink(missing_ok=True)


def test_context_render_effective_view_omits_answered_questions(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    res = runner.invoke(
        app, ["context", "answer", str(project_dir), "Q002", "--text", "balanced"]
    )
    assert res.exit_code == 0, res.output

    full = runner.invoke(app, ["context", "render", str(project_dir), "--stdout"])
    effective = runner.invoke(
        app, ["context", "render", str(project_dir), "--view", "effective", "--stdout"]
    )

    assert full.exit_code == 0, full.output
    assert effective.exit_code == 0, effective.output
    assert "## Answered questions" in full.output
    assert "## Answered questions" not in effective.output
    assert "## Style" in effective.output
