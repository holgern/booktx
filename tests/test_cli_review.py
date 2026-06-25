"""CLI integration tests for the ``booktx review`` command group."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from booktx.cli import app
from booktx.config import (
    load_project,
    write_profile_config,
    write_translation_store,
    write_translation_version_ledger,
)
from booktx.models import (
    QualityReviewConfig,
    ReviewPassConfig,
    StoredTranslationRecordV2,
    TranslationCandidate,
    TranslationStoreV2,
    TranslationSubversionLedgerEntry,
    TranslationTrackLedgerEntry,
    TranslationVersionLedger,
)
from booktx.progress import source_record_sha256

runner = CliRunner()


def _make_project(
    tmp_path: Path, *, source: str = "# Chapter One\n\nAlice ran fast.\n"
) -> Path:
    src = tmp_path / "book.md"
    src.write_text(source, encoding="utf-8")
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
        ],
    )
    assert res.exit_code == 0, res.output
    ext = runner.invoke(app, ["extract", str(project_dir)])
    assert ext.exit_code == 0, ext.output
    return project_dir


def _enable_quality_review(proj, *, active_passes=(1,), enforce="warn"):
    cfg = proj.profile_config.model_copy(
        update={
            "quality_review": QualityReviewConfig(
                enabled=True,
                active_passes=list(active_passes),
                passes=[
                    ReviewPassConfig(pass_number=p, enforce=enforce)
                    for p in active_passes
                ],
            )
        }
    )
    write_profile_config(proj, cfg)
    return load_project(proj.root)


def _setup_store(tmp_path: Path) -> Path:
    project_dir = _make_project(tmp_path)
    proj = load_project(project_dir)
    # Write a v2 identity store with one record.
    chunk = json.loads(sorted(proj.chunks_dir.glob("*.json"))[0].read_text("utf-8"))
    rec = chunk["records"][0]
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
                        target=rec["source"],
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
    return project_dir


def test_review_status_disabled_when_no_config(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    result = runner.invoke(app, ["review", "status", str(project_dir)])
    assert result.exit_code == 0
    assert "disabled" in result.output


def test_review_status_reports_coverage(tmp_path: Path):
    project_dir = _setup_store(tmp_path)
    proj = load_project(project_dir)
    _enable_quality_review(proj)
    result = runner.invoke(app, ["review", "status", str(project_dir)])
    assert result.exit_code == 0
    assert "eligible base records: 1" in result.output
    assert "missing review: 1" in result.output


def test_review_next_creates_task(tmp_path: Path):
    project_dir = _setup_store(tmp_path)
    proj = load_project(project_dir)
    _enable_quality_review(proj)
    result = runner.invoke(app, ["review", "next", str(project_dir), "--pass", "1"])
    assert result.exit_code == 0
    assert "review task: btr-" in result.output
    assert "insert" in result.output


def test_review_insert_accepts_unchanged_target(tmp_path: Path):
    project_dir = _setup_store(tmp_path)
    proj = load_project(project_dir)
    _enable_quality_review(proj)
    # Create a review task.
    next_result = runner.invoke(
        app, ["review", "next", str(project_dir), "--pass", "1"]
    )
    assert next_result.exit_code == 0, next_result.output
    # Extract the review_task_id from output.
    task_id_line = [
        line
        for line in next_result.output.splitlines()
        if line.startswith("review task: btr-")
    ][0]
    review_task_id = task_id_line.split(": ")[1].strip()
    # Build a block submission file with unchanged targets.
    proj2 = load_project(project_dir)
    from booktx.config import load_translation_review_task

    task = load_translation_review_task(proj2, review_task_id)
    assert task is not None
    import tempfile

    block_file = Path(tempfile.mktemp(suffix=".block.txt"))
    lines = ["# review block submission", f"# review_task: {review_task_id}", ""]
    for rec in task.records:
        lines.append(f">>> {rec.id}")
        lines.append(rec.base_target)
        lines.append("")
    block_file.write_text("\n".join(lines), encoding="utf-8")
    insert_result = runner.invoke(
        app,
        [
            "review",
            "insert",
            str(project_dir),
            "--review-task-id",
            review_task_id,
            "--file",
            str(block_file),
        ],
    )
    assert insert_result.exit_code == 0, insert_result.output
    assert "accepted" in insert_result.output


def test_review_activate_sets_active_review(tmp_path: Path):
    project_dir = _setup_store(tmp_path)
    proj = load_project(project_dir)
    _enable_quality_review(proj)
    # Create a review task and insert.
    next_result = runner.invoke(
        app, ["review", "next", str(project_dir), "--pass", "1"]
    )
    task_id_line = [
        line
        for line in next_result.output.splitlines()
        if line.startswith("review task: btr-")
    ][0]
    review_task_id = task_id_line.split(": ")[1].strip()
    proj2 = load_project(project_dir)
    from booktx.config import load_translation_review_task

    task = load_translation_review_task(proj2, review_task_id)
    assert task is not None
    import tempfile

    block_file = Path(tempfile.mktemp(suffix=".block.txt"))
    lines = [f"# review_task: {review_task_id}", ""]
    for rec in task.records:
        lines.append(f">>> {rec.id}")
        lines.append(rec.base_target)
        lines.append("")
    block_file.write_text("\n".join(lines), encoding="utf-8")
    insert_result = runner.invoke(
        app,
        [
            "review",
            "insert",
            str(project_dir),
            "--review-task-id",
            review_task_id,
            "--file",
            str(block_file),
        ],
    )
    assert insert_result.exit_code == 0
    # The candidate was activated by default.
    from booktx.config import load_translation_store

    store = load_translation_store(load_project(project_dir))
    for rec in task.records:
        stored = store.records[rec.id]
        assert stored.active_review is not None
