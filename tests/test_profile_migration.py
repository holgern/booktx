"""Tests for migrating legacy single-layout projects into translation profiles."""

from __future__ import annotations

import json
from pathlib import Path

import tomli_w
from typer.testing import CliRunner

from booktx.cli import app
from booktx.config import load_project

runner = CliRunner()

DOC = """\
# One

Alice met Bob. They were happy.
"""


def _write_legacy_project(tmp_path: Path, *, include_target: bool = True) -> Path:
    project_dir = tmp_path / "legacy-book"
    source_dir = project_dir / "source"
    booktx_dir = project_dir / ".booktx"
    for path in (
        source_dir,
        booktx_dir / "translated",
        booktx_dir / "tasks",
        booktx_dir / "ingest",
        booktx_dir / "reports",
        booktx_dir / "chunks",
        project_dir / "output",
    ):
        path.mkdir(parents=True, exist_ok=True)
    (source_dir / "book.md").write_text(DOC, encoding="utf-8")
    config = {
        "source_language": "en",
        "source_file": "book.md",
        "format": "markdown",
        "chunk_size": 5,
    }
    if include_target:
        config["target_language"] = "de"
    (booktx_dir / "config.toml").write_bytes(tomli_w.dumps(config).encode("utf-8"))
    (booktx_dir / "names.json").write_text(
        json.dumps({"protected_terms": []}), encoding="utf-8"
    )
    assert runner.invoke(app, ["extract", str(project_dir)]).exit_code == 0
    assert (
        runner.invoke(
            app, ["context", "init", str(project_dir), "--non-interactive"]
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app, ["context", "mark-ready", str(project_dir), "--force"]
        ).exit_code
        == 0
    )
    next_res = runner.invoke(
        app, ["translate", "next", str(project_dir), "--unit", "paragraph", "--json"]
    )
    assert next_res.exit_code == 0, next_res.output
    first_chunk = json.loads(
        next((booktx_dir / "chunks").glob("*.json")).read_text("utf-8")
    )
    translated = {
        "chunk_id": first_chunk["chunk_id"],
        "records": [
            {"id": record["id"], "target": record["source"]}
            for record in first_chunk["records"]
        ],
    }
    (booktx_dir / "translated" / f"{first_chunk['chunk_id']}.json").write_text(
        json.dumps(translated), encoding="utf-8"
    )
    (project_dir / "output" / "book.de.md").write_text(
        "legacy output", encoding="utf-8"
    )
    return project_dir


def test_profile_migrate_current_dry_run_writes_nothing(tmp_path: Path):
    project_dir = _write_legacy_project(tmp_path)

    res = runner.invoke(
        app,
        [
            "profile",
            "migrate-current",
            str(project_dir),
            "de_gpt5_5",
            "--dry-run",
        ],
    )

    assert res.exit_code == 0, res.output
    assert (project_dir / ".booktx" / "config.toml").is_file()
    assert not (project_dir / ".booktx" / "source-config.toml").exists()
    assert not (project_dir / "translations" / "de_gpt5_5").exists()


def test_profile_migrate_current_moves_mutable_state_and_stamps_tasks(tmp_path: Path):
    project_dir = _write_legacy_project(tmp_path)

    res = runner.invoke(
        app,
        [
            "profile",
            "migrate-current",
            str(project_dir),
            "de_gpt5_5",
            "--select",
        ],
    )

    assert res.exit_code == 0, res.output
    profile_dir = project_dir / "translations" / "de_gpt5_5"
    assert not (project_dir / ".booktx" / "config.toml").exists()
    assert (project_dir / ".booktx" / "source-config.toml").is_file()
    assert (project_dir / ".booktx" / "chunks").is_dir()
    assert (project_dir / ".booktx" / "names.json").is_file()
    assert (profile_dir / "context.json").is_file()
    assert (profile_dir / "context.md").is_file()
    assert (profile_dir / "translated").is_dir()
    assert (profile_dir / "output" / "book.de.md").is_file()
    task_path = next((profile_dir / "tasks").glob("*.json"))
    task = json.loads(task_path.read_text("utf-8"))
    assert task["profile"] == "de_gpt5_5"
    assert task["target_locale"] == "de"
    assert load_project(project_dir).profile == "de_gpt5_5"


def test_profile_migrate_current_validate_and_build_work_after_migration(
    tmp_path: Path,
):
    project_dir = _write_legacy_project(tmp_path)
    assert (
        runner.invoke(
            app,
            ["profile", "migrate-current", str(project_dir), "de_gpt5_5", "--select"],
        ).exit_code
        == 0
    )

    validate_res = runner.invoke(
        app, ["validate", str(project_dir), "--profile", "de_gpt5_5"]
    )
    build_res = runner.invoke(
        app, ["build", str(project_dir), "--profile", "de_gpt5_5"]
    )

    assert validate_res.exit_code == 0, validate_res.output
    assert build_res.exit_code == 0, build_res.output
    assert (
        project_dir / "translations" / "de_gpt5_5" / "output" / "book.de.md"
    ).is_file()


def test_profile_migrate_current_rejects_existing_nonempty_profile_dir(tmp_path: Path):
    project_dir = _write_legacy_project(tmp_path)
    profile_dir = project_dir / "translations" / "de_gpt5_5"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "keep.txt").write_text("x", encoding="utf-8")

    res = runner.invoke(
        app, ["profile", "migrate-current", str(project_dir), "de_gpt5_5"]
    )

    assert res.exit_code != 0
    assert "migration target already exists and is not empty" in res.output


def test_profile_migrate_current_requires_target_when_legacy_target_missing(
    tmp_path: Path,
):
    project_dir = _write_legacy_project(tmp_path, include_target=False)

    res = runner.invoke(
        app, ["profile", "migrate-current", str(project_dir), "de_gpt5_5"]
    )

    assert res.exit_code != 0
    assert "legacy project has no target language" in res.output
