"""CLI tests for chapter-aware workflow."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from booktx.cli import app
from booktx.config import load_project

runner = CliRunner()


DOC = """\
# One

First sentence. Second sentence.

# Two

Third sentence. Fourth sentence.
"""


def _make_project(tmp_path: Path) -> Path:
    src = tmp_path / "book.md"
    src.write_text(DOC, encoding="utf-8")
    project_dir = tmp_path / "book"
    res = runner.invoke(
        app,
        [
            "init",
            str(project_dir),
            "--target",
            "de",
            "--source-file",
            str(src),
            "--chunk-size",
            "2",
        ],
    )
    assert res.exit_code == 0, res.output
    ext = runner.invoke(app, ["extract", str(project_dir)])
    assert ext.exit_code == 0, ext.output
    return project_dir


def _ready_context(project_dir: Path) -> None:
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    runner.invoke(app, ["context", "mark-ready", str(project_dir), "--force"])


def _translated_dir(project_dir: Path) -> Path:
    path = load_project(project_dir).translated_dir
    assert path is not None
    return path


def test_chapters_lists_detected_ranges(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    res = runner.invoke(app, ["chapters", str(project_dir)])
    assert res.exit_code == 0, res.output
    assert "0001" in res.output
    assert "One" in res.output
    assert "Two" in res.output
    assert "chunks:" in res.output
    assert (project_dir / ".booktx" / "chapter-map.json").is_file()


def test_next_chapter_respects_context_gate(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    res = runner.invoke(app, ["next-chapter", str(project_dir)])
    assert res.exit_code == 1
    assert "context" in res.output.lower()


def test_next_chapter_prints_first_incomplete_chapter(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    _ready_context(project_dir)
    res = runner.invoke(app, ["next-chapter", str(project_dir)])
    assert res.exit_code == 0, res.output
    assert "context:" in res.output
    assert "chapter: 0001" in res.output
    assert "chunks:" in res.output
    assert "pending chunks:" in res.output
    assert "record range:" in res.output


def test_next_unit_chapter_matches_next_chapter(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    _ready_context(project_dir)
    res = runner.invoke(app, ["next", str(project_dir), "--unit", "chapter"])
    assert res.exit_code == 0, res.output
    assert "chapter: 0001" in res.output
    assert "pending chunks:" in res.output


def test_next_chapter_skips_completed_chapter(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    _ready_context(project_dir)
    translated_dir = _translated_dir(project_dir)
    # Chapter one covers chunks 0001 and 0002 in this fixture. Mark them done.
    for cid in ("0001", "0002"):
        chunk_path = project_dir / ".booktx" / "chunks" / f"{cid}.json"
        chunk = json.loads(chunk_path.read_text("utf-8"))
        records = [{"id": r["id"], "target": r["source"]} for r in chunk["records"]]
        payload = {"chunk_id": cid, "records": records}
        out_path = translated_dir / f"{cid}.json"
        out_path.write_text(json.dumps(payload), encoding="utf-8")
    res = runner.invoke(app, ["next-chapter", str(project_dir)])
    assert res.exit_code == 0, res.output
    assert "chapter: 0002" in res.output
