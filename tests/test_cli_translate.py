"""CLI regressions for the command-based translation workflow."""

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
    runner.invoke(app, ["context", "mark-ready", str(project_dir), "--force"])
    return project_dir


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
    (project_dir / ".booktx" / "translated" / f"{chunk_id}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


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

    assert task["ingest_path"].startswith(".booktx/ingest/")
    assert f"--json-file {task['ingest_path']}" in task["submit_hint"]
    assert ingest_file.is_file()
    template = json.loads(ingest_file.read_text("utf-8"))
    assert template["task_id"] == task["task_id"]
    assert [record["target"] for record in template["records"]] == [""] * len(
        task["records"]
    )

    payload = {
        "task_id": task["task_id"],
        "records": [
            {"id": record["id"], "target": record["source"]}
            for record in task["records"]
        ],
    }
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
    assert ingest_file.is_file()
    assert (project_dir / ".booktx" / "translation-store.json").is_file()
    assert not list((project_dir / ".booktx" / "translated").glob("*.json"))

    status_res = runner.invoke(app, ["status", str(project_dir), "--json"])
    status = json.loads(status_res.output)
    assert status["totals"]["records_translated"] >= len(task["records"])


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
    store = json.loads(
        (project_dir / ".booktx" / "translation-store.json").read_text("utf-8")
    )
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
    store = json.loads(
        (project_dir / ".booktx" / "translation-store.json").read_text("utf-8")
    )
    assert store["records"][record_id]["target"] == "Er sagte:\n„Geh jetzt.“"


def test_translate_next_format_block_prints_submit_hint(tmp_path: Path):
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
        ],
    )

    assert block_res.exit_code == 0, block_res.output
    assert "--format block" in block_res.output
    assert "--stdin" in block_res.output
    assert f">>> {first_record['id']}" in block_res.output
    assert "Sources:" in block_res.output
    assert first_record["source"] in block_res.output


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
    before = project_dir / ".booktx" / "translation-store.json"
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
    after = project_dir / ".booktx" / "translation-store.json"
    after_text = after.read_text("utf-8") if after.is_file() else None
    assert after_text == before_text


def test_translate_import_legacy_and_export_roundtrip(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    _identity_legacy_chunk(project_dir, "0001")

    import_res = runner.invoke(app, ["translate", "import-legacy", str(project_dir)])
    assert import_res.exit_code == 0, import_res.output
    store = json.loads(
        (project_dir / ".booktx" / "translation-store.json").read_text("utf-8")
    )
    assert any(record_id.startswith("0001-") for record_id in store["records"])

    legacy_file = project_dir / ".booktx" / "translated" / "0001.json"
    legacy_file.unlink()
    export_res = runner.invoke(app, ["translate", "export", str(project_dir)])
    assert export_res.exit_code == 0, export_res.output
    assert legacy_file.is_file()


def test_build_cli_require_complete_fails_with_missing_records(tmp_path: Path):
    project_dir = _make_project(tmp_path)

    res = runner.invoke(app, ["build", str(project_dir), "--require-complete"])

    assert res.exit_code == 1
    assert "build requires complete translations" in res.output
