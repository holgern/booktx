"""CLI tests for `booktx context ...`."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from booktx.cli import app
from booktx.config import load_project
from booktx.context import context_markdown_path, context_path

runner = CliRunner()


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
    res = runner.invoke(app, ["context", "mark-ready", str(project_dir), "--force"])
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
    assert "--write" in res.output
    assert "--stdout" in res.output
    assert "--force-discard-md-only" in res.output


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
