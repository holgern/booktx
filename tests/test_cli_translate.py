"""CLI regressions for the command-based translation workflow."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from booktx.cli import app
from booktx.config import (
    find_source_file,
    init_project,
    load_project,
    translation_store_path,
)
from booktx.context import load_context, write_context

runner = CliRunner()

DOC = """\
# One

First sentence. Second sentence.

# Two

Third sentence. Fourth sentence.
"""


def _make_project(tmp_path: Path, *, protected_terms: list[str] | None = None) -> Path:
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
    if protected_terms:
        proj = load_project(project_dir)
        proj.names_path.write_text(
            json.dumps({"protected_terms": protected_terms}),
            encoding="utf-8",
        )
    ext = runner.invoke(app, ["extract", str(project_dir)])
    assert ext.exit_code == 0, ext.output
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    runner.invoke(
        app,
        [
            "context",
            "mark-ready",
            str(project_dir),
            "--force",
            "--reason",
            "test setup",
        ],
    )
    return project_dir


def _proj(project_dir: Path):
    return load_project(project_dir)


def _translated_dir(project_dir: Path) -> Path:
    path = _proj(project_dir).translated_dir
    assert path is not None
    return path


def _store_path(project_dir: Path) -> Path:
    return translation_store_path(_proj(project_dir))


def _tasks_dir(project_dir: Path) -> Path:
    path = _proj(project_dir).tasks_dir
    assert path is not None
    return path


def _ledger_path(project_dir: Path) -> Path:
    path = _proj(project_dir).ledger_path
    assert path is not None
    return path


def _identity_legacy_chunk(project_dir: Path, chunk_id: str) -> None:
    chunk_path = project_dir / ".booktx" / "chunks" / f"{chunk_id}.json"
    chunk = json.loads(chunk_path.read_text("utf-8"))
    payload = {
        "chunk_id": chunk_id,
        "records": [
            {"id": record["id"], "target": record["source"]}
            for record in chunk["records"]
        ],
    }
    (_translated_dir(project_dir) / f"{chunk_id}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _write_legacy_store(project_dir: Path, payload: dict[str, object]) -> None:
    _store_path(project_dir).write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _insert_identity_target(
    project_dir: Path,
    *,
    task_id: str | None = None,
    record_id: str | None = None,
    target: str | None = None,
):
    next_res = runner.invoke(
        app,
        ["translate", "next", str(project_dir), "--unit", "paragraph", "--json"],
    )
    assert next_res.exit_code == 0, next_res.output
    task = json.loads(next_res.output)
    record = task["records"][0]
    payload = {
        "task_id": task_id or task["task_id"],
        "records": [
            {
                "id": record_id or record["id"],
                "target": target or record["source"],
            }
        ],
    }
    res = runner.invoke(
        app,
        [
            "translate",
            "insert",
            str(project_dir),
            "--task-id",
            task_id or task["task_id"],
            "--stdin",
        ],
        input=json.dumps(payload),
    )
    assert res.exit_code == 0, res.output
    return task, record, res


def test_status_json_reports_totals_before_translation(tmp_path: Path):
    project_dir = _make_project(tmp_path)

    res = runner.invoke(app, ["status", str(project_dir), "--json"])

    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["totals"]["records_remaining"] == data["totals"]["records_total"]
    assert data["totals"]["chapters_pending"] == 2
    assert data["totals"]["chunks_pending"] == data["totals"]["chunks_total"]


def test_status_and_translate_next_respect_boundary_overlap(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    _identity_legacy_chunk(project_dir, "0002")

    status_res = runner.invoke(
        app,
        ["status", str(project_dir), "--chapter", "0002", "--json"],
    )

    assert status_res.exit_code == 0, status_res.output
    status = json.loads(status_res.output)
    chapter = status["chapters"][0]
    assert chapter["record_range"]["start"] == "0002-000002"
    assert chapter["record_range"]["end"] == "0003-000002"
    assert chapter["records_total"] == 3
    assert chapter["records_translated"] == 1
    assert chapter["pending_chunk_ids"] == ["0003"]

    next_res = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--chapter",
            "0002",
            "--unit",
            "chapter",
            "--json",
        ],
    )

    assert next_res.exit_code == 0, next_res.output
    payload = json.loads(next_res.output)
    assert [record["id"] for record in payload["records"]] == [
        "0003-000001",
        "0003-000002",
    ]


def test_translate_next_creates_ingest_file_and_insert_updates_store(tmp_path: Path):
    project_dir = _make_project(tmp_path)

    next_res = runner.invoke(
        app,
        ["translate", "next", str(project_dir), "--unit", "paragraph", "--json"],
    )
    assert next_res.exit_code == 0, next_res.output
    task = json.loads(next_res.output)
    ingest_file = project_dir / task["ingest_path"]

    assert task["translation_version"] == "1.1"
    assert task["baseline_ref"] == "1.1"
    assert task["baseline_sha256"]
    assert task["context_sha256"]
    assert task["context_view_sha256"] == task["context_sha256"]
    assert task["context_view_path"].endswith("/context.json")
    assert task["context_notes_scope"] == "before_target_chapter"
    assert task["context_target_chapter_id"] == task["chapter_id"]
    assert task["source_sha256"]
    assert task["ingest_path"].startswith("translations/de_default/ingest/")
    assert f"--json-file {task['ingest_path']}" in task["submit_hint"]
    assert ingest_file.is_file()
    template = json.loads(ingest_file.read_text("utf-8"))
    assert template["schema_version"] == 2
    assert template["task_id"] == task["task_id"]
    assert template["translation_version"] == "1.1"
    assert [record["target"] for record in template["records"]] == [""] * len(
        task["records"]
    )

    payload = template
    payload["records"] = [
        {"id": record["id"], "target": record["source"]} for record in task["records"]
    ]
    ingest_file.write_text(json.dumps(payload), encoding="utf-8")
    insert_res = runner.invoke(
        app,
        [
            "translate",
            "insert",
            str(project_dir),
            "--task-id",
            task["task_id"],
            "--json-file",
            str(ingest_file),
        ],
    )

    assert insert_res.exit_code == 0, insert_res.output
    assert "version: 1.1" in insert_res.output
    assert ingest_file.is_file()
    assert _store_path(project_dir).is_file()
    assert not list(_translated_dir(project_dir).glob("*.json"))

    status_res = runner.invoke(app, ["status", str(project_dir), "--json"])
    status = json.loads(status_res.output)
    assert status["totals"]["records_translated"] >= len(task["records"])


def test_translate_next_block_template_includes_translation_version(tmp_path: Path):
    project_dir = _make_project(tmp_path)

    next_res = runner.invoke(
        app,
        ["translate", "next", str(project_dir), "--unit", "paragraph", "--json"],
    )
    assert next_res.exit_code == 0, next_res.output
    task = json.loads(next_res.output)
    block_file = project_dir / task["block_ingest_path"]

    text = block_file.read_text("utf-8")
    assert "# translation_version: 1.1" in text
    assert "# baseline: 1.1" in text
    assert "# baseline_sha256: " in text
    assert "# context_view_sha256: " in text
    assert "# context_view_path: " in text


def test_translate_insert_tsv_accepts_batch(tmp_path: Path):
    project_dir = _make_project(tmp_path)

    next_res = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--unit",
            "batch",
            "--max-words",
            "20",
            "--json",
        ],
    )
    task = json.loads(next_res.output)
    tsv = (
        "\n".join(f"{record['id']}\t{record['source']}" for record in task["records"])
        + "\n"
    )

    insert_res = runner.invoke(
        app,
        [
            "translate",
            "insert",
            str(project_dir),
            "--task-id",
            task["task_id"],
            "--stdin",
            "--format",
            "tsv",
        ],
        input=tsv,
    )

    assert insert_res.exit_code == 0, insert_res.output
    assert "accepted:" in insert_res.output


def test_translate_insert_block_accepts_batch(tmp_path: Path):
    project_dir = _make_project(tmp_path)

    next_res = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--unit",
            "batch",
            "--max-words",
            "20",
            "--json",
        ],
    )
    task = json.loads(next_res.output)
    block = (
        "\n\n".join(
            f">>> {record['id']}\n{record['source']}" for record in task["records"]
        )
        + "\n"
    )

    insert_res = runner.invoke(
        app,
        [
            "translate",
            "insert",
            str(project_dir),
            "--task-id",
            task["task_id"],
            "--stdin",
            "--format",
            "block",
        ],
        input=block,
    )

    assert insert_res.exit_code == 0, insert_res.output
    assert "accepted:" in insert_res.output
    store = json.loads(_store_path(project_dir).read_text("utf-8"))
    for record in task["records"]:
        assert record["id"] in store["records"]


def test_translate_insert_block_file_accepts_batch(tmp_path: Path):
    project_dir = _make_project(tmp_path)

    next_res = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--unit",
            "batch",
            "--max-words",
            "20",
            "--json",
        ],
    )
    task = json.loads(next_res.output)
    block_file = project_dir / task["block_ingest_path"]
    block_file.write_text(
        "\n\n".join(
            f">>> {record['id']}\n{record['source']}" for record in task["records"]
        )
        + "\n",
        encoding="utf-8",
    )

    insert_res = runner.invoke(
        app,
        [
            "translate",
            "insert",
            str(project_dir),
            "--task-id",
            task["task_id"],
            "--file",
            str(block_file),
            "--format",
            "block",
        ],
    )

    assert insert_res.exit_code == 0, insert_res.output
    assert "accepted:" in insert_res.output


def test_translate_insert_accepts_task_after_live_baseline_change(tmp_path: Path):
    project_dir = _make_project(tmp_path)

    next_res = runner.invoke(
        app,
        ["translate", "next", str(project_dir), "--unit", "paragraph", "--json"],
    )
    assert next_res.exit_code == 0, next_res.output
    task = json.loads(next_res.output)
    ingest_file = project_dir / task["ingest_path"]
    payload = json.loads(ingest_file.read_text("utf-8"))
    payload["records"][0]["target"] = task["records"][0]["source"]
    ingest_file.write_text(json.dumps(payload), encoding="utf-8")

    proj = load_project(project_dir)
    ctx = load_context(proj)
    assert ctx is not None
    ctx.global_rules.append("Prefer shorter German clauses.")
    write_context(proj, ctx)

    insert_res = runner.invoke(
        app,
        [
            "translate",
            "insert",
            str(project_dir),
            "--task-id",
            task["task_id"],
            "--json-file",
            str(ingest_file),
        ],
    )

    assert insert_res.exit_code == 0, insert_res.output
    assert "version: 1.1" in insert_res.output
    store = json.loads(_store_path(project_dir).read_text("utf-8"))
    candidate = store["records"][task["records"][0]["id"]]["versions"][0]
    assert candidate["version_ref"] == "1.1"
    assert candidate["baseline_ref"] == task["baseline_ref"]
    assert candidate["baseline_sha256"] == task["baseline_sha256"]
    assert candidate["context_view_sha256"] == task["context_view_sha256"]


def test_translate_insert_legacy_task_without_translation_version_remains_accepted(
    tmp_path: Path,
):
    project_dir = _make_project(tmp_path)

    next_res = runner.invoke(
        app,
        ["translate", "next", str(project_dir), "--unit", "paragraph", "--json"],
    )
    assert next_res.exit_code == 0, next_res.output
    task = json.loads(next_res.output)
    task_path = _tasks_dir(project_dir) / f"{task['task_id']}.json"
    task_payload = json.loads(task_path.read_text("utf-8"))
    task_payload.pop("translation_version", None)
    task_path.write_text(json.dumps(task_payload), encoding="utf-8")

    ingest_file = project_dir / task["ingest_path"]
    payload = json.loads(ingest_file.read_text("utf-8"))
    payload["records"][0]["target"] = task["records"][0]["source"]
    ingest_file.write_text(json.dumps(payload), encoding="utf-8")

    insert_res = runner.invoke(
        app,
        [
            "translate",
            "insert",
            str(project_dir),
            "--task-id",
            task["task_id"],
            "--json-file",
            str(ingest_file),
        ],
    )

    assert insert_res.exit_code == 0, insert_res.output


def test_translate_insert_block_rejects_missing_header(tmp_path: Path):
    project_dir = _make_project(tmp_path)

    insert_res = runner.invoke(
        app,
        ["translate", "insert", str(project_dir), "--stdin", "--format", "block"],
        input="German target without an id\n",
    )

    assert insert_res.exit_code != 0
    assert "expected '>>> <record-id>'" in insert_res.output


def test_translate_insert_block_rejects_duplicate_id(tmp_path: Path):
    project_dir = _make_project(tmp_path)

    insert_res = runner.invoke(
        app,
        ["translate", "insert", str(project_dir), "--stdin", "--format", "block"],
        input=">>> 0001-000001\none\n\n>>> 0001-000001\ntwo\n",
    )

    assert insert_res.exit_code != 0
    assert "duplicate record id" in insert_res.output


def test_translate_insert_block_preserves_multiline_target(tmp_path: Path):
    project_dir = _make_project(tmp_path)

    next_res = runner.invoke(
        app,
        ["translate", "next", str(project_dir), "--unit", "paragraph", "--json"],
    )
    task = json.loads(next_res.output)
    record_id = task["records"][0]["id"]

    insert_res = runner.invoke(
        app,
        [
            "translate",
            "insert",
            str(project_dir),
            "--task-id",
            task["task_id"],
            "--stdin",
            "--format",
            "block",
        ],
        input=f">>> {record_id}\nEr sagte:\n„Geh jetzt.“\n",
    )

    assert insert_res.exit_code == 0, insert_res.output
    store = json.loads(_store_path(project_dir).read_text("utf-8"))
    assert store["records"][record_id]["active_version"] == "1.1"
    assert (
        store["records"][record_id]["versions"][0]["target"]
        == "Er sagte:\n„Geh jetzt.“"
    )


def test_translate_next_format_block_prints_concise_summary(tmp_path: Path):
    project_dir = _make_project(tmp_path)

    json_res = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--unit",
            "batch",
            "--max-words",
            "20",
            "--json",
        ],
    )
    assert json_res.exit_code == 0, json_res.output
    task = json.loads(json_res.output)
    first_record = task["records"][0]

    block_res = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--unit",
            "batch",
            "--max-words",
            "20",
            "--format",
            "block",
        ],
    )

    assert block_res.exit_code == 0, block_res.output
    # Concise default: task summary + file paths + submit/view hints only.
    assert "task:" in block_res.output
    # Each translate next call creates a new task; verify the output contains
    # the expected file patterns rather than comparing exact paths across tasks.
    assert ".source.block.txt" in block_res.output
    assert ".block.txt" in block_res.output
    assert "--format block" in block_res.output
    # Source text and heredoc body must NOT appear by default. Use a
    # distinctive prose source (not a heading that matches a chapter title).
    prose_source = next(r["source"] for r in task["records"] if "." in r["source"])
    assert f">>> {first_record['id']}" not in block_res.output
    assert "Sources:" not in block_res.output
    assert prose_source not in block_res.output
    assert "BOOKTX" not in block_res.output


def test_translate_next_format_block_show_sources(tmp_path: Path):
    project_dir = _make_project(tmp_path)

    json_res = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--unit",
            "batch",
            "--max-words",
            "20",
            "--json",
        ],
    )
    task = json.loads(json_res.output)
    first_record = task["records"][0]

    block_res = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--unit",
            "batch",
            "--max-words",
            "20",
            "--format",
            "block",
            "--show-sources",
        ],
    )

    assert block_res.exit_code == 0, block_res.output
    assert "Sources:" in block_res.output
    assert f">>> {first_record['id']}" in block_res.output
    assert first_record["source"] in block_res.output


def test_translate_next_format_block_show_template(tmp_path: Path):
    project_dir = _make_project(tmp_path)

    block_res = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--unit",
            "batch",
            "--max-words",
            "20",
            "--format",
            "block",
            "--show-template",
        ],
    )

    assert block_res.exit_code == 0, block_res.output
    assert "--stdin --format block <<'BOOKTX'" in block_res.output
    assert "BOOKTX" in block_res.output


def test_translate_next_creates_block_ingest_template(tmp_path: Path):
    project_dir = _make_project(tmp_path)

    next_res = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--unit",
            "batch",
            "--max-words",
            "20",
            "--json",
        ],
    )
    task = json.loads(next_res.output)
    json_ingest_file = project_dir / task["ingest_path"]
    block_ingest_file = project_dir / task["block_ingest_path"]

    assert json_ingest_file.is_file()
    assert block_ingest_file.is_file()
    headers = [
        line
        for line in block_ingest_file.read_text("utf-8").splitlines()
        if line.startswith(">>> ")
    ]
    assert headers == [f">>> {record['id']}" for record in task["records"]]


def test_invalid_insert_is_atomic(tmp_path: Path):
    project_dir = _make_project(tmp_path, protected_terms=["First", "Second"])

    next_res = runner.invoke(
        app,
        ["translate", "next", str(project_dir), "--unit", "paragraph", "--json"],
    )
    task = json.loads(next_res.output)
    before = _store_path(project_dir)
    before_text = before.read_text("utf-8") if before.is_file() else None

    payload = {
        "task_id": task["task_id"],
        "records": [{"id": task["records"][0]["id"], "target": "__NAME_999__ broken"}],
    }
    insert_res = runner.invoke(
        app,
        ["translate", "insert", str(project_dir), "--stdin"],
        input=json.dumps(payload),
    )

    assert insert_res.exit_code == 1
    assert "submission rejected" in insert_res.output
    after = _store_path(project_dir)
    after_text = after.read_text("utf-8") if after.is_file() else None
    assert after_text == before_text


def test_translate_import_legacy_and_export_roundtrip(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    _identity_legacy_chunk(project_dir, "0001")

    import_res = runner.invoke(app, ["translate", "import-legacy", str(project_dir)])
    assert import_res.exit_code == 0, import_res.output
    store = json.loads(_store_path(project_dir).read_text("utf-8"))
    assert any(record_id.startswith("0001-") for record_id in store["records"])

    legacy_file = _translated_dir(project_dir) / "0001.json"
    legacy_file.unlink()
    export_res = runner.invoke(app, ["translate", "export", str(project_dir)])
    assert export_res.exit_code == 0, export_res.output
    assert legacy_file.is_file()


def test_translate_migrate_store_dry_run_does_not_rewrite(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    next_res = runner.invoke(
        app,
        ["translate", "next", str(project_dir), "--unit", "paragraph", "--json"],
    )
    task = json.loads(next_res.output)
    record = task["records"][0]
    _write_legacy_store(
        project_dir,
        {
            "version": 1,
            "source_sha256": "abc123",
            "records": {
                record["id"]: {
                    "chunk_id": record["id"].split("-", 1)[0],
                    "source_sha256": "legacy",
                    "target": "Hallo.",
                    "status": "accepted",
                    "updated_at": "2026-06-22T12:00:00Z",
                }
            },
        },
    )

    res = runner.invoke(app, ["translate", "migrate-store", str(project_dir)])

    assert res.exit_code == 0, res.output
    assert "dry-run: would migrate 1 record(s)" in res.output
    data = json.loads(_store_path(project_dir).read_text("utf-8"))
    assert data["version"] == 1


def test_translate_migrate_store_write_creates_v2_and_ledger(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    chunk = json.loads(
        next((project_dir / ".booktx" / "chunks").glob("*.json")).read_text("utf-8")
    )
    record = chunk["records"][0]
    _write_legacy_store(
        project_dir,
        {
            "version": 1,
            "source_sha256": "abc123",
            "records": {
                record["id"]: {
                    "chunk_id": record["id"].split("-", 1)[0],
                    "source_sha256": "legacy",
                    "target": "Hallo.",
                    "status": "accepted",
                    "updated_at": "2026-06-22T12:00:00Z",
                }
            },
        },
    )

    res = runner.invoke(
        app,
        [
            "translate",
            "migrate-store",
            str(project_dir),
            "--write",
            "--actor",
            "user:nahrstaedt",
            "--harness",
            "pi",
            "--model",
            "codex-openai/gpt-5.5@low",
            "--context-label",
            "initial migrated context",
        ],
    )

    assert res.exit_code == 0, res.output
    store = json.loads(_store_path(project_dir).read_text("utf-8"))
    ledger = json.loads(_ledger_path(project_dir).read_text("utf-8"))
    assert store["version"] == 2
    migrated = store["records"][record["id"]]
    assert migrated["chunk_id"] == int(record["id"].split("-", 1)[0])
    assert migrated["part_id"] == 1
    assert migrated["source"] == record["source"]
    assert migrated["active_version"] == "1.1"
    assert migrated["versions"][0]["target"] == "Hallo."
    assert ledger["active_version"] == "1.1"
    assert ledger["tracks"]["1"]["actor"] == "user:nahrstaedt"
    assert (
        ledger["tracks"]["1"]["subversions"]["1"]["context_label"]
        == "initial migrated context"
    )


def test_translate_migrate_store_write_fails_on_missing_source(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    _write_legacy_store(
        project_dir,
        {
            "version": 1,
            "source_sha256": "abc123",
            "records": {
                "0001-999999": {
                    "chunk_id": "0001",
                    "source_sha256": "legacy",
                    "target": "Ghost record.",
                    "status": "accepted",
                    "updated_at": "2026-06-22T12:00:00Z",
                }
            },
        },
    )

    res = runner.invoke(
        app, ["translate", "migrate-store", str(project_dir), "--write"]
    )

    assert res.exit_code == 1
    assert "cannot migrate store with missing source records" in res.output


def test_translation_get_record_json_and_human_output(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    task, record, _ = _insert_identity_target(project_dir)

    proj = load_project(project_dir)
    ctx = load_context(proj)
    assert ctx is not None
    ctx.global_rules.append("Prefer shorter German clauses.")
    write_context(proj, ctx)

    res = runner.invoke(
        app,
        [
            "translate",
            "set-record",
            str(project_dir),
            "--task-id",
            task["task_id"],
            "--record-id",
            record["id"],
            "--target",
            "Andere Fassung.",
        ],
    )
    assert res.exit_code == 0, res.output

    json_res = runner.invoke(
        app,
        [
            "translation",
            "get-record",
            str(project_dir),
            "1@1",
            "--before",
            "0",
            "--after",
            "1",
            "--json",
        ],
    )
    assert json_res.exit_code == 0, json_res.output
    payload = json.loads(json_res.output)
    assert payload["selected_record_ref"] == "0001-000001"
    assert [item["version_ref"] for item in payload["available_targets"]] == [
        "1.1",
        "1.2",
    ]
    assert payload["after"][0]["id"] == "0001-000002"

    human_res = runner.invoke(
        app,
        ["translation", "get-record", str(project_dir), "0001-000001", "--after", "1"],
    )
    assert human_res.exit_code == 0, human_res.output
    assert ">> 0001-000001" in human_res.output


def test_translation_list_range_uses_source_order(tmp_path: Path):
    project_dir = _make_project(tmp_path)

    res = runner.invoke(
        app,
        [
            "translation",
            "list",
            str(project_dir),
            "--range",
            "1@2..2@1",
            "--json",
        ],
    )

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert [record["id"] for record in payload["records"]] == [
        "0001-000002",
        "0002-000001",
    ]


def test_translation_activate_review_compare_and_version_commands(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    assert (
        runner.invoke(
            app, ["actor", "set", str(project_dir), "user:nahrstaedt"]
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["harness", "set", str(project_dir), "pi"]).exit_code == 0
    assert (
        runner.invoke(
            app, ["model", "set", str(project_dir), "codex-openai/gpt-5.5@low"]
        ).exit_code
        == 0
    )

    whoami = runner.invoke(app, ["actor", "whoami", str(project_dir)])
    assert whoami.exit_code == 0
    assert whoami.output.strip() == "user:nahrstaedt"

    task, record, _ = _insert_identity_target(project_dir, target="Erste Fassung.")
    proj = load_project(project_dir)
    ctx = load_context(proj)
    assert ctx is not None
    ctx.global_rules.append("Prefer shorter German clauses.")
    write_context(proj, ctx)

    second = runner.invoke(
        app,
        [
            "translate",
            "set-record",
            str(project_dir),
            "--task-id",
            task["task_id"],
            "--record-id",
            record["id"],
            "--target",
            "Zweite Fassung.",
        ],
    )
    assert second.exit_code == 0, second.output

    compare_res = runner.invoke(
        app,
        [
            "translation",
            "compare",
            str(project_dir),
            "1@1",
            "--versions",
            "1.1,1.2",
            "--json",
        ],
    )
    assert compare_res.exit_code == 0, compare_res.output
    compare_payload = json.loads(compare_res.output)
    assert [item["target"] for item in compare_payload["comparisons"]] == [
        "Erste Fassung.",
        "Zweite Fassung.",
    ]

    activate_res = runner.invoke(
        app, ["translation", "activate", str(project_dir), "1@1", "1.2"]
    )
    assert activate_res.exit_code == 0, activate_res.output

    review_res = runner.invoke(
        app,
        [
            "translation",
            "review",
            str(project_dir),
            "1@1",
            "--activate",
            "1.2",
            "--note",
            "Better in context.",
        ],
    )
    assert review_res.exit_code == 0, review_res.output

    get_res = runner.invoke(
        app, ["translation", "get-record", str(project_dir), "1@1", "--json"]
    )
    payload = json.loads(get_res.output)
    assert payload["active_version"] == "1.2"
    assert payload["available_targets"][1]["review_note"] == "Better in context."

    current_res = runner.invoke(app, ["version", "current", str(project_dir), "--json"])
    assert current_res.exit_code == 0, current_res.output
    current_payload = json.loads(current_res.output)
    assert current_payload["active_version"] == "1.2"

    show_res = runner.invoke(
        app, ["version", "show", str(project_dir), "1.2", "--json"]
    )
    assert show_res.exit_code == 0, show_res.output
    show_payload = json.loads(show_res.output)
    assert show_payload["version_ref"] == "1.2"

    fork_res = runner.invoke(
        app,
        ["version", "fork-context", str(project_dir), "--note", "manual split"],
    )
    assert fork_res.exit_code == 0, fork_res.output
    assert fork_res.output.strip() == "1.3"


def test_whoami_reports_active_version_and_scoped_identity(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    assert (
        runner.invoke(
            app, ["actor", "set", str(project_dir), "user:nahrstaedt"]
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["harness", "set", str(project_dir), "pi"]).exit_code == 0
    assert (
        runner.invoke(
            app, ["model", "set", str(project_dir), "codex-openai/gpt-5.5@low"]
        ).exit_code
        == 0
    )

    _insert_identity_target(project_dir, target="Erste Fassung.")

    res = runner.invoke(app, ["whoami", str(project_dir), "--json"])

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["actor"] == "user:nahrstaedt"
    assert payload["harness"] == "pi"
    assert payload["model"] == "codex-openai/gpt-5.5@low"
    assert payload["active_version"] == "1.1"
    assert payload["context"]["exists"] is True
    assert payload["context"]["ready"] is True
    assert payload["context"]["sha256"]
    assert payload["store"]["exists"] is True
    assert payload["store"]["version"] == 2
    assert payload["store"]["record_count"] >= 1
    assert runner.invoke(app, ["identity", "whoami", str(project_dir)]).exit_code == 0
    assert (
        runner.invoke(app, ["harness", "whoami", str(project_dir)]).output.strip()
        == "pi"
    )
    assert (
        runner.invoke(app, ["model", "whoami", str(project_dir)]).output.strip()
        == "codex-openai/gpt-5.5@low"
    )


def test_translate_export_can_select_exact_version(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    next_res = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--unit",
            "batch",
            "--max-words",
            "20",
            "--json",
        ],
    )
    assert next_res.exit_code == 0, next_res.output
    task = json.loads(next_res.output)
    record = task["records"][0]
    payload = {
        "task_id": task["task_id"],
        "records": [
            {
                "id": item["id"],
                "target": "Erste Fassung."
                if item["id"] == record["id"]
                else item["source"],
            }
            for item in task["records"]
        ],
    }
    insert_res = runner.invoke(
        app,
        [
            "translate",
            "insert",
            str(project_dir),
            "--task-id",
            task["task_id"],
            "--stdin",
        ],
        input=json.dumps(payload),
    )
    assert insert_res.exit_code == 0, insert_res.output
    proj = load_project(project_dir)
    ctx = load_context(proj)
    assert ctx is not None
    ctx.global_rules.append("Prefer shorter German clauses.")
    write_context(proj, ctx)

    second = runner.invoke(
        app,
        [
            "translate",
            "set-record",
            str(project_dir),
            "--task-id",
            task["task_id"],
            "--record-id",
            record["id"],
            "--target",
            "Zweite Fassung.",
        ],
    )
    assert second.exit_code == 0, second.output
    activate_res = runner.invoke(
        app, ["translation", "activate", str(project_dir), "1@1", "1.2"]
    )
    assert activate_res.exit_code == 0, activate_res.output

    export_res = runner.invoke(
        app,
        ["translate", "export", str(project_dir), "--version", "1.1"],
    )
    assert export_res.exit_code == 0, export_res.output
    exported = json.loads(
        (_translated_dir(project_dir) / "0001.json").read_text("utf-8")
    )
    assert exported["records"][0]["version"] == "1.1"
    assert exported["records"][0]["target"] == "Erste Fassung."


def test_build_cli_require_complete_fails_with_missing_records(tmp_path: Path):
    project_dir = _make_project(tmp_path)

    res = runner.invoke(app, ["build", str(project_dir), "--require-complete"])

    assert res.exit_code == 1
    assert "build requires complete translations" in res.output


def test_translate_next_writes_source_block_file(tmp_path: Path):
    project_dir = _make_project(tmp_path)

    next_res = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--unit",
            "batch",
            "--max-words",
            "20",
            "--json",
        ],
    )
    assert next_res.exit_code == 0, next_res.output
    task = json.loads(next_res.output)
    source_block = project_dir / task["source_block_path"]

    assert source_block.is_file()
    text = source_block.read_text("utf-8")
    assert f"# task: {task['task_id']}" in text
    for record in task["records"]:
        assert f">>> {record['id']}" in text
        assert record["source"] in text


def test_translate_insert_missing_file_is_concise(tmp_path: Path):
    project_dir = _make_project(tmp_path)

    next_res = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--unit",
            "batch",
            "--max-words",
            "20",
            "--json",
        ],
    )
    task = json.loads(next_res.output)

    res = runner.invoke(
        app,
        [
            "translate",
            "insert",
            str(project_dir),
            "--task-id",
            task["task_id"],
            "--file",
            "/tmp/bt-missing-test.block.txt",
            "--format",
            "block",
        ],
    )

    assert res.exit_code != 0
    assert "submission file not found" in res.output
    assert "Traceback" not in res.output


def test_translate_task_status_reports_missing_and_accepted(tmp_path: Path):
    project_dir = _make_project(tmp_path)

    next_res = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--unit",
            "batch",
            "--max-words",
            "20",
            "--json",
        ],
    )
    task = json.loads(next_res.output)
    first_id = task["records"][0]["id"]
    second_id = task["records"][1]["id"]

    insert_res = runner.invoke(
        app,
        [
            "translate",
            "insert",
            str(project_dir),
            "--task-id",
            task["task_id"],
            "--record-id",
            first_id,
            "--target",
            "Erstens.",
        ],
    )
    assert insert_res.exit_code == 0, insert_res.output

    status_res = runner.invoke(
        app,
        [
            "translate",
            "task-status",
            str(project_dir),
            "--task-id",
            task["task_id"],
            "--json",
        ],
    )
    assert status_res.exit_code == 1
    payload = json.loads(status_res.output)
    assert payload["records_accepted"] == 1
    assert payload["records_missing"] == len(task["records"]) - 1
    assert payload["first_missing"] == second_id


def test_block_parser_ignores_generated_comments(tmp_path: Path):
    project_dir = _make_project(tmp_path)

    next_res = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--unit",
            "batch",
            "--max-words",
            "20",
            "--json",
        ],
    )
    task = json.loads(next_res.output)
    block_file = project_dir / task["block_ingest_path"]
    # The generated template already has metadata comment headers; add a
    # target under each header and submit.
    lines = ["# extra leading comment", ""]
    for record in task["records"]:
        lines.append(f">>> {record['id']}")
        lines.append(f"target-{record['id']}")
    block_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    res = runner.invoke(
        app,
        [
            "translate",
            "insert",
            str(project_dir),
            "--task-id",
            task["task_id"],
            "--file",
            str(block_file),
            "--format",
            "block",
        ],
    )

    assert res.exit_code == 0, res.output
    store = json.loads(_store_path(project_dir).read_text("utf-8"))
    for record in task["records"]:
        assert (
            store["records"][record["id"]]["versions"][0]["target"]
            == f"target-{record['id']}"
        )


def test_record_stdin_commit(tmp_path: Path):
    project_dir = _make_project(tmp_path)

    next_res = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--unit",
            "batch",
            "--max-words",
            "20",
            "--json",
        ],
    )
    task = json.loads(next_res.output)
    record_id = task["records"][0]["id"]

    res = runner.invoke(
        app,
        [
            "translate",
            "set-record",
            str(project_dir),
            "--task-id",
            task["task_id"],
            "--record-id",
            record_id,
            "--stdin",
        ],
        input="Er sagte:\n„Geh jetzt.“",
    )

    assert res.exit_code == 0, res.output
    store = json.loads(_store_path(project_dir).read_text("utf-8"))
    assert (
        store["records"][record_id]["versions"][0]["target"]
        == "Er sagte:\n„Geh jetzt.“"
    )


def test_make_task_id_is_deterministic_across_processes(monkeypatch):
    """Identical record-id lists yield identical digest parts across calls.

    Python's built-in hash() is process-randomized; the task id must use a
    stable blake2s digest instead.
    """
    import hashlib
    from datetime import datetime, timezone

    from booktx import tasks

    record_ids = ["c0001-r0001", "c0001-r0002", "c0001-r0003"]

    class _FixedDatetime:
        @staticmethod
        def now(_tz=None):
            return datetime(2026, 6, 22, 12, 30, 5, tzinfo=timezone.utc)

    monkeypatch.setattr(tasks, "datetime", _FixedDatetime)
    first = tasks.make_task_id("ch01", record_ids[0], record_ids)
    second = tasks.make_task_id("ch01", record_ids[0], record_ids)
    assert first == second
    assert first.startswith("bt-task-20260622T123005Z-ch01-c0001r0001-")
    # Digest is the 8-hex-char blake2s(digest_size=4) of the joined ids.
    expected_digest = hashlib.blake2s(
        "|".join(record_ids).encode("utf-8"), digest_size=4
    ).hexdigest()
    assert first.endswith("-" + expected_digest)


def test_make_task_id_distinguishes_different_record_sets(monkeypatch):
    from datetime import datetime, timezone

    from booktx import tasks

    class _FixedDatetime:
        @staticmethod
        def now(_tz=None):
            return datetime(2026, 6, 22, 12, 30, 5, tzinfo=timezone.utc)

    monkeypatch.setattr(tasks, "datetime", _FixedDatetime)
    a = tasks.make_task_id("ch01", "c0001-r0001", ["c0001-r0001", "c0001-r0002"])
    b = tasks.make_task_id("ch01", "c0001-r0001", ["c0001-r0001", "c0001-r9999"])
    assert a != b


def test_translate_next_refuses_ready_context_with_unapproved_required_answers(
    tmp_path: Path,
):
    project_dir = _make_project(tmp_path)
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    ctx = load_context(load_project(project_dir))
    assert ctx is not None
    for q in ctx.questions:
        if q.required:
            q.answer = "agent filled"
            q.status = "answered"
            q.answer_source = "agent"
    ctx.ready = True
    ctx.ready_forced = False
    write_context(load_project(project_dir), ctx)
    res = runner.invoke(
        app, ["translate", "next", str(project_dir), "--format", "block"]
    )
    assert res.exit_code == 1
    assert "unapproved" in res.output


# --- EPUB inline-XHTML pre-write rejection --------------------------------


def test_translate_insert_rejects_missing_inline_tag_for_epub_record(
    tmp_path: Path,
):
    # ac-0009: translate insert rejects an EPUB inline-XHTML target missing a
    # required tag BEFORE writing translation-store.json.
    from ebooklib import epub

    proj = init_project(tmp_path / "book", target_language="de")
    book = epub.EpubBook()
    book.set_identifier("test")
    book.set_title("T")
    book.set_language("en")
    ch1 = epub.EpubHtml(title="C1", file_name="ch1.xhtml", lang="en")
    ch1.content = (
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        "<head><title>C1</title></head><body>"
        "<p>Alice met <strong>Bob</strong>.</p>"
        "</body></html>"
    )
    book.add_item(ch1)
    book.spine = ["nav", ch1]
    book.add_item(epub.EpubNav())
    book.add_item(epub.EpubNcx())
    book.toc = (ch1,)
    epub.write_epub(str(proj.source_dir / "book.epub"), book, {})
    find_source_file(proj)
    res = runner.invoke(app, ["extract", str(proj.root)])
    assert res.exit_code == 0, res.output
    proj = load_project(proj.root)
    # Simulate old project: strip source_markup from chunk records.
    for path in proj.chunks():
        chunk = json.loads(path.read_text("utf-8"))
        for r in chunk["records"]:
            r.pop("source_markup", None)
        path.write_text(json.dumps(chunk, ensure_ascii=False), encoding="utf-8")
    # Initialize context so translate next works.
    runner.invoke(app, ["context", "init", str(proj.root), "--non-interactive"])
    runner.invoke(
        app,
        ["context", "mark-ready", str(proj.root), "--force", "--reason", "test"],
    )
    # Request the next task.
    next_res = runner.invoke(
        app,
        ["translate", "next", str(proj.root), "--unit", "chapter", "--json"],
    )
    assert next_res.exit_code == 0, next_res.output
    task = json.loads(next_res.output)
    # Build a submission that drops the <strong> tag.
    records = []
    for r in task["records"]:
        if "<strong>" in r.get("source", ""):
            records.append({"id": r["id"], "target": "Hallo Welt."})
        else:
            records.append({"id": r["id"], "target": r["source"]})
    ingest_path = proj.profile_dir / "ingest" / f"{task['task_id']}.json"
    ingest_path.parent.mkdir(parents=True, exist_ok=True)
    ingest_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "profile": proj.profile,
                "task_id": task["task_id"],
                "translation_version": task.get("translation_version"),
                "records": records,
            }
        ),
        encoding="utf-8",
    )
    insert_res = runner.invoke(
        app,
        [
            "translate",
            "insert",
            str(proj.root),
            "--task-id",
            task["task_id"],
            "--json-file",
            str(ingest_path),
        ],
    )
    # Pre-write enforcement: insert should reject before writing the store,
    # never accepting the broken target.
    assert insert_res.exit_code != 0
    assert "inline_xhtml_preserved" in insert_res.output
    assert "accepted:" not in insert_res.output
    from booktx.config import load_translation_store

    store = load_translation_store(load_project(proj.root))
    assert all(
        candidate.target != "Hallo Welt."
        for stored in store.records.values()
        for candidate in stored.versions
    )


def test_translate_insert_stages_new_records_in_partially_translated_epub_chunk(
    tmp_path: Path,
):
    # ac-0002: submitting a new record in an already-partial chunk must not
    # crash the EPUB inline-XHTML preflight staging with a Pydantic
    # ValidationError (SubmittedRecord mixed into TranslatedChunk).
    from ebooklib import epub

    from booktx.config import find_source_file, init_project

    proj = init_project(tmp_path / "book", target_language="de")
    book = epub.EpubBook()
    book.set_identifier("test")
    book.set_title("T")
    book.set_language("en")
    ch1 = epub.EpubHtml(title="C1", file_name="ch1.xhtml", lang="en")
    ch1.content = (
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        "<head><title>C1</title></head><body>"
        "<p>Alpha sentence one.</p>"
        "<p>Beta sentence two.</p>"
        "<p>Gamma sentence three.</p>"
        "</body></html>"
    )
    book.add_item(ch1)
    book.spine = ["nav", ch1]
    book.add_item(epub.EpubNav())
    book.add_item(epub.EpubNcx())
    book.toc = (ch1,)
    epub.write_epub(str(proj.source_dir / "book.epub"), book, {})

    find_source_file(proj)
    assert runner.invoke(app, ["extract", str(proj.root)]).exit_code == 0
    runner.invoke(app, ["context", "init", str(proj.root), "--non-interactive"])
    runner.invoke(
        app,
        ["context", "mark-ready", str(proj.root), "--force", "--reason", "test"],
    )

    # The EPUB fixture deterministically produces >=2 records in one chunk.
    first = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(proj.root),
            "--unit",
            "batch",
            "--max-words",
            "1",
            "--json",
        ],
    )
    assert first.exit_code == 0, first.output
    first_task = json.loads(first.output)
    first_record = first_task["records"][0]
    first_chunk = first_record["chunk_id"]

    # Accept the early record so effective.chunks[first_chunk] exists but is
    # partial (only the first record is accepted).
    first_ingest = proj.profile_dir / "ingest" / f"{first_task['task_id']}.json"
    first_ingest.parent.mkdir(parents=True, exist_ok=True)
    first_ingest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "profile": proj.profile,
                "task_id": first_task["task_id"],
                "translation_version": first_task.get("translation_version"),
                "records": [{"id": first_record["id"], "target": "Alpha Satz eins."}],
            }
        ),
        encoding="utf-8",
    )
    first_insert = runner.invoke(
        app,
        [
            "translate",
            "insert",
            str(proj.root),
            "--task-id",
            first_task["task_id"],
            "--json-file",
            str(first_ingest),
        ],
    )
    assert first_insert.exit_code == 0, first_insert.output

    # Now request the next task: a later record in the same chunk that is not
    # yet present in the effective (partial) chunk.
    second = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(proj.root),
            "--unit",
            "batch",
            "--max-words",
            "1",
            "--json",
        ],
    )
    assert second.exit_code == 0, second.output
    later = json.loads(second.output)
    later_record = later["records"][0]
    assert later_record["chunk_id"] == first_chunk
    assert later_record["id"] != first_record["id"]

    # Submit a later record in the already-partial chunk. The old bug crashed
    # here with a Pydantic ValidationError for TranslatedChunk.
    second_ingest = proj.profile_dir / "ingest" / f"{later['task_id']}.json"
    second_ingest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "profile": proj.profile,
                "task_id": later["task_id"],
                "translation_version": later.get("translation_version"),
                "records": [{"id": later_record["id"], "target": "Beta Satz zwei."}],
            }
        ),
        encoding="utf-8",
    )
    second_insert = runner.invoke(
        app,
        [
            "translate",
            "insert",
            str(proj.root),
            "--task-id",
            later["task_id"],
            "--json-file",
            str(second_ingest),
        ],
    )

    assert "ValidationError" not in second_insert.output
    assert "TranslatedChunk" not in second_insert.output
    assert "SubmittedRecord" not in second_insert.output
    assert second_insert.exit_code == 0, second_insert.output


def test_translate_revise_record_succeeds(tmp_path: Path):
    """ac-0007: revise-record accepts a new target and writes to the store."""
    project_dir = _make_project(tmp_path)
    task, record, _ = _insert_identity_target(project_dir)
    record_id = record["id"]

    # Revise the record with a new target.
    res = runner.invoke(
        app,
        [
            "translate",
            "revise-record",
            str(project_dir),
            record_id,
            "--target",
            "Revised translation for " + record_id,
        ],
    )

    assert res.exit_code == 0, res.output
    assert "revised:" in res.output
    assert record_id in res.output
    assert "recheck:" in res.output


def test_translate_revise_record_rejects_unknown_record(tmp_path: Path):
    """ac-0008: revise-record rejects unknown record ids."""
    project_dir = _make_project(tmp_path)

    res = runner.invoke(
        app,
        [
            "translate",
            "revise-record",
            str(project_dir),
            "9999-999999",
            "--target",
            "some text",
        ],
    )

    assert res.exit_code != 0
    assert "no matching source" in res.output or "error" in res.output


def test_translate_revise_record_rejects_empty_target(tmp_path: Path):
    """ac-0008: revise-record rejects empty targets."""
    project_dir = _make_project(tmp_path)
    task, record, _ = _insert_identity_target(project_dir)
    record_id = record["id"]

    res = runner.invoke(
        app,
        [
            "translate",
            "revise-record",
            str(project_dir),
            record_id,
            "--target",
            "",
        ],
    )

    assert res.exit_code != 0
    assert "empty" in res.output.lower()


def test_translate_revise_record_store_stays_valid(tmp_path: Path):
    """ac-0007: after revision, the store is valid Pydantic JSON."""
    project_dir = _make_project(tmp_path)
    task, record, _ = _insert_identity_target(project_dir)
    record_id = record["id"]

    runner.invoke(
        app,
        [
            "translate",
            "revise-record",
            str(project_dir),
            record_id,
            "--target",
            "Revised " + record_id,
        ],
    )

    # Reload the store and verify it parses.
    store_path = _store_path(project_dir)
    raw = json.loads(store_path.read_text("utf-8"))
    assert "records" in raw
    assert record_id in raw["records"]
    stored = raw["records"][record_id]
    assert "active_version" in stored
    assert "versions" in stored
    # Verify the revised target is present.
    av = stored["active_version"]
    versions = [v for v in stored["versions"] if v["version_ref"] == av]
    assert len(versions) == 1
    assert versions[0]["target"] == "Revised " + record_id


# --- outer_quotation_marks_preserved CLI regression ---------------------


QUOTED_DOC = """\
‘They won’t make any allowances for me if they catch us.’
"""


def _make_quoted_project(tmp_path: Path) -> Path:
    """Project whose first source record is fully outer-quoted."""
    src = tmp_path / "quoted.md"
    src.write_text(QUOTED_DOC, encoding="utf-8")
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
    assert runner.invoke(app, ["extract", str(project_dir)]).exit_code == 0
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    runner.invoke(
        app,
        [
            "context",
            "mark-ready",
            str(project_dir),
            "--force",
            "--reason",
            "test setup",
        ],
    )
    return project_dir


def _first_task_record(project_dir: Path):
    next_res = runner.invoke(
        app,
        ["translate", "next", str(project_dir), "--unit", "paragraph", "--json"],
    )
    assert next_res.exit_code == 0, next_res.output
    task = json.loads(next_res.output)
    return task, task["records"][0]


def test_translate_insert_rejects_missing_final_quote(tmp_path: Path):
    project_dir = _make_quoted_project(tmp_path)
    task, record = _first_task_record(project_dir)
    record_id = record["id"]
    before = _store_path(project_dir)
    before_text = before.read_text("utf-8") if before.is_file() else None

    # Block submission: target opens „ but never closes.
    block = f">>> {record_id}\n„Sie werden keine Rücksicht auf mich nehmen.\n"
    insert_res = runner.invoke(
        app,
        [
            "translate",
            "insert",
            str(project_dir),
            "--task-id",
            task["task_id"],
            "--stdin",
            "--format",
            "block",
        ],
        input=block,
    )

    assert insert_res.exit_code == 1, insert_res.output
    assert "submission rejected" in insert_res.output
    assert "outer_quotation_marks_preserved" in insert_res.output
    # The offending source/target must be rendered for an actionable rejection.
    assert "source:" in insert_res.output
    assert "target:" in insert_res.output
    # The candidate must NOT have been written.
    after = _store_path(project_dir)
    after_text = after.read_text("utf-8") if after.is_file() else None
    assert after_text == before_text


def test_translate_revise_record_rejects_missing_final_quote(tmp_path: Path):
    project_dir = _make_quoted_project(tmp_path)
    task, record = _first_task_record(project_dir)
    record_id = record["id"]

    # First accept a fully valid target („...") so a record exists to revise.
    valid_block = f">>> {record_id}\n„Sie werden keine Rücksicht auf mich nehmen.“\n"
    ok_res = runner.invoke(
        app,
        [
            "translate",
            "insert",
            str(project_dir),
            "--task-id",
            task["task_id"],
            "--stdin",
            "--format",
            "block",
        ],
        input=valid_block,
    )
    assert ok_res.exit_code == 0, ok_res.output
    store = json.loads(_store_path(project_dir).read_text("utf-8"))
    active = store["records"][record_id]["active_version"]
    valid_versions = [
        v for v in store["records"][record_id]["versions"] if v["version_ref"] == active
    ]
    assert len(valid_versions) == 1 and valid_versions[0]["target"].endswith("“")

    # Now revise to a target that drops the closing quote.
    rev_res = runner.invoke(
        app,
        [
            "translate",
            "revise-record",
            str(project_dir),
            record_id,
            "--stdin",
        ],
        input="„Sie werden keine Rücksicht auf mich nehmen.\n",
    )

    assert rev_res.exit_code == 1, rev_res.output
    assert "submission rejected" in rev_res.output
    assert "outer_quotation_marks_preserved" in rev_res.output
    assert "source:" in rev_res.output
    assert "target:" in rev_res.output

    # The store must be unchanged: the active target still ends with the valid “.
    store_after = json.loads(_store_path(project_dir).read_text("utf-8"))
    active_after = store_after["records"][record_id]["active_version"]
    active_versions = [
        v
        for v in store_after["records"][record_id]["versions"]
        if v["version_ref"] == active_after
    ]
    assert len(active_versions) == 1 and active_versions[0]["target"].endswith("“")


# ---------------------------------------------------------------------------
# translate search: behavioral coverage (Phase 0 defect repair)
#
# The --record non-jsonl branch previously crashed with
# AttributeError: 'SourceRecordView' has no attribute 'chapter_id'.
# ---------------------------------------------------------------------------


def _write_one_record_v2_store(project_dir: Path) -> str:
    """Write a v2 store with one accepted translation for the first record.

    Returns the record id."""
    from booktx.config import (
        write_translation_store,
        write_translation_version_ledger,
    )
    from booktx.models import (
        StoredTranslationRecordV2,
        TranslationCandidate,
        TranslationStoreV2,
        TranslationSubversionLedgerEntry,
        TranslationTrackLedgerEntry,
        TranslationVersionLedger,
    )
    from booktx.progress import source_record_sha256

    proj = _proj(project_dir)
    chunk = json.loads(sorted(proj.chunks_dir.glob("*.json"))[0].read_text("utf-8"))
    rec = chunk["records"][0]
    target = rec["source"] + " [de]"
    store = TranslationStoreV2(
        records={
            rec["id"]: StoredTranslationRecordV2(
                chunk_id=1,
                part_id=1,
                source_sha256=source_record_sha256(rec["source"]),
                source=rec["source"],
                active_version="1.1",
                versions=[
                    TranslationCandidate(
                        version=1,
                        subversion=1,
                        version_ref="1.1",
                        target=target,
                        status="accepted",
                        created_at="2026-06-22T12:00:00Z",
                        updated_at="2026-06-22T12:00:00Z",
                    )
                ],
            )
        }
    )
    write_translation_store(proj, store)
    write_translation_version_ledger(
        proj,
        TranslationVersionLedger(
            active_version="1.1",
            tracks={
                "1": TranslationTrackLedgerEntry(
                    version=1,
                    actor="user:test",
                    harness="pi",
                    model="human",
                    created_at="2026-06-22T12:00:00Z",
                    updated_at="2026-06-22T12:00:00Z",
                    subversions={
                        "1": TranslationSubversionLedgerEntry(
                            version=1,
                            subversion=1,
                            version_ref="1.1",
                            context_sha256="a" * 64,
                            created_at="2026-06-22T12:00:00Z",
                            updated_at="2026-06-22T12:00:00Z",
                        )
                    },
                )
            },
        ),
    )
    return rec["id"]


def test_translate_search_record_human_mode(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    record_id = _write_one_record_v2_store(project_dir)
    res = runner.invoke(
        app, ["translate", "search", str(project_dir), "--record", record_id]
    )
    assert res.exit_code == 0, res.output
    # No crash; chapter is printed from record_to_chapter, not source_view.
    assert "record:" in res.output
    assert "chapter=" in res.output
    assert "source:" in res.output
    assert "target:" in res.output
    assert "ref:" in res.output


def test_translate_search_record_jsonl(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    record_id = _write_one_record_v2_store(project_dir)
    res = runner.invoke(
        app,
        [
            "translate",
            "search",
            str(project_dir),
            "--record",
            record_id,
            "--jsonl",
        ],
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["id"] == record_id
    assert payload["target"].endswith("[de]")
    assert payload["effective_ref"]


def test_translate_search_target_match(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    _write_one_record_v2_store(project_dir)
    res = runner.invoke(
        app,
        ["translate", "search", str(project_dir), "--target", "[de]"],
    )
    assert res.exit_code == 0, res.output
    assert "found" in res.output
    assert "[de]" in res.output


def test_translate_search_source_match(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    _write_one_record_v2_store(project_dir)
    res = runner.invoke(
        app,
        ["translate", "search", str(project_dir), "--source", "First"],
    )
    assert res.exit_code == 0, res.output
    assert "found" in res.output


def test_translate_search_no_match(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    _write_one_record_v2_store(project_dir)
    res = runner.invoke(
        app,
        ["translate", "search", str(project_dir), "--target", "zzz-nope"],
    )
    assert res.exit_code == 0, res.output
    assert "found 0 matches" in res.output


def test_translate_search_record_rejects_unknown(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    res = runner.invoke(
        app,
        ["translate", "search", str(project_dir), "--record", "9999-999999"],
    )
    assert res.exit_code != 0
    assert "not found" in res.output
