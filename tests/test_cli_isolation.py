"""CLI tests for profile-root isolated mode."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from booktx.cli import app

runner = CliRunner()

DOC = """\
# One

First sentence. Second sentence.
"""


def _make_project(tmp_path: Path) -> tuple[Path, Path]:
    src = tmp_path / "book.md"
    src.write_text(DOC, encoding="utf-8")
    project_dir = tmp_path / "book"
    init_res = runner.invoke(
        app,
        ["init", str(project_dir), "--target", "de", "--source-file", str(src)],
    )
    assert init_res.exit_code == 0, init_res.output
    create_res = runner.invoke(
        app,
        ["profile", "create", str(project_dir), "fr_default", "--target", "fr"],
    )
    assert create_res.exit_code == 0, create_res.output
    extract_res = runner.invoke(app, ["extract", str(project_dir)])
    assert extract_res.exit_code == 0, extract_res.output
    init_ctx = runner.invoke(
        app, ["context", "init", str(project_dir), "--non-interactive"]
    )
    assert init_ctx.exit_code == 0, init_ctx.output
    ready_ctx = runner.invoke(
        app, ["context", "mark-ready", str(project_dir), "--force"]
    )
    assert ready_ctx.exit_code == 0, ready_ctx.output
    profile_root = project_dir / "translations" / "de_default"
    return project_dir, profile_root


def test_whoami_from_profile_root_redacts_project_root(monkeypatch, tmp_path: Path):
    project_dir, profile_root = _make_project(tmp_path)
    monkeypatch.chdir(profile_root)

    res = runner.invoke(app, ["whoami", "."])

    assert res.exit_code == 0, res.output
    assert "booktx identity: ." in res.output
    assert "context-history" not in res.output
    assert "fr_default" not in res.output
    assert str(project_dir) not in res.output
    assert "../" not in res.output
    assert "READY context.json" in res.output


def test_status_from_profile_root_redacts_project_root(monkeypatch, tmp_path: Path):
    project_dir, profile_root = _make_project(tmp_path)
    monkeypatch.chdir(profile_root)

    res = runner.invoke(app, ["status", "."])

    assert res.exit_code == 0, res.output
    assert "booktx status — ." in res.output
    assert "fr_default" not in res.output
    assert str(project_dir) not in res.output
    assert "../" not in res.output


def test_context_status_from_profile_root_uses_profile_local_path(
    monkeypatch, tmp_path: Path
):
    project_dir, profile_root = _make_project(tmp_path)
    monkeypatch.chdir(profile_root)

    res = runner.invoke(app, ["context", "status", "."])

    assert res.exit_code == 0, res.output
    assert "context: context.md" in res.output
    assert "fr_default" not in res.output
    assert str(project_dir) not in res.output
    assert "../" not in res.output


def test_profile_commands_are_blocked_in_profile_root_mode(monkeypatch, tmp_path: Path):
    _, profile_root = _make_project(tmp_path)
    monkeypatch.chdir(profile_root)

    for args in (
        ["profile", "list", "."],
        ["profile", "show", ".", "fr_default"],
        [
            "profile",
            "compare",
            ".",
            "--profiles",
            "de_default,fr_default",
            "--record",
            "0001-000001",
        ],
        ["pass-through", ".", "--profile", "passthrough_en"],
    ):
        res = runner.invoke(app, args)
        assert res.exit_code != 0
        assert "profile-root isolated mode" in res.output
        assert "../" not in res.output
        assert str(profile_root.parent.parent) not in res.output


def test_mode_source_and_doctor_commands_work_from_profile_root(
    monkeypatch, tmp_path: Path
):
    project_dir, profile_root = _make_project(tmp_path)
    monkeypatch.chdir(profile_root)

    mode_res = runner.invoke(app, ["mode", "."])
    status_res = runner.invoke(app, ["source", "status", "."])
    record_res = runner.invoke(app, ["source", "record", ".", "0001-000001"])
    chapter_res = runner.invoke(
        app, ["source", "chapter", ".", "0001", "--format", "block"]
    )
    doctor_res = runner.invoke(app, ["doctor", "isolation", "."])

    assert mode_res.exit_code == 0, mode_res.output
    assert "mode: profile-root" in mode_res.output
    assert "profiles visible: no" in mode_res.output
    assert "cross-profile access: no" in mode_res.output
    assert "safe for model evaluation: yes" in mode_res.output

    assert status_res.exit_code == 0, status_res.output
    assert "source: available" in status_res.output
    assert ".booktx/chunks" not in status_res.output

    assert record_res.exit_code == 0, record_res.output
    assert ">>> 0001-000001" in record_res.output
    assert str(project_dir) not in record_res.output
    assert "../" not in record_res.output

    assert chapter_res.exit_code == 0, chapter_res.output
    assert ">>> 0001-000001" in chapter_res.output
    assert str(project_dir) not in chapter_res.output
    assert "../" not in chapter_res.output

    assert doctor_res.exit_code == 0, doctor_res.output
    assert "isolation: PASS" in doctor_res.output
    assert "mode: profile-root" in doctor_res.output
    assert "cross-profile commands: blocked" in doctor_res.output
    assert "path redaction: PASS" in doctor_res.output


def test_translate_next_from_profile_root_keeps_output_and_artifacts_local(
    monkeypatch, tmp_path: Path
):
    project_dir, profile_root = _make_project(tmp_path)
    monkeypatch.chdir(profile_root)

    res = runner.invoke(
        app,
        [
            "translate",
            "next",
            ".",
            "--unit",
            "batch",
            "--max-words",
            "20",
            "--json",
        ],
    )

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["ingest_path"].startswith("ingest/")
    assert payload["block_ingest_path"].startswith("ingest/")
    assert payload["source_block_path"].startswith("tasks/")
    assert payload["context_view_path"].startswith("context-history/")
    assert "translations/" not in res.output
    assert "fr_default" not in res.output
    assert "../" not in res.output
    assert str(project_dir) not in res.output

    artifact_paths = [
        profile_root / "tasks" / f"{payload['task_id']}.json",
        profile_root / "tasks" / f"{payload['task_id']}.source.block.txt",
        profile_root / "ingest" / f"{payload['task_id']}.json",
        profile_root / "ingest" / f"{payload['task_id']}.block.txt",
    ]
    for artifact in artifact_paths:
        assert artifact.is_file()
        text = artifact.read_text("utf-8")
        assert str(project_dir) not in text
        assert "../" not in text
        assert ".booktx/chunks" not in text
        assert "fr_default" not in text


def test_validate_and_build_work_from_profile_root_without_path_leaks(
    monkeypatch, tmp_path: Path
):
    _, profile_root = _make_project(tmp_path)
    monkeypatch.chdir(profile_root)

    next_res = runner.invoke(
        app,
        ["translate", "next", ".", "--unit", "batch", "--max-words", "20", "--json"],
    )
    assert next_res.exit_code == 0, next_res.output
    payload = json.loads(next_res.output)
    ingest_path = profile_root / payload["ingest_path"]
    template = json.loads(ingest_path.read_text("utf-8"))
    template["records"] = [
        {"id": record["id"], "target": record["source"]}
        for record in payload["records"]
    ]
    ingest_path.write_text(json.dumps(template), encoding="utf-8")

    insert_res = runner.invoke(
        app,
        [
            "translate",
            "insert",
            ".",
            "--task-id",
            payload["task_id"],
            "--json-file",
            payload["ingest_path"],
        ],
    )
    assert insert_res.exit_code == 0, insert_res.output

    validate_res = runner.invoke(app, ["validate", "."])
    build_res = runner.invoke(app, ["build", "."])

    assert validate_res.exit_code == 0, validate_res.output
    assert "report: reports/" in validate_res.output
    assert "../" not in validate_res.output
    assert "/tmp/" not in validate_res.output

    assert build_res.exit_code == 0, build_res.output
    assert "output/" in build_res.output
    assert "../" not in build_res.output
    assert "/tmp/" not in build_res.output


def test_profile_root_mode_does_not_leak_sibling_profile_translations(
    monkeypatch, tmp_path: Path
):
    project_dir, profile_root = _make_project(tmp_path)
    sibling_ctx_init = runner.invoke(
        app,
        [
            "context",
            "init",
            str(project_dir),
            "--profile",
            "fr_default",
            "--non-interactive",
        ],
    )
    assert sibling_ctx_init.exit_code == 0, sibling_ctx_init.output
    sibling_ctx_ready = runner.invoke(
        app,
        [
            "context",
            "mark-ready",
            str(project_dir),
            "--profile",
            "fr_default",
            "--force",
        ],
    )
    assert sibling_ctx_ready.exit_code == 0, sibling_ctx_ready.output

    sibling_next = runner.invoke(
        app,
        [
            "translate",
            "next",
            str(project_dir),
            "--profile",
            "fr_default",
            "--unit",
            "batch",
            "--max-words",
            "20",
            "--json",
        ],
    )
    assert sibling_next.exit_code == 0, sibling_next.output
    sibling_payload = json.loads(sibling_next.output)
    sibling_ingest = project_dir / sibling_payload["ingest_path"]
    sibling_template = json.loads(sibling_ingest.read_text("utf-8"))
    sibling_template["records"] = [
        {"id": record["id"], "target": f"LEAK-CHECK-{idx}"}
        for idx, record in enumerate(sibling_payload["records"], start=1)
    ]
    sibling_ingest.write_text(json.dumps(sibling_template), encoding="utf-8")
    sibling_insert = runner.invoke(
        app,
        [
            "translate",
            "insert",
            str(project_dir),
            "--profile",
            "fr_default",
            "--task-id",
            sibling_payload["task_id"],
            "--json-file",
            str(sibling_ingest),
        ],
    )
    assert sibling_insert.exit_code == 0, sibling_insert.output

    monkeypatch.chdir(profile_root)

    next_res = runner.invoke(
        app,
        ["translate", "next", ".", "--unit", "batch", "--max-words", "20", "--json"],
    )
    list_res = runner.invoke(app, ["translation", "list", ".", "--chapter", "1"])

    assert next_res.exit_code == 0, next_res.output
    assert list_res.exit_code == 0, list_res.output
    assert "LEAK-CHECK" not in next_res.output
    assert "LEAK-CHECK" not in list_res.output
    assert "fr_default" not in next_res.output
    assert "fr_default" not in list_res.output
