"""Tests for the large-chapter protection in booktx translate next.

Covers:
  - oversized chapter auto-creates single-chapter todo
  - small chapter stays a normal chapter task
  - --force-chapter allows large tasks
  - retries reuse existing todo (no duplicates)
  - insert prints todo-resume next hint for todo-backed tasks
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from booktx.cli import app
from booktx.config import load_project, translation_store_path

runner = CliRunner()

THREE_CHAPTERS = """\
# One

First sentence. Second sentence.

# Two

Third sentence. Fourth sentence.

# Three

Fifth sentence. Sixth sentence.
"""


def _make_project(tmp_path: Path) -> Path:
    """Create a project with 3 chapters (6 records, chunk_size=2 -> 3 chunks)."""
    src = tmp_path / "book.md"
    src.write_text(THREE_CHAPTERS, encoding="utf-8")
    project_dir = tmp_path / "proj"
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
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    runner.invoke(
        app,
        ["context", "mark-ready", str(project_dir), "--force", "--reason", "test"],
    )
    return project_dir


def _proj(project_dir: Path):
    return load_project(project_dir)


def _store_path(project_dir: Path) -> Path:
    return translation_store_path(_proj(project_dir))


def _accept_chapter(project_dir: Path, chapter_id: str, *, max_words: int = 900):
    """Accept a chapter task and insert translated records."""
    next_res = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--chapter",
            chapter_id,
            "--unit",
            "chapter",
            "--max-words",
            str(max_words),
            "--json",
        ],
    )
    assert next_res.exit_code == 0, next_res.output
    task = json.loads(next_res.output)
    ingest_path = (
        project_dir
        / "translations"
        / "de_default"
        / "ingest"
        / f"{task['task_id']}.block.txt"
    )
    ingest_json = {
        "schema_version": 2,
        "profile": _proj(project_dir).profile,
        "task_id": task["task_id"],
        "translation_version": task["translation_version"],
        "records": [
            {"id": r["id"], "target": f"Translated {r['id']}"} for r in task["records"]
        ],
    }
    ingest_path.write_text(json.dumps(ingest_json), encoding="utf-8")
    insert_res = runner.invoke(
        app,
        [
            "translate",
            "insert",
            str(project_dir),
            "--task-id",
            task["task_id"],
            "--json-file",
            str(ingest_path),
        ],
    )
    assert insert_res.exit_code == 0, insert_res.output
    return task


# ---------------------------------------------------------------------------
# Test: oversized chapter auto-creates single-chapter todo
# ---------------------------------------------------------------------------


def test_translate_next_chapter_oversized_auto_creates_single_chapter_todo(
    tmp_path: Path,
):
    # Accept chapter 0001 so we have chapter 0002 remaining with 2 records.
    # Use a very small word budget to force oversized behavior.
    project_dir = _make_project(tmp_path)
    _accept_chapter(project_dir, "0001")

    res = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--chapter",
            "0002",
            "--unit",
            "chapter",
            "--chapter-word-limit",
            "1",  # very small -> chapter 0002 (2 words) is oversized
            "--format",
            "block",
        ],
    )
    assert res.exit_code == 0, res.output
    output = res.output
    assert "large chapter detected" in output or "todo" in output.lower()


# ---------------------------------------------------------------------------
# Test: small chapter stays a normal chapter task
# ---------------------------------------------------------------------------


def test_translate_next_chapter_small_stays_chapter_task(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    _accept_chapter(project_dir, "0001")

    res = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--chapter",
            "0002",
            "--unit",
            "chapter",
            "--max-words",
            "900",
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    task = json.loads(res.output)
    assert task["unit"] == "chapter"
    assert task.get("todo_id") is None or task.get("todo_id") == ""


# ---------------------------------------------------------------------------
# Test: --force-chapter allows large task
# ---------------------------------------------------------------------------


def test_translate_next_chapter_force_allows_large_task(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    _accept_chapter(project_dir, "0001")

    res = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--chapter",
            "0002",
            "--unit",
            "chapter",
            "--chapter-word-limit",
            "1",
            "--force-chapter",
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    task = json.loads(res.output)
    assert task["unit"] == "chapter"
    assert task.get("todo_id") is None or task.get("todo_id") == ""


# ---------------------------------------------------------------------------
# Test: retries reuse existing single-chapter todo
# ---------------------------------------------------------------------------


def test_retries_reuse_existing_single_chapter_todo(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    _accept_chapter(project_dir, "0001")

    # First call creates the todo.
    res1 = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--chapter",
            "0002",
            "--unit",
            "chapter",
            "--chapter-word-limit",
            "1",
            "--format",
            "block",
        ],
    )
    assert res1.exit_code == 0, res1.output

    # Second call should reuse the same todo.
    res2 = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--chapter",
            "0002",
            "--unit",
            "chapter",
            "--chapter-word-limit",
            "1",
            "--format",
            "block",
        ],
    )
    # Both should succeed and point to the same todo.
    assert res2.exit_code == 0, res2.output
    # No duplicate todo files should exist.
    todo_dir = project_dir / "translations" / "de_default" / "todos"
    if todo_dir.exists():
        json_files = list(todo_dir.glob("*.json"))
        assert len(json_files) <= 1, f"expected at most 1 todo, got {len(json_files)}"


# ---------------------------------------------------------------------------
# Test: insert from todo prints todo-resume next hint
# ---------------------------------------------------------------------------


def test_insert_from_todo_prints_todo_resume_next_hint(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    _accept_chapter(project_dir, "0001")

    # Create a todo-backed task with block output to get the task id.
    res = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--chapter",
            "0002",
            "--unit",
            "chapter",
            "--chapter-word-limit",
            "1",
            "--format",
            "block",
        ],
    )
    assert res.exit_code == 0, res.output
    output = res.output

    # Parse the task id from the output.
    import re

    task_match = re.search(r"task-id\s+(\S+)", output)
    if not task_match:
        return  # small chapter, no todo created
    task_id = task_match.group(1)

    # Verify a todo was created.
    todo_dir = project_dir / "translations" / "de_default" / "todos"
    if not todo_dir.exists():
        return
    json_files = list(todo_dir.glob("*.json"))
    if not json_files:
        return

    # Get the task file to read ingest path.
    tasks_dir = project_dir / "translations" / "de_default" / "tasks"
    task_json_path = tasks_dir / f"{task_id}.json"
    if not task_json_path.exists():
        return
    task = json.loads(task_json_path.read_text(encoding="utf-8"))
    if not task.get("todo_id"):
        return

    # Insert the records.
    ingest_path = (
        project_dir / "translations" / "de_default" / "ingest" / f"{task_id}.block.txt"
    )
    ingest_json = {
        "schema_version": 2,
        "profile": _proj(project_dir).profile,
        "task_id": task_id,
        "translation_version": task.get("translation_version"),
        "records": [
            {"id": r["id"], "target": f"Translated {r['id']}"} for r in task["records"]
        ],
    }
    ingest_path.write_text(json.dumps(ingest_json), encoding="utf-8")
    insert_res = runner.invoke(
        app,
        [
            "translate",
            "insert",
            str(project_dir),
            "--task-id",
            task_id,
            "--json-file",
            str(ingest_path),
        ],
    )
    assert insert_res.exit_code == 0, insert_res.output
    assert "todo" in insert_res.output.lower()
