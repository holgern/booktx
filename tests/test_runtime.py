"""Runtime resolution tests for project-root and profile-root execution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from booktx.cli import app
from booktx.config import BooktxError
from booktx.runtime import resolve_runtime

runner = CliRunner()

DOC = """\
# Demo

Alice met Bob. They were happy.
"""


def _make_project(tmp_path: Path) -> Path:
    src = tmp_path / "book.md"
    src.write_text(DOC, encoding="utf-8")
    project_dir = tmp_path / "book"
    init_res = runner.invoke(
        app,
        ["init", str(project_dir), "--source-file", str(src), "--source-lang", "en"],
    )
    assert init_res.exit_code == 0, init_res.output
    create_res = runner.invoke(
        app,
        [
            "profile",
            "create",
            str(project_dir),
            "de_default",
            "--target",
            "de",
            "--target-locale",
            "de-DE",
            "--select",
        ],
    )
    assert create_res.exit_code == 0, create_res.output
    return project_dir


def test_profile_create_writes_profile_root_marker(tmp_path: Path):
    project_dir = _make_project(tmp_path)

    marker_path = project_dir / "translations" / "de_default" / ".booktx-profile.json"

    assert marker_path.is_file()
    marker = json.loads(marker_path.read_text("utf-8"))
    assert marker["schema"] == "booktx.profile-root.v1"
    assert marker["profile"] == "de_default"
    assert marker["target_language"] == "de"
    assert marker["target_locale"] == "de-DE"
    assert marker["source_id"].startswith("sha256:")


def test_resolve_runtime_project_root_mode(tmp_path: Path):
    project_dir = _make_project(tmp_path)

    runtime = resolve_runtime(project_dir)

    assert runtime.mode.kind == "project-root"
    assert runtime.mode.isolated_output is False
    assert runtime.mode.profile_root is None
    assert runtime.project.root == project_dir


def test_resolve_runtime_profile_root_mode(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    profile_root = project_dir / "translations" / "de_default"

    runtime = resolve_runtime(profile_root)

    assert runtime.mode.kind == "profile-root"
    assert runtime.mode.isolated_output is True
    assert runtime.mode.profile_root == profile_root
    assert runtime.mode.profile_name == "de_default"
    assert runtime.project.root == project_dir
    assert runtime.project.profile == "de_default"


def test_resolve_runtime_dot_from_profile_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project_dir = _make_project(tmp_path)
    profile_root = project_dir / "translations" / "de_default"
    monkeypatch.chdir(profile_root)

    runtime = resolve_runtime(Path("."))

    assert runtime.mode.kind == "profile-root"
    assert runtime.mode.profile_root == profile_root


def test_resolve_runtime_rejects_other_profile_in_profile_root_mode(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    create_res = runner.invoke(
        app,
        ["profile", "create", str(project_dir), "fr_default", "--target", "fr"],
    )
    assert create_res.exit_code == 0, create_res.output

    profile_root = project_dir / "translations" / "de_default"

    with pytest.raises(BooktxError, match="cannot target a different profile"):
        resolve_runtime(profile_root, profile="fr_default")


def test_resolve_runtime_accepts_matching_profile_in_profile_root_mode(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    profile_root = project_dir / "translations" / "de_default"

    runtime = resolve_runtime(profile_root, profile="de_default")

    assert runtime.mode.kind == "profile-root"
    assert runtime.project.profile == "de_default"


def test_resolve_runtime_rejects_stale_marker_source_id(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    marker_path = project_dir / "translations" / "de_default" / ".booktx-profile.json"
    marker = json.loads(marker_path.read_text("utf-8"))
    marker["source_id"] = "sha256:" + ("0" * 64)
    marker_path.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(BooktxError, match="source id is stale"):
        resolve_runtime(project_dir / "translations" / "de_default")


def test_resolve_runtime_rejects_marker_profile_mismatch(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    marker_path = project_dir / "translations" / "de_default" / ".booktx-profile.json"
    marker = json.loads(marker_path.read_text("utf-8"))
    marker["profile"] = "other_profile"
    marker_path.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(
        BooktxError, match="does not match the enclosing profile directory"
    ):
        resolve_runtime(project_dir / "translations" / "de_default")
