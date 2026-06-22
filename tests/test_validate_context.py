"""Context terminology validation tests."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from booktx.cli import app
from booktx.config import init_project, load_project
from booktx.context import default_context, write_context, write_context_markdown
from booktx.models import Chunk, Record
from booktx.validate import validate_project

runner = CliRunner()


def _src_chunk() -> Chunk:
    return Chunk(
        chunk_id="0001",
        source_language="en",
        target_language="de",
        records=[
            Record(
                id="0001-000001",
                source="The Wasp Empire has commenced its war against the Lowlands.",
            )
        ],
    )


def _write_project(tmp_path: Path, target: str = "die Niederlande") -> Path:
    proj = init_project(tmp_path / "book", target_language="de")
    proj.chunks_dir.mkdir(parents=True, exist_ok=True)
    proj.translated_dir.mkdir(parents=True, exist_ok=True)
    chunk = _src_chunk()
    (proj.chunks_dir / "0001.json").write_text(
        chunk.model_dump_json(), encoding="utf-8"
    )
    (proj.translated_dir / "0001.json").write_text(
        json.dumps(
            {
                "chunk_id": "0001",
                "records": [{"id": "0001-000001", "target": target}],
            }
        ),
        encoding="utf-8",
    )
    return proj.root


def _write_context(proj_path: Path, enforce: str = "error") -> None:
    proj = load_project(proj_path)
    ctx = default_context(proj)
    for entry in ctx.glossary:
        if entry.source == "Lowlands":
            entry.enforce = enforce  # type: ignore[assignment]
    write_context(proj, ctx)
    write_context_markdown(proj, ctx)


def test_forbidden_term_used_error_fails_report(tmp_path: Path):
    proj_path = _write_project(tmp_path)
    _write_context(proj_path, enforce="error")
    report = validate_project(load_project(proj_path))
    assert not report.passed
    finding = next(f for f in report.findings if f.rule == "forbidden_term_used")
    assert finding.severity == "error"
    assert finding.record_id == "0001-000001"
    assert "Lowlands" in finding.message
    assert "Niederlande" in finding.message


def test_forbidden_term_used_warn_passes_with_warning(tmp_path: Path):
    proj_path = _write_project(tmp_path)
    _write_context(proj_path, enforce="warn")
    report = validate_project(load_project(proj_path))
    assert report.passed
    finding = next(f for f in report.findings if f.rule == "forbidden_term_used")
    assert finding.severity == "warn"


def test_forbidden_term_enforce_off_emits_no_finding(tmp_path: Path):
    proj_path = _write_project(tmp_path)
    _write_context(proj_path, enforce="off")
    report = validate_project(load_project(proj_path))
    assert report.passed
    assert "forbidden_term_used" not in {f.rule for f in report.findings}


def test_missing_context_keeps_existing_validate_behavior(tmp_path: Path):
    proj_path = _write_project(tmp_path)
    report = validate_project(load_project(proj_path))
    assert report.passed
    assert report.findings == []


def test_forbidden_target_only_checked_when_source_term_matches(tmp_path: Path):
    proj_path = _write_project(tmp_path, target="die Niederlande")
    proj = load_project(proj_path)
    chunk = Chunk(
        chunk_id="0001",
        source_language="en",
        target_language="de",
        records=[Record(id="0001-000001", source="A different region.")],
    )
    (proj.chunks_dir / "0001.json").write_text(
        chunk.model_dump_json(), encoding="utf-8"
    )
    _write_context(proj_path, enforce="error")
    report = validate_project(load_project(proj_path))
    assert report.passed
    assert "forbidden_term_used" not in {f.rule for f in report.findings}


def test_validate_cli_exits_nonzero_for_error_enforcement(tmp_path: Path):
    proj_path = _write_project(tmp_path)
    _write_context(proj_path, enforce="error")
    res = runner.invoke(app, ["validate", str(proj_path)])
    assert res.exit_code == 1
    assert "forbidden_term_used" in res.output


def test_validate_cli_passes_with_warning_for_warn_enforcement(tmp_path: Path):
    proj_path = _write_project(tmp_path)
    _write_context(proj_path, enforce="warn")
    res = runner.invoke(app, ["validate", str(proj_path)])
    assert res.exit_code == 0, res.output
    assert "forbidden_term_used" in res.output
    assert "warnings=2" in res.output
