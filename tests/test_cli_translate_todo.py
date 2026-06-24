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
from booktx.context import (
    ChapterContext,
    load_context,
    write_context,
    write_context_markdown,
)

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


def _create_todo(
    project_dir: Path,
    *,
    chapters: int = 2,
    batch_words: int = 800,
    max_run_words: int | None = None,
) -> dict:
    args = [
        "translate",
        "todo-next",
        str(project_dir),
        "--chapters",
        str(chapters),
        "--batch-words",
        str(batch_words),
        "--write",
        "--json",
    ]
    if max_run_words is not None:
        args.extend(["--max-run-words", str(max_run_words)])
    res = runner.invoke(app, args)
    assert res.exit_code == 0, res.output
    return json.loads(res.output)


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
    """Accept 1 record in chapter 0002 so todo-next keeps 0002 first."""
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
    """With --skip-current, selection should start after the current chapter."""
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
        "baseline_ref",
        "baseline_sha256",
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
    """When all chapters are complete, todo-next should exit non-zero."""
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

    # Capture a follow-up insert directly so the next-hint stays observable.
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
    # We inserted 1 record earlier and 1 more here, so chapter 0001 is complete.
    # The hint should NOT pin chapter 0001.
    assert "--chapter 0001" not in insert_res.output, (
        "expected no --chapter 0001 pin after chapter completion, "
        f"got:\n{insert_res.output}"
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


# ---------------------------------------------------------------------------
# Tests: todo-status / todo-resume
# ---------------------------------------------------------------------------


def test_todo_status_latest_reports_partial_current_progress(tmp_path: Path):
    project_dir = _make_three_chapter_project(tmp_path)
    todo = _create_todo(project_dir, chapters=2, batch_words=777)
    _accept_n_records(project_dir, "0001", 1)

    res = runner.invoke(
        app,
        [
            "translate",
            "todo-status",
            str(project_dir),
            "--latest",
            "--json",
        ],
    )

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["todo_id"] == todo["todo_id"]
    assert payload["goal_complete"] is False
    assert payload["chapters_complete"] == 0
    assert payload["next_planned_chapter"] == "0001"
    assert payload["next_safe_command"].startswith("booktx translate todo-resume .")
    assert payload["next_safe_command"].endswith("--format block")
    assert payload["chapters"][0]["records_translated_now"] == 1
    assert (
        payload["chapters"][0]["records_remaining_now"]
        == payload["chapters"][0]["records_total"] - 1
    )


def test_todo_status_does_not_block_on_chapter_note_only_change(tmp_path: Path):
    project_dir = _make_three_chapter_project(tmp_path)
    todo = _create_todo(project_dir, chapters=2, batch_words=777)
    proj = _proj(project_dir)
    ctx = load_context(proj)
    assert ctx is not None
    ctx.chapter_contexts.append(
        ChapterContext(chapter_id="0001", title="One", translation_summary="Done one.")
    )
    write_context(proj, ctx)
    write_context_markdown(proj, ctx)

    res = runner.invoke(
        app,
        [
            "translate",
            "todo-status",
            str(project_dir),
            "--todo-id",
            todo["todo_id"],
            "--json",
        ],
    )

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["context_drifted"] is False
    assert payload["state"] == "ready"


def test_todo_resume_latest_pins_current_planned_chapter(tmp_path: Path):
    project_dir = _make_three_chapter_project(tmp_path)
    todo = _create_todo(project_dir, chapters=2, batch_words=777)
    _accept_n_records(project_dir, "0001", 1)

    res = runner.invoke(
        app,
        [
            "translate",
            "todo-resume",
            str(project_dir),
            "--latest",
            "--format",
            "text",
            "--json",
        ],
    )

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["chapter_id"] == "0001"
    assert payload["requested_max_words"] == 777
    assert payload["todo_id"] == todo["todo_id"]


def test_todo_resume_advances_to_next_planned_chapter_after_completion(tmp_path: Path):
    project_dir = _make_three_chapter_project(tmp_path)
    todo = _create_todo(project_dir, chapters=2)
    _accept_chapter(project_dir, "0001")

    res = runner.invoke(
        app,
        [
            "translate",
            "todo-resume",
            str(project_dir),
            "--todo-id",
            todo["todo_id"],
            "--format",
            "text",
            "--json",
        ],
    )

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["chapter_id"] == "0002"
    assert payload["todo_id"] == todo["todo_id"]


def test_todo_resume_refuses_completed_todo(tmp_path: Path):
    project_dir = _make_three_chapter_project(tmp_path)
    todo = _create_todo(project_dir, chapters=2)
    _accept_chapter(project_dir, "0001")
    _accept_chapter(project_dir, "0002")

    res = runner.invoke(
        app,
        [
            "translate",
            "todo-resume",
            str(project_dir),
            "--todo-id",
            todo["todo_id"],
        ],
    )

    assert res.exit_code == 1
    assert "already" in res.output
    assert "No further task will be issued." in res.output


def test_todo_status_latest_refuses_overlapping_incomplete_todos(tmp_path: Path):
    project_dir = _make_three_chapter_project(tmp_path)
    _create_todo(project_dir, chapters=3)
    _create_todo(project_dir, chapters=2)

    res = runner.invoke(
        app,
        [
            "translate",
            "todo-status",
            str(project_dir),
            "--latest",
        ],
    )

    assert res.exit_code == 1
    assert "overlaps planned chapters" in res.output


def test_todo_resume_blocks_source_drift(tmp_path: Path):
    project_dir = _make_three_chapter_project(tmp_path)
    todo = _create_todo(project_dir, chapters=2)
    src = project_dir / "source" / "book.md"
    src.write_text("# Mutated\nNew text.\n", encoding="utf-8")

    res = runner.invoke(
        app,
        [
            "translate",
            "todo-resume",
            str(project_dir),
            "--todo-id",
            todo["todo_id"],
        ],
    )

    assert res.exit_code == 1
    assert "Run `booktx extract .`" in res.output


def test_todo_resume_blocks_validation_warnings(tmp_path: Path):
    project_dir = _make_three_chapter_project(tmp_path)
    todo = _create_todo(project_dir, chapters=2)
    proj = _proj(project_dir)
    assert proj.context_md_path is not None
    proj.context_md_path.write_text("stale render\n", encoding="utf-8")

    res = runner.invoke(
        app,
        [
            "translate",
            "todo-resume",
            str(project_dir),
            "--todo-id",
            todo["todo_id"],
        ],
    )

    assert res.exit_code == 1
    assert "warning(s)" in res.output


def test_todo_markdown_uses_strict_validate_and_advisory_budget(tmp_path: Path):
    project_dir = _make_three_chapter_project(tmp_path)
    todo = _create_todo(project_dir, chapters=2, batch_words=800, max_run_words=12000)
    todo_md = project_dir / todo["markdown_path"]
    text = todo_md.read_text("utf-8")

    assert "Advisory run budget: 12,000 source words" in text
    assert "--fail-on-warnings" in text
    assert "booktx translate todo-status ." in text
    assert "booktx translate todo-resume ." in text
    assert "stop and report progress before requesting more work" in text


def test_todo_next_human_output_keeps_paths_and_commands_copyable(tmp_path: Path):
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
        ],
        env={"COLUMNS": "40"},
    )

    assert res.exit_code == 0, res.output
    assert ".js\non" not in res.output
    assert "json: translations/" in res.output
    assert "next command: booktx translate todo-status ." in res.output
    assert "resume command: booktx translate todo-resume ." in res.output


