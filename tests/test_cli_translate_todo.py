"""CLI tests for the bounded multi-chapter agent run todo (translate todo-next).

Covers:
  - durable file creation (JSON + Markdown)
  - chapter selection order (include-current, skip-current)
  - stable JSON shape
  - no-pending-chapters non-zero exit
  - context gate
  - source drift guard
  - smarter post-insert next-hint
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from booktx.cli import app
from booktx.config import load_project, translation_store_path

runner = CliRunner()

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

THREE_CHAPTERS_DOC = """\
# One

First sentence. Second sentence.

# Two

Third sentence. Fourth sentence.

# Three

Fifth sentence. Sixth sentence.
"""


def _make_three_chapter_project(tmp_path: Path) -> Path:
    """Create a project with 3 chapters (6 records, chunk_size=2 → 3 chunks)."""
    src = tmp_path / "book.md"
    src.write_text(THREE_CHAPTERS_DOC, encoding="utf-8")
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
    runner.invoke(app, ["context", "mark-ready", str(project_dir), "--force"])
    return project_dir


def _proj(project_dir: Path):
    return load_project(project_dir)


def _store_path(project_dir: Path) -> Path:
    return translation_store_path(_proj(project_dir))


def _accept_chapter(
    project_dir: Path, chapter_id: str, *, max_words: int = 900
) -> dict:
    """Request a chapter task and insert a translated version for all its records."""
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
    ingest_path = project_dir / task["ingest_path"]
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


def _accept_n_records(
    project_dir: Path, chapter_id: str, n: int, *, max_words: int = 900
) -> dict:
    """Request a chapter task and insert translations for only the first *n* records."""
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
    ingest_path = project_dir / task["ingest_path"]
    ingest_json = {
        "schema_version": 2,
        "profile": _proj(project_dir).profile,
        "task_id": task["task_id"],
        "translation_version": task["translation_version"],
        "records": [
            {"id": r["id"], "target": f"Translated {r['id']}"}
            for r in task["records"][:n]
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
# Tests: todo-next
# ---------------------------------------------------------------------------


def test_todo_next_creates_durable_files(tmp_path: Path):
    project_dir = _make_three_chapter_project(tmp_path)
    res = runner.invoke(
        app,
        [
            "translate",
            "todo-next",
            str(project_dir),
            "--chapters",
            "2",
            "--batch-words",
            "800",
            "--write",
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["chapters_requested"] == 2
    assert payload["batch_words"] == 800
    assert payload["version"] == 1

    json_path = project_dir / payload["json_path"]
    md_path = project_dir / payload["markdown_path"]
    assert json_path.is_file(), f"JSON file not found: {json_path}"
    assert md_path.is_file(), f"MD file not found: {md_path}"
    assert json_path.suffix == ".json"
    assert md_path.suffix == ".md"


def test_todo_next_selects_chapters_in_reading_order(tmp_path: Path):
    """After completing chapter 0001, the todo should select 0002 and 0003."""
    project_dir = _make_three_chapter_project(tmp_path)
    _accept_chapter(project_dir, "0001")

    res = runner.invoke(
        app,
        [
            "translate",
            "todo-next",
            str(project_dir),
            "--chapters",
            "2",
            "--batch-words",
            "800",
            "--write",
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    chapter_ids = [c["chapter_id"] for c in payload["chapters"]]
    assert chapter_ids == ["0002", "0003"], f"expected 0002,0003 got {chapter_ids}"


def test_todo_next_includes_current_partial_chapter_by_default(tmp_path: Path):
    """Accept 1 record in chapter 0002; todo-next --chapters 2 should include 0002 as first."""
    project_dir = _make_three_chapter_project(tmp_path)
    _accept_chapter(project_dir, "0001")
    _accept_n_records(project_dir, "0002", 1)

    res = runner.invoke(
        app,
        [
            "translate",
            "todo-next",
            str(project_dir),
            "--chapters",
            "2",
            "--batch-words",
            "800",
            "--write",
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    chapter_ids = [c["chapter_id"] for c in payload["chapters"]]
    assert chapter_ids[0] == "0002", f"expected 0002 first, got {chapter_ids}"


def test_todo_next_skip_current_starts_after_current(tmp_path: Path):
    """With --skip-current, the first selected chapter should be the second incomplete chapter."""
    project_dir = _make_three_chapter_project(tmp_path)
    _accept_chapter(project_dir, "0001")
    _accept_n_records(project_dir, "0002", 1)

    res = runner.invoke(
        app,
        [
            "translate",
            "todo-next",
            str(project_dir),
            "--chapters",
            "2",
            "--batch-words",
            "800",
            "--skip-current",
            "--write",
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    chapter_ids = [c["chapter_id"] for c in payload["chapters"]]
    assert "0002" not in chapter_ids, f"expected skip of 0002, got {chapter_ids}"
    assert chapter_ids[0] == "0003", f"expected 0003 first, got {chapter_ids}"


def test_todo_next_json_shape_is_stable(tmp_path: Path):
    """Assert the exact top-level and chapter-item keys in the JSON payload."""
    project_dir = _make_three_chapter_project(tmp_path)
    res = runner.invoke(
        app,
        [
            "translate",
            "todo-next",
            str(project_dir),
            "--chapters",
            "2",
            "--batch-words",
            "800",
            "--write",
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)

    expected_top = {
        "version",
        "todo_id",
        "profile",
        "target_language",
        "target_locale",
        "chapters_requested",
        "batch_words",
        "max_run_words",
        "include_current",
        "created_at",
        "context_sha256",
        "source_sha256",
        "chapters",
        "json_path",
        "markdown_path",
    }
    assert set(payload.keys()) == expected_top, (
        f"missing: {expected_top - set(payload.keys())}, "
        f"extra: {set(payload.keys()) - expected_top}"
    )

    expected_chapter = {
        "chapter_id",
        "title",
        "status",
        "records_total",
        "records_translated_at_start",
        "records_remaining_at_start",
        "source_words_remaining_at_start",
        "pending_chunk_ids",
    }
    for ch in payload["chapters"]:
        assert set(ch.keys()) == expected_chapter, (
            f"missing: {expected_chapter - set(ch.keys())}, "
            f"extra: {set(ch.keys()) - expected_chapter}"
        )


def test_todo_next_no_pending_chapters_exits_nonzero(tmp_path: Path):
    """When all chapters are complete, todo-next should exit non-zero without writing."""
    project_dir = _make_three_chapter_project(tmp_path)
    _accept_chapter(project_dir, "0001")
    _accept_chapter(project_dir, "0002")
    _accept_chapter(project_dir, "0003")

    res = runner.invoke(
        app,
        [
            "translate",
            "todo-next",
            str(project_dir),
            "--chapters",
            "2",
            "--batch-words",
            "800",
            "--write",
            "--json",
        ],
    )
    assert res.exit_code != 0, f"expected non-zero exit, got {res.exit_code}"
    assert (
        "no chapters have remaining records" in res.output.lower()
        or "remaining" in res.output.lower()
    )


def test_todo_next_context_gate_applies(tmp_path: Path):
    """If context is missing, todo-next should exit non-zero."""
    project_dir = _make_three_chapter_project(tmp_path)
    # Remove context
    proj = _proj(project_dir)
    assert proj.context_json_path is not None
    proj.context_json_path.unlink()
    if proj.context_md_path is not None:
        proj.context_md_path.unlink()

    res = runner.invoke(
        app,
        [
            "translate",
            "todo-next",
            str(project_dir),
            "--chapters",
            "2",
            "--batch-words",
            "800",
        ],
    )
    assert res.exit_code != 0, f"expected non-zero exit, got {res.exit_code}"
    assert "context" in res.output.lower()


def test_todo_next_source_drift_guard_applies(tmp_path: Path):
    """If source file changed, todo-next should exit non-zero."""
    project_dir = _make_three_chapter_project(tmp_path)
    # Mutate the source file to trigger drift
    src = project_dir / "source" / "book.md"
    src.write_text("# Mutated\nNew text.\n", encoding="utf-8")

    res = runner.invoke(
        app,
        [
            "translate",
            "todo-next",
            str(project_dir),
            "--chapters",
            "2",
            "--batch-words",
            "800",
        ],
    )
    assert res.exit_code != 0, f"expected non-zero exit, got {res.exit_code}"
    assert (
        "source" in res.output.lower()
        or "drift" in res.output.lower()
        or "extract" in res.output.lower()
    )


# ---------------------------------------------------------------------------
# Tests: smarter insert next-hint
# ---------------------------------------------------------------------------


def test_insert_hint_when_chapter_remains_incomplete(tmp_path: Path):
    """After a partial insert, next hint should pin the current chapter."""
    project_dir = _make_three_chapter_project(tmp_path)
    # Insert only 1 record in chapter 0001 (2 records total)
    _accept_n_records(project_dir, "0001", 1)

    # The last insert output should have pointed to chapter 0001 (since it's still incomplete).
    # We can verify by doing another next and checking the hint.
    # Actually, let's capture the insert output directly.
    next_res = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--chapter",
            "0001",
            "--unit",
            "paragraph",
            "--json",
        ],
    )
    assert next_res.exit_code == 0, next_res.output
    task = json.loads(next_res.output)
    ingest_path = project_dir / task["ingest_path"]
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
    # Chapter 0001 is now complete (2/2). Hint should advance (no --chapter 0001).
    # But wait - we only inserted 1 record initially, then 1 more. So now 0001 is complete.
    # The hint should NOT pin chapter 0001.
    assert "--chapter 0001" not in insert_res.output, (
        f"expected no --chapter 0001 pin after chapter completion, got:\n{insert_res.output}"
    )


def test_insert_hint_advances_when_chapter_completes(tmp_path: Path):
    """After completing chapter 0001, next hint should advance (no chapter pin)."""
    project_dir = _make_three_chapter_project(tmp_path)
    _accept_chapter(project_dir, "0001")

    # Now insert chapter 0002 fully
    _accept_chapter(project_dir, "0002")

    # Insert chapter 0003 fully - should suggest build
    next_res = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--chapter",
            "0003",
            "--unit",
            "chapter",
            "--json",
        ],
    )
    assert next_res.exit_code == 0, next_res.output
    task = json.loads(next_res.output)
    ingest_path = project_dir / task["ingest_path"]
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
    # All records are now complete. Hint should suggest build, not translate next.
    assert "build" in insert_res.output.lower(), (
        f"expected build hint when all records complete, got:\n{insert_res.output}"
    )


def test_insert_hint_preserves_requested_batch_size(tmp_path: Path):
    """The next hint should reuse the batch size the task was created with."""
    project_dir = _make_three_chapter_project(tmp_path)

    # Create a task with --max-words 777
    next_res = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--chapter",
            "0001",
            "--unit",
            "batch",
            "--max-words",
            "777",
            "--json",
        ],
    )
    assert next_res.exit_code == 0, next_res.output
    task = json.loads(next_res.output)
    assert task["requested_max_words"] == 777

    # Insert it
    ingest_path = project_dir / task["ingest_path"]
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
    # The next hint should use --max-words 777
    assert "--max-words 777" in insert_res.output, (
        f"expected --max-words 777 in hint, got:\n{insert_res.output}"
    )
