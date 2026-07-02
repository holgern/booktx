"""CLI and workflow coverage for static source analysis."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from booktx.cli import app
from booktx.config import (
    load_source_project,
    source_analysis_markdown_path,
    source_analysis_path,
)
from booktx.errors import BooktxError
from booktx.workflows.source import analyze_source

runner = CliRunner()


def _project(tmp_path: Path) -> Path:
    source = tmp_path / "novel.md"
    source.write_text(
        "# One\n\nTisamon met Tisamon. wasp-kinden wasp-kinden.\n",
        encoding="utf-8",
    )
    root = tmp_path / "novel"
    for args in (
        ["init", str(root), "--target", "de", "--source-file", str(source)],
        ["extract", str(root)],
        ["chapters", str(root)],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
    return root


def test_analyze_dry_run_does_not_write(tmp_path: Path) -> None:
    root = _project(tmp_path)
    result = runner.invoke(app, ["source", "analyze", str(root), "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["analysis_sha256"]
    project = load_source_project(root)
    assert not source_analysis_path(project).exists()
    assert not source_analysis_markdown_path(project).exists()


def test_write_sync_and_isolated_read(tmp_path: Path) -> None:
    root = _project(tmp_path)
    result = runner.invoke(
        app,
        ["source", "analyze", str(root), "--write", "--sync-profiles"],
    )
    assert result.exit_code == 0, result.output
    canonical = json.loads((root / ".booktx" / "source-analysis.json").read_text())
    snapshot_path = root / "translations" / "de_default" / "source-analysis.json"
    snapshot = json.loads(snapshot_path.read_text())
    assert snapshot["analysis_sha256"] == canonical["analysis_sha256"]
    assert snapshot["report"] == canonical

    isolated = runner.invoke(
        app,
        [
            "source",
            "analysis",
            str(root / "translations" / "de_default"),
            "--format",
            "json",
        ],
    )
    assert isolated.exit_code == 0, isolated.output
    assert (
        json.loads(isolated.output)["analysis_sha256"] == canonical["analysis_sha256"]
    )
    assert str(root) not in isolated.output
    assert "../" not in isolated.output


def test_sync_profiles_requires_write(tmp_path: Path) -> None:
    root = _project(tmp_path)
    result = runner.invoke(app, ["source", "analyze", str(root), "--sync-profiles"])
    assert result.exit_code != 0
    assert "requires --write" in result.output


def test_profile_analysis_missing_snapshot_hint_mentions_sync_profiles(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    written = runner.invoke(app, ["source", "analyze", str(root), "--write"])
    assert written.exit_code == 0, written.output
    snapshot_path = root / "translations" / "de_default" / "source-analysis.json"
    if snapshot_path.exists():
        snapshot_path.unlink()
    result = runner.invoke(
        app,
        ["source", "analysis", str(root / "translations" / "de_default")],
    )
    assert result.exit_code != 0
    assert "--sync-profiles" in result.output


def test_profile_creation_copies_current_analysis(tmp_path: Path) -> None:
    root = _project(tmp_path)
    written = runner.invoke(app, ["source", "analyze", str(root), "--write"])
    assert written.exit_code == 0, written.output
    created = runner.invoke(
        app,
        ["profile", "create", str(root), "fr_reader", "--target", "fr"],
    )
    assert created.exit_code == 0, created.output
    canonical = json.loads((root / ".booktx" / "source-analysis.json").read_text())
    snapshot = json.loads(
        (root / "translations" / "fr_reader" / "source-analysis.json").read_text()
    )
    assert snapshot["schema"] == "booktx.source-analysis-snapshot.v1"
    assert snapshot["profile"] == "fr_reader"
    assert snapshot["analysis_sha256"] == canonical["analysis_sha256"]
    assert (root / "translations" / "fr_reader" / "source-analysis.md").is_file()


def test_sync_reports_partial_markdown_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    project = load_source_project(root)
    from booktx import io_utils

    real_write = io_utils.write_text_atomic

    def fail_profile_markdown(path: Path, text: str) -> None:
        if path.name == "source-analysis.md" and "translations" in path.parts:
            raise OSError("simulated profile markdown failure")
        real_write(path, text)

    monkeypatch.setattr(io_utils, "write_text_atomic", fail_profile_markdown)
    result = analyze_source(project, write=True, sync_profiles=True)
    assert result.canonical_json_written
    assert result.canonical_md_written
    assert len(result.failed_syncs) == 1
    failed = result.failed_syncs[0]
    assert failed.json_written
    assert not failed.md_written
    assert "simulated profile markdown failure" in (failed.error or "")


def test_bad_engine_and_profile_root_analyze_are_controlled(tmp_path: Path) -> None:
    root = _project(tmp_path)
    bad = runner.invoke(app, ["source", "analyze", str(root), "--engine", "unknown"])
    assert bad.exit_code != 0
    isolated = runner.invoke(
        app,
        ["source", "analyze", str(root / "translations" / "de_default")],
    )
    assert isolated.exit_code != 0
    assert "project-root command" in isolated.output


def test_workflow_rejects_invalid_settings(tmp_path: Path) -> None:
    project = load_source_project(_project(tmp_path))
    with pytest.raises(BooktxError, match="ngram-max"):
        analyze_source(project, ngram_max=5)
