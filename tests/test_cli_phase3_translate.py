"""Phase 3 slice 7 behavioral tests: translate/translation commands.

Covers the translate_app (alias translation) commands and workflow functions.
The translation alias equality is asserted in tests/test_cli.py.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from booktx.cli import app

runner = CliRunner()

DOC = "# Chapter One\n\nAlice met Bob.\n"


def _make_project(tmp_path: Path) -> Path:
    src = tmp_path / "book.md"
    src.write_text(DOC, encoding="utf-8")
    project_dir = tmp_path / "book"
    r = runner.invoke(app, ["init", str(project_dir), "--target", "de", "--source-file", str(src)])
    assert r.exit_code == 0, r.output
    return project_dir


def _init_context(project_dir: Path) -> None:
    r = runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    assert r.exit_code == 0
    for q in ["Q001", "Q002", "Q003", "Q004", "Q005", "Q006", "Q012"]:
        runner.invoke(app, ["context", "answer", str(project_dir), q, "--text", "x"])
    runner.invoke(app, ["context", "mark-ready", str(project_dir)])
    runner.invoke(app, ["extract", str(project_dir)])


# --- BooktxError error paths (via CliRunner) ---------------------------------


def test_translation_activate_unknown_record_errors(tmp_path: Path) -> None:
    project_dir = _make_project(tmp_path)
    _init_context(project_dir)
    res = runner.invoke(app, ["translation", "activate", str(project_dir), "9999-999999", "1.1"])
    assert res.exit_code != 0
    assert "error:" in res.output
    assert "has no stored translations" in res.output


def test_translation_review_unknown_record_errors(tmp_path: Path) -> None:
    project_dir = _make_project(tmp_path)
    _init_context(project_dir)
    res = runner.invoke(app, ["translation", "review", str(project_dir), "9999-999999"])
    assert res.exit_code != 0
    assert "error:" in res.output
    assert "has no stored translations" in res.output


def test_translate_insert_requires_input(tmp_path: Path) -> None:
    project_dir = _make_project(tmp_path)
    _init_context(project_dir)
    res = runner.invoke(app, ["translate", "insert", str(project_dir), "--stdin", "--format", "block"], input="")
    assert res.exit_code != 0
    assert "error:" in res.output


def test_translate_next_requires_ready_context(tmp_path: Path) -> None:
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    runner.invoke(app, ["extract", str(project_dir)])
    res = runner.invoke(app, ["translate", "next", str(project_dir)])
    assert res.exit_code != 0
    assert "error:" in res.output


# --- Typer command success paths (CliRunner) ---------------------------------


def test_translate_next_command_creates_task(tmp_path: Path) -> None:
    project_dir = _make_project(tmp_path)
    _init_context(project_dir)
    res = runner.invoke(app, ["translate", "next", str(project_dir)])
    assert res.exit_code == 0, res.output
    assert "task:" in res.output


def test_translation_alias_works(tmp_path: Path) -> None:
    """The 'translation' alias maps to the same 'translate' sub-app."""
    project_dir = _make_project(tmp_path)
    _init_context(project_dir)
    res = runner.invoke(app, ["translation", "next", str(project_dir)])
    assert res.exit_code == 0, res.output
    assert "task:" in res.output
