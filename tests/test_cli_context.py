"""CLI tests for `booktx context ...`."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from booktx.cli import app

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


def test_context_init_non_interactive_creates_files(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    res = runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    assert res.exit_code == 0, res.output
    ctx_path = project_dir / ".booktx" / "context.json"
    md_path = project_dir / ".booktx" / "context.md"
    assert ctx_path.is_file()
    assert md_path.is_file()
    data = json.loads(ctx_path.read_text("utf-8"))
    assert data["ready"] is False
    assert len(data["questions"]) == 12
    assert {g["source"] for g in data["glossary"]} >= {"Lowlands", "Lowlander"}
    assert "Niederlande" in md_path.read_text("utf-8")


def test_context_status_reports_not_ready_when_required_open(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    res = runner.invoke(app, ["context", "status", str(project_dir)])
    assert res.exit_code == 0, res.output
    assert "NOT READY" in res.output
    assert "open_required=10" in res.output


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
    data = json.loads((project_dir / ".booktx" / "context.json").read_text("utf-8"))
    low = next(g for g in data["glossary"] if g["source"] == "Lowlands")
    assert "Niederländer" in low["forbidden_targets"]
    assert low["enforce"] == "error"
    assert "Niederländer" in (project_dir / ".booktx" / "context.md").read_text("utf-8")


def test_context_mark_ready_fails_until_required_answers_exist(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    res = runner.invoke(app, ["context", "mark-ready", str(project_dir)])
    assert res.exit_code == 1
    assert "required questions" in res.output

    required_ids = (
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
    )
    for qid in required_ids:
        ans = runner.invoke(
            app,
            ["context", "answer", str(project_dir), qid, "--text", f"answer {qid}"],
        )
        assert ans.exit_code == 0, ans.output
    res2 = runner.invoke(app, ["context", "mark-ready", str(project_dir)])
    assert res2.exit_code == 0, res2.output
    data = json.loads((project_dir / ".booktx" / "context.json").read_text("utf-8"))
    assert data["ready"] is True
    assert "READY" in (project_dir / ".booktx" / "context.md").read_text("utf-8")


def test_context_mark_ready_force_allows_open_questions(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    res = runner.invoke(app, ["context", "mark-ready", str(project_dir), "--force"])
    assert res.exit_code == 0, res.output
    data = json.loads((project_dir / ".booktx" / "context.json").read_text("utf-8"))
    assert data["ready"] is True


def test_context_render_regenerates_markdown(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    md_path = project_dir / ".booktx" / "context.md"
    md_path.write_text("stale", encoding="utf-8")
    res = runner.invoke(app, ["context", "render", str(project_dir)])
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

    render = runner.invoke(app, ["context", "render", str(project_dir)])
    assert render.exit_code == 0, render.output

    markdown = (project_dir / ".booktx" / "context.md").read_text("utf-8")
    assert "Target locale: de-DE" in markdown
    assert "- Prose style: balanced" in markdown
    assert "- Register: neutral" in markdown
    assert "- Dialogue: natural dialogue; keep meaning" in markdown
    assert (
        "- Punctuation: German quotation marks; preserve dashes and italics"
        in markdown
    )
    assert "- Units: feet -> Fuß, miles -> Meilen" in markdown
