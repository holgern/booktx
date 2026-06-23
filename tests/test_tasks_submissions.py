"""Direct unit tests for booktx.tasks and booktx.submissions."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from booktx.cli import app
from booktx.config import load_project
from booktx.submissions import (
    ParsedSubmission,
    parse_block_submission,
    parse_json_submission,
    parse_tsv_submission,
    read_submission_file,
    resolve_submission,
)
from booktx.tasks import TaskPaths, make_task_id, project_relative, task_paths

runner = CliRunner()

DOC = """\
# Chapter One

Alice met Bob. They were happy.
"""


def _make_project(tmp_path: Path) -> Path:
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
            "5",
        ],
    )
    assert res.exit_code == 0, res.output
    assert runner.invoke(app, ["extract", str(project_dir)]).exit_code == 0
    return project_dir


def test_task_paths_bundles_four_durable_files(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    proj = load_project(project_dir)
    paths = task_paths(proj, "bt-task-x")
    assert isinstance(paths, TaskPaths)
    assert paths.task_json.name == "bt-task-x.json"
    assert paths.source_block.name == "bt-task-x.source.block.txt"
    assert paths.ingest_json.name == "bt-task-x.json"
    assert paths.ingest_block.name == "bt-task-x.block.txt"
    display = paths.display(proj.root)
    assert display.source_block.startswith("translations/de_default/tasks")
    hint = paths.block_submit_hint("bt-task-x", proj.root)
    assert hint.startswith("booktx translate insert")
    assert "--profile de_default" in hint
    assert "--json-file" in paths.json_submit_hint("bt-task-x", proj.root)


def test_project_relative_falls_back_to_absolute(tmp_path: Path):
    a = tmp_path / "a.txt"
    assert project_relative(a, tmp_path / "other") == str(a)


def test_parse_json_submission_returns_submitted_records():
    payload = {
        "task_id": "t1",
        "records": [
            {"id": "r1", "target": "x"},
            {"id": "r2", "target": "y"},
        ],
    }
    text = json.dumps(payload)
    parsed = parse_json_submission(text)
    assert isinstance(parsed, ParsedSubmission)
    assert parsed.task_id == "t1"
    assert [r.id for r in parsed.records] == ["r1", "r2"]
    assert parsed.records[1].target == "y"


def test_parse_tsv_submission_skips_blanks():
    parsed = parse_tsv_submission("r1\tx\n\nr2\ty\n")
    assert [r.id for r in parsed.records] == ["r1", "r2"]


def test_parse_block_submission_preserves_internal_comments():
    # A comment line flanked by target text is internal and preserved;
    # only trailing separator comments/blank lines before the next header
    # (or EOF) are stripped.
    text = ">>> r1\nfirst\n# keep me\nmore\n\n>>> r2\nsecond\n"
    parsed = parse_block_submission(text)
    assert [r.id for r in parsed.records] == ["r1", "r2"]
    assert parsed.records[0].target == "first\n# keep me\nmore"
    assert parsed.records[1].target == "second"


def test_read_submission_file_missing_raises_booktx_error(tmp_path: Path):
    from booktx.config import BooktxError

    try:
        read_submission_file(tmp_path / "nope.json")
    except BooktxError as exc:
        assert "not found" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected BooktxError")


def test_resolve_submission_record_pair():
    parsed = resolve_submission(
        record_id="r1",
        target="hello",
        input_format="json",
        stdin=False,
        json_file=None,
        input_file=None,
    )
    assert parsed.records[0].id == "r1"
    assert parsed.records[0].target == "hello"


def test_resolve_submission_no_input_raises(tmp_path: Path):
    from booktx.config import BooktxError

    try:
        resolve_submission(
            record_id=None,
            target=None,
            input_format="json",
            stdin=False,
            json_file=None,
            input_file=None,
        )
    except BooktxError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected BooktxError for missing input")


def test_make_task_id_idempotent_for_same_ids():
    a = make_task_id("ch01", "c0001-r0001", ["c0001-r0001", "c0001-r0002"])
    b = make_task_id("ch01", "c0001-r0001", ["c0001-r0001", "c0001-r0002"])
    # Timestamps may differ by second; the digest suffix must be identical.
    assert a.rsplit("-", 1)[0].startswith("bt-task-")
    assert a.rsplit("-", 1)[1] == b.rsplit("-", 1)[1]
