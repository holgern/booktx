"""CLI tests for translation-profile management."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from booktx.cli import app
from booktx.config import load_profile_project, load_project, translation_store_path

runner = CliRunner()

DOC = """\
# Demo

Alice met Bob. They were happy.
"""


def _make_source_project(tmp_path: Path) -> Path:
    src = tmp_path / "book.md"
    src.write_text(DOC, encoding="utf-8")
    project_dir = tmp_path / "book"
    res = runner.invoke(
        app,
        [
            "init",
            str(project_dir),
            "--source-file",
            str(src),
            "--source-lang",
            "en",
        ],
    )
    assert res.exit_code == 0, res.output
    return project_dir


def test_profile_create_creates_profile_local_layout(tmp_path: Path):
    project_dir = _make_source_project(tmp_path)

    res = runner.invoke(
        app,
        [
            "profile",
            "create",
            str(project_dir),
            "de_gpt5_5",
            "--target",
            "de",
            "--target-locale",
            "de-DE",
            "--model",
            "codex-openai/gpt-5.5@low",
            "--select",
        ],
    )

    assert res.exit_code == 0, res.output
    profile_dir = project_dir / "translations" / "de_gpt5_5"
    assert (profile_dir / "config.toml").is_file()
    assert (profile_dir / "identity.json").is_file()
    assert (profile_dir / "tasks").is_dir()
    assert (profile_dir / "ingest").is_dir()
    assert (profile_dir / "translated").is_dir()
    assert (profile_dir / "reports").is_dir()
    assert (profile_dir / "output").is_dir()
    assert load_project(project_dir).profile == "de_gpt5_5"


def test_profile_list_marks_active_profile(tmp_path: Path):
    project_dir = _make_source_project(tmp_path)
    runner.invoke(
        app,
        [
            "profile",
            "create",
            str(project_dir),
            "de_gpt5_5",
            "--target",
            "de",
            "--select",
        ],
    )
    runner.invoke(
        app,
        ["profile", "create", str(project_dir), "fr_gpt5_5", "--target", "fr"],
    )

    res = runner.invoke(app, ["profile", "list", str(project_dir)])

    assert res.exit_code == 0, res.output
    assert "* de_gpt5_5" in res.output
    assert "fr_gpt5_5" in res.output


def test_profile_select_persists_active_profile(tmp_path: Path):
    project_dir = _make_source_project(tmp_path)
    runner.invoke(
        app,
        ["profile", "create", str(project_dir), "de_gpt5_5", "--target", "de"],
    )
    runner.invoke(
        app,
        ["profile", "create", str(project_dir), "fr_gpt5_5", "--target", "fr"],
    )

    res = runner.invoke(app, ["profile", "select", str(project_dir), "fr_gpt5_5"])

    assert res.exit_code == 0, res.output
    assert res.output.strip() == "fr_gpt5_5"
    assert load_project(project_dir).profile == "fr_gpt5_5"


def test_profile_create_rejects_invalid_name(tmp_path: Path):
    project_dir = _make_source_project(tmp_path)

    res = runner.invoke(
        app,
        ["profile", "create", str(project_dir), "../de", "--target", "de"],
    )

    assert res.exit_code != 0
    assert "invalid translation profile name" in res.output


def test_profile_create_rejects_duplicate_profile(tmp_path: Path):
    project_dir = _make_source_project(tmp_path)
    first = runner.invoke(
        app,
        ["profile", "create", str(project_dir), "de_gpt5_5", "--target", "de"],
    )
    second = runner.invoke(
        app,
        ["profile", "create", str(project_dir), "de_gpt5_5", "--target", "de"],
    )

    assert first.exit_code == 0, first.output
    assert second.exit_code != 0
    assert "translation profile already exists" in second.output


def test_profile_create_rejects_output_filename_mismatch(tmp_path: Path):
    project_dir = _make_source_project(tmp_path)

    res = runner.invoke(
        app,
        [
            "profile",
            "create",
            str(project_dir),
            "de_gpt5_5",
            "--target",
            "de",
            "--output-filename",
            "book.fr.md",
        ],
    )

    assert res.exit_code != 0
    assert "output filename book.fr.md does not match target language de" in res.output


def test_status_without_selected_profile_lists_overview(tmp_path: Path):
    project_dir = _make_source_project(tmp_path)
    runner.invoke(
        app,
        ["profile", "create", str(project_dir), "de_gpt5_5", "--target", "de"],
    )
    runner.invoke(
        app,
        ["profile", "create", str(project_dir), "fr_gpt5_5", "--target", "fr"],
    )

    res = runner.invoke(app, ["status", str(project_dir)])

    assert res.exit_code == 0, res.output
    assert "profiles:" in res.output
    assert "de_gpt5_5" in res.output
    assert "fr_gpt5_5" in res.output


def test_status_with_profile_shows_selected_profile_detail(tmp_path: Path):
    project_dir = _make_source_project(tmp_path)
    runner.invoke(
        app,
        [
            "profile",
            "create",
            str(project_dir),
            "de_gpt5_5",
            "--target",
            "de",
            "--select",
        ],
    )
    assert runner.invoke(app, ["extract", str(project_dir)]).exit_code == 0

    res = runner.invoke(app, ["status", str(project_dir), "--profile", "de_gpt5_5"])

    assert res.exit_code == 0, res.output
    assert "Target language: de" in res.output


def test_profile_show_json_reports_profile_metadata(tmp_path: Path):
    project_dir = _make_source_project(tmp_path)
    runner.invoke(
        app,
        [
            "profile",
            "create",
            str(project_dir),
            "de_gpt5_5",
            "--target",
            "de",
            "--target-locale",
            "de-DE",
            "--model",
            "codex-openai/gpt-5.5@low",
            "--select",
        ],
    )

    res = runner.invoke(
        app, ["profile", "show", str(project_dir), "de_gpt5_5", "--json"]
    )

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["profile"] == "de_gpt5_5"
    assert payload["target_language"] == "de"
    assert payload["target_locale"] == "de-DE"
    assert payload["model"] == "codex-openai/gpt-5.5@low"


def test_profile_compare_reads_profile_local_store_targets(tmp_path: Path):
    project_dir = _make_source_project(tmp_path)
    runner.invoke(
        app,
        ["profile", "create", str(project_dir), "de_gpt5_5", "--target", "de"],
    )
    runner.invoke(
        app,
        ["profile", "create", str(project_dir), "de_glm_5_2", "--target", "de"],
    )
    assert runner.invoke(app, ["extract", str(project_dir)]).exit_code == 0
    chunk = json.loads(
        next((project_dir / ".booktx" / "chunks").glob("*.json")).read_text("utf-8")
    )
    record = chunk["records"][0]
    for profile_name, target in (
        ("de_gpt5_5", "A"),
        ("de_glm_5_2", "B"),
    ):
        proj = load_profile_project(project_dir, profile_name)
        translation_store_path(proj).write_text(
            json.dumps(
                {
                    "version": 2,
                    "records": {
                        record["id"]: {
                            "chunk_id": int(record["id"].split("-", 1)[0]),
                            "part_id": 1,
                            "source_sha256": "abc123",
                            "source": record["source"],
                            "active_version": "1.1",
                            "versions": [
                                {
                                    "version": 1,
                                    "subversion": 1,
                                    "version_ref": "1.1",
                                    "target": target,
                                    "status": "accepted",
                                    "created_at": "2026-06-22T12:00:00Z",
                                    "updated_at": "2026-06-22T12:00:00Z",
                                }
                            ],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

    res = runner.invoke(
        app,
        [
            "profile",
            "compare",
            str(project_dir),
            "--profiles",
            "de_gpt5_5,de_glm_5_2",
            "--record",
            record["id"],
        ],
    )

    assert res.exit_code == 0, res.output
    assert "de_gpt5_5" in res.output
    assert "de_glm_5_2" in res.output
    assert "A" in res.output
    assert "B" in res.output


def test_profile_show_uses_identity_json_after_model_set(tmp_path: Path):
    project_dir = _make_source_project(tmp_path)
    runner.invoke(
        app,
        [
            "profile",
            "create",
            str(project_dir),
            "de_gpt5_5",
            "--target",
            "de",
            "--model",
            "human",
            "--select",
        ],
    )

    set_res = runner.invoke(
        app,
        [
            "model",
            "set",
            str(project_dir),
            "codex-openai/gpt-5.5@low",
            "--profile",
            "de_gpt5_5",
        ],
    )
    assert set_res.exit_code == 0, set_res.output

    show = runner.invoke(
        app, ["profile", "show", str(project_dir), "de_gpt5_5", "--json"]
    )
    assert show.exit_code == 0, show.output
    payload = json.loads(show.output)
    assert payload["model"] == "codex-openai/gpt-5.5@low"
    assert payload["kind"] == "translation"
    assert payload["actor"] == "user:unknown"

    who = runner.invoke(
        app, ["whoami", str(project_dir), "--profile", "de_gpt5_5", "--json"]
    )
    assert who.exit_code == 0, who.output
    assert json.loads(who.output)["model"] == "codex-openai/gpt-5.5@low"


def test_profile_list_uses_identity_json_after_model_set(tmp_path: Path):
    project_dir = _make_source_project(tmp_path)
    runner.invoke(
        app,
        [
            "profile",
            "create",
            str(project_dir),
            "de_gpt5_5",
            "--target",
            "de",
            "--model",
            "human",
            "--select",
        ],
    )
    runner.invoke(
        app,
        [
            "model",
            "set",
            str(project_dir),
            "glm-5.2",
            "--profile",
            "de_gpt5_5",
        ],
    )

    res = runner.invoke(app, ["profile", "list", str(project_dir), "--json"])
    assert res.exit_code == 0, res.output
    overview = json.loads(res.output)
    assert overview["profiles"]
    item = next(p for p in overview["profiles"] if p["profile"] == "de_gpt5_5")
    assert item["model"] == "glm-5.2"
    assert item["kind"] == "translation"


def test_profile_create_still_initializes_all_standard_dirs(tmp_path: Path):
    project_dir = _make_source_project(tmp_path)
    res = runner.invoke(
        app,
        [
            "profile",
            "create",
            str(project_dir),
            "de_gpt5_5",
            "--target",
            "de",
            "--select",
        ],
    )
    assert res.exit_code == 0, res.output
    profile_dir = project_dir / "translations" / "de_gpt5_5"
    for name in ("tasks", "ingest", "translated", "reports", "output"):
        assert (profile_dir / name).is_dir(), f"missing profile dir: {name}"
