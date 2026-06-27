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
    assert "next: booktx review next" in result.output


def test_review_status_json_is_actionable(tmp_path: Path):
    project_dir = _setup_store(tmp_path)
    proj = load_project(project_dir)
    _enable_quality_review(proj)
    result = runner.invoke(app, ["review", "status", str(project_dir), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["enabled"] is True
    assert payload["next_command"].startswith("booktx review next"), payload[
        "next_command"
    ]
    assert payload["first_missing_record"] is not None
    assert payload["first_missing_chapter"] is not None


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


def _setup_store_with_pass1_review(tmp_path: Path) -> Path:
    """Project whose single record has an accepted, active R1.1 review."""
    from booktx.models import TranslationReviewCandidate
    from booktx.translation_store import sha256_text

    project_dir = _make_project(tmp_path)
    proj = load_project(project_dir)
    chunk = json.loads(sorted(proj.chunks_dir.glob("*.json"))[0].read_text("utf-8"))
    rec = chunk["records"][0]
    source = rec["source"]
    review = TranslationReviewCandidate(
        pass_number=1,
        run_number=1,
        review_ref="R1.1",
        base_kind="translation",
        base_ref="1.1",
        base_target_sha256=sha256_text(source),
        target="polished",
        target_sha256=sha256_text("polished"),
        created_at="2026-06-22T12:00:00Z",
        updated_at="2026-06-22T12:00:00Z",
    )
    store = TranslationStoreV2(
        records={
            rec["id"]: StoredTranslationRecordV2(
                chunk_id=1,
                part_id=1,
                source_sha256=source_record_sha256(source),
                source=source,
                active_version="1.1",
                active_review="R1.1",
                versions=[
                    TranslationCandidate(
                        version=1,
                        subversion=1,
                        version_ref="1.1",
                        target=source,
                        created_at="2026-06-22T12:00:00Z",
                        updated_at="2026-06-22T12:00:00Z",
                    )
                ],
                reviews=[review],
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


def test_review_next_selection_reviewed_creates_r1_2(tmp_path: Path):
    project_dir = _setup_store_with_pass1_review(tmp_path)
    proj = load_project(project_dir)
    _enable_quality_review(proj)
    result = runner.invoke(
        app,
        [
            "review",
            "next",
            str(project_dir),
            "--pass",
            "1",
            "--selection",
            "reviewed",
            "--base",
            "active_review",
        ],
    )
    assert result.exit_code == 0, result.output
    from booktx.config import load_translation_review_task

    task_id_line = [
        line for line in result.output.splitlines() if line.startswith("review task: ")
    ][0]
    review_task_id = task_id_line.split(": ", 1)[1].strip()
    proj2 = load_project(project_dir)
    task = load_translation_review_task(proj2, review_task_id)
    assert task is not None
    # Rerun assigns R1.2 based on the active R1.1 review.
    assert task.records[0].review_ref == "R1.2"
    assert task.records[0].base_kind == "review"
    assert task.records[0].base_ref == "R1.1"


def test_review_next_rejects_invalid_selection(tmp_path: Path):
    project_dir = _setup_store(tmp_path)
    proj = load_project(project_dir)
    _enable_quality_review(proj)
    result = runner.invoke(
        app,
        ["review", "next", str(project_dir), "--pass", "1", "--selection", "bogus"],
    )
    assert result.exit_code != 0
    assert "invalid --selection" in result.output


def test_review_next_rejects_invalid_base(tmp_path: Path):
    project_dir = _setup_store(tmp_path)
    proj = load_project(project_dir)
    _enable_quality_review(proj)
    result = runner.invoke(
        app,
        ["review", "next", str(project_dir), "--pass", "1", "--base", "translation"],
    )
    assert result.exit_code != 0
    assert "invalid --base" in result.output


def test_review_insert_activates_by_default(tmp_path: Path):
    project_dir = _setup_store(tmp_path)
    proj = load_project(project_dir)
    _enable_quality_review(proj)
    next_result = runner.invoke(
        app, ["review", "next", str(project_dir), "--pass", "1"]
    )
    assert next_result.exit_code == 0
    task_id_line = [
        line
        for line in next_result.output.splitlines()
        if line.startswith("review task: btr-")
    ][0]
    review_task_id = task_id_line.split(": ", 1)[1].strip()
    proj2 = load_project(project_dir)
    from booktx.config import load_translation_review_task, load_translation_store

    task = load_translation_review_task(proj2, review_task_id)
    assert task is not None
    import tempfile

    block_file = Path(tempfile.mktemp(suffix=".block.txt"))
    lines = [f"# review_task: {review_task_id}", ""]
    for rec in task.records:
        lines.append(f">>> {rec.id}")
        lines.append(rec.base_target + " improved")
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
    assert "activated" in insert_result.output
    store = load_translation_store(load_project(project_dir))
    for rec in task.records:
        stored = store.records[rec.id]
        assert stored.active_review is not None
        assert stored.active_review == rec.review_ref


def test_review_insert_rejects_dropped_inline_tag_for_epub(tmp_path: Path):
    # ac-0005: review insert runs the shared staged EPUB inline-XHTML
    # preflight and rejects a target that drops a required <strong> tag
    # BEFORE the store is written, matching translation revise-record safety.
    from ebooklib import epub

    from booktx.config import find_source_file, init_project

    proj = init_project(tmp_path / "book", target_language="de")
    book = epub.EpubBook()
    book.set_identifier("test")
    book.set_title("T")
    book.set_language("en")
    ch1 = epub.EpubHtml(title="C1", file_name="ch1.xhtml", lang="en")
    ch1.content = (
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        "<head><title>C1</title></head><body>"
        "<p>Alice met <strong>Bob</strong>.</p>"
        "</body></html>"
    )
    book.add_item(ch1)
    book.spine = ["nav", ch1]
    book.add_item(epub.EpubNav())
    book.add_item(epub.EpubNcx())
    book.toc = (ch1,)
    epub.write_epub(str(proj.source_dir / "book.epub"), book, {})
    find_source_file(proj)
    assert runner.invoke(app, ["extract", str(proj.root)]).exit_code == 0
    proj = load_project(proj.root)
    chunk = json.loads(sorted(proj.chunks_dir.glob("*.json"))[0].read_text("utf-8"))
    rec = chunk["records"][0]
    rid = rec["id"]
    cid, pid = (int(x) for x in rid.split("-"))
    source = rec["source"]
    # Active translation preserves the inline tag (the review base).
    store = TranslationStoreV2(
        records={
            rid: StoredTranslationRecordV2(
                chunk_id=cid,
                part_id=pid,
                source_sha256=source_record_sha256(source),
                source=source,
                active_version="1.1",
                versions=[
                    TranslationCandidate(
                        version=1,
                        subversion=1,
                        version_ref="1.1",
                        target=source,
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
    proj = load_project(proj.root)
    _enable_quality_review(proj)
    next_result = runner.invoke(
        app,
        [
            "review",
            "next",
            str(proj.root),
            "--pass",
            "1",
            "--chapter",
            rid.split("-")[0],
        ],
    )
    assert next_result.exit_code == 0, next_result.output
    task_id_line = [
        line
        for line in next_result.output.splitlines()
        if line.startswith("review task: btr-")
    ][0]
    review_task_id = task_id_line.split(": ", 1)[1].strip()
    from booktx.config import load_translation_review_task, load_translation_store

    task = load_translation_review_task(load_project(proj.root), review_task_id)
    assert task is not None
    import tempfile

    block_file = Path(tempfile.mktemp(suffix=".block.txt"))
    lines = [f"# review_task: {review_task_id}", ""]
    for r in task.records:
        lines.append(f">>> {r.id}")
        # Drop the <strong> tag from the review target.
        lines.append(r.base_target.replace("<strong>", "").replace("</strong>", ""))
        lines.append("")
    block_file.write_text("\n".join(lines), encoding="utf-8")
    insert_result = runner.invoke(
        app,
        [
            "review",
            "insert",
            str(proj.root),
            "--review-task-id",
            review_task_id,
            "--file",
            str(block_file),
        ],
    )
    # Pre-write enforcement: review insert rejects before writing the store.
    assert insert_result.exit_code != 0
    assert "accepted" not in insert_result.output
    store2 = load_translation_store(load_project(proj.root))
    assert all(len(st.reviews) == 0 for st in store2.records.values())
