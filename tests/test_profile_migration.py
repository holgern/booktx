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
            app,
            [
                "context",
                "mark-ready",
                str(project_dir),
                "--force",
                "--reason",
                "test setup",
            ],
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


def _write_legacy_identity(
    booktx_dir: Path, *, actor: str, harness: str, model: str
) -> None:
    """Write a legacy .booktx/identity.json that migration must supersede."""
    from booktx.models import TranslationIdentity

    identity = TranslationIdentity(actor=actor, harness=harness, model=model)
    (booktx_dir / "identity.json").write_text(
        identity.model_dump_json(indent=2), encoding="utf-8"
    )


def test_profile_migrate_current_honors_model_override_when_legacy_identity_exists(
    tmp_path: Path,
):
    project_dir = _write_legacy_project(tmp_path)
    _write_legacy_identity(
        project_dir / ".booktx", actor="user:human", harness="booktx", model="human"
    )

    res = runner.invoke(
        app,
        [
            "profile",
            "migrate-current",
            str(project_dir),
            "de_gpt5_5",
            "--model",
            "codex-openai/gpt-5.5@low",
            "--select",
        ],
    )

    assert res.exit_code == 0, res.output
    identity_path = project_dir / "translations" / "de_gpt5_5" / "identity.json"
    assert identity_path.is_file()
    identity = json.loads(identity_path.read_text("utf-8"))
    assert identity["model"] == "codex-openai/gpt-5.5@low"

    who = runner.invoke(
        app, ["whoami", str(project_dir), "--profile", "de_gpt5_5", "--json"]
    )
    assert who.exit_code == 0, who.output
    payload = json.loads(who.output)
    assert payload["model"] == "codex-openai/gpt-5.5@low"


def test_profile_migrate_current_honors_actor_and_harness_overrides(tmp_path: Path):
    project_dir = _write_legacy_project(tmp_path)
    _write_legacy_identity(
        project_dir / ".booktx",
        actor="user:human",
        harness="booktx",
        model="human",
    )

    res = runner.invoke(
        app,
        [
            "profile",
            "migrate-current",
            str(project_dir),
            "de_gpt5_5",
            "--actor",
            "agent:translator",
            "--harness",
            "codex",
            "--select",
        ],
    )

    assert res.exit_code == 0, res.output
    identity = json.loads(
        (project_dir / "translations" / "de_gpt5_5" / "identity.json").read_text(
            "utf-8"
        )
    )
    assert identity["actor"] == "agent:translator"
    assert identity["harness"] == "codex"


def test_profile_migrate_current_creates_empty_profile_dirs_when_legacy_dirs_missing(
    tmp_path: Path,
):
    project_dir = _write_legacy_project(tmp_path)
    # Remove the optional mutable directories so the legacy project lacks them.
    import shutil as _shutil

    for name in ("tasks", "ingest", "translated", "reports"):
        target = project_dir / ".booktx" / name
        if target.exists():
            _shutil.rmtree(target)
    output_dir = project_dir / "output"
    if output_dir.exists():
        _shutil.rmtree(output_dir)

    res = runner.invoke(
        app,
        ["profile", "migrate-current", str(project_dir), "de_gpt5_5", "--select"],
    )

    assert res.exit_code == 0, res.output
    profile_dir = project_dir / "translations" / "de_gpt5_5"
    for name in ("tasks", "ingest", "translated", "reports", "output"):
        assert (profile_dir / name).is_dir(), f"missing profile dir: {name}"


def test_profile_migration_manifest_uses_project_relative_paths(tmp_path: Path):
    project_dir = _write_legacy_project(tmp_path)

    res = runner.invoke(
        app,
        ["profile", "migrate-current", str(project_dir), "de_gpt5_5", "--select"],
    )

    assert res.exit_code == 0, res.output
    migrations_dir = project_dir / ".booktx" / "migrations"
    manifest_path = next(migrations_dir.glob("*.json"))
    manifest = json.loads(manifest_path.read_text("utf-8"))
    assert manifest["moves"]
    for entry in manifest["moves"]:
        for key in ("source", "destination"):
            value = entry[key]
            assert not Path(value).is_absolute(), (
                f"manifest {key} must be project-relative, got: {value}"
            )
            assert str(project_dir.resolve()) not in value


def test_profile_migrate_current_does_not_remove_legacy_config_if_move_fails(
    tmp_path: Path, monkeypatch
):
    project_dir = _write_legacy_project(tmp_path)
    legacy_config = project_dir / ".booktx" / "config.toml"
    assert legacy_config.is_file()

    import shutil as _shutil

    import booktx.profile_migration as migration_module

    real_move = _shutil.move
    call_count = {"n": 0}

    def failing_move(src, dst):
        call_count["n"] += 1
        # Let at least one move happen, then fail to simulate a mid-migration error.
        if call_count["n"] >= 2:
            raise OSError("simulated migration failure")
        return real_move(str(src), str(dst))

    monkeypatch.setattr(migration_module.shutil, "move", failing_move)
    res = runner.invoke(
        app,
        ["profile", "migrate-current", str(project_dir), "de_gpt5_5"],
    )

    assert res.exit_code != 0
    # The legacy config.toml must survive a mid-migration failure so the
    # project is still loadable as a legacy project and the migration can be
    # retried.
    assert legacy_config.is_file(), "legacy config.toml must be preserved on failure"