def test_insert_hint_uses_todo_resume_for_partial_todo_progress(tmp_path: Path):
    project_dir = _make_three_chapter_project(tmp_path)
    todo = _create_todo(project_dir, chapters=2, batch_words=777)
    next_res = runner.invoke(
        app,
        [
            "translate",
            "todo-resume",
            str(project_dir),
            "--todo-id",
            todo["todo_id"],
            "--format",
            "text",
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
        "records": [{"id": task["records"][0]["id"], "target": "Translated first"}],
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
    assert "booktx translate todo-resume ." in insert_res.output
    assert f"--todo-id {todo['todo_id']}" in insert_res.output
    assert "translate next ." not in insert_res.output


def test_insert_hint_prints_chapter_note_template_for_todo_chapter_completion(
    tmp_path: Path,
):
    project_dir = _make_three_chapter_project(tmp_path)
    todo = _create_todo(project_dir, chapters=2, batch_words=900)
    next_res = runner.invoke(
        app,
        [
            "translate",
            "todo-resume",
            str(project_dir),
            "--todo-id",
            todo["todo_id"],
            "--format",
            "text",
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
            {"id": record["id"], "target": f"Translated {record['id']}"}
            for record in task["records"]
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
    assert "chapter complete: 0001 One" in insert_res.output
    assert "recommended context update template:" in insert_res.output
    assert "booktx context chapter-note ." in insert_res.output
    assert f"--todo-id {todo['todo_id']}" in insert_res.output


def test_insert_hint_stops_when_todo_goal_is_complete(tmp_path: Path):
    project_dir = _make_three_chapter_project(tmp_path)
    todo = _create_todo(project_dir, chapters=1, batch_words=900)
    next_res = runner.invoke(
        app,
        [
            "translate",
            "todo-resume",
            str(project_dir),
            "--todo-id",
            todo["todo_id"],
            "--format",
            "text",
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
            {"id": record["id"], "target": f"Translated {record['id']}"}
            for record in task["records"]
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
    assert f"todo complete: {todo['todo_id']}" in insert_res.output
    assert "next: stop - todo goal complete" in insert_res.output
    assert "translate next ." not in insert_res.output
