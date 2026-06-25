"""Direct unit tests for booktx.review_tasks selection and artifact rendering."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from booktx.cli import app
from booktx.config import (
    load_project,
    translation_review_ingest_block_path,
    translation_review_source_block_path,
    translation_review_task_path,
    write_translation_store,
    write_translation_version_ledger,
)
from booktx.models import (
    QualityReviewConfig,
    ReviewPassConfig,
    StoredTranslationRecordV2,
    TranslationCandidate,
    TranslationReviewCandidate,
    TranslationStoreV2,
    TranslationSubversionLedgerEntry,
    TranslationTrackLedgerEntry,
    TranslationVersionLedger,
)
from booktx.progress import source_record_sha256
from booktx.review_tasks import (
    create_review_task,
    select_review_records,
)
from booktx.status import build_status_snapshot
from booktx.translation_store import sha256_text

runner = CliRunner()

DOC = """\
# Chapter One

Alice met Bob. They were happy.

Bob left. Alice stayed.
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
            "2",
        ],
    )
    assert res.exit_code == 0, res.output
    ext = runner.invoke(app, ["extract", str(project_dir)])
    assert ext.exit_code == 0, ext.output
    return project_dir


def _write_ledger(proj) -> None:
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


def _store_record(rid: str, source: str, *, reviews=None, active_review=None):
    cid, pid = (int(x) for x in rid.split("-"))
    return StoredTranslationRecordV2(
        chunk_id=cid,
        part_id=pid,
        source_sha256=source_record_sha256(source),
        source=source,
        active_version="1.1",
        active_review=active_review,
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
        reviews=reviews or [],
    )


def _pass_cfg():
    return QualityReviewConfig(
        enabled=True,
        active_passes=[1],
        passes=[ReviewPassConfig(pass_number=1, enforce="warn")],
    )


def test_select_pass1_picks_records_without_review(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    proj = load_project(project_dir)
    records = {}
    for path in sorted(proj.chunks_dir.glob("*.json")):
        chunk = json.loads(path.read_text("utf-8"))
        for rec in chunk["records"]:
            records[rec["id"]] = _store_record(rec["id"], rec["source"])
    write_translation_store(proj, TranslationStoreV2(records=records))
    _write_ledger(proj)
    proj = load_project(project_dir)
    bundle = build_status_snapshot(proj, context_exists=False, context_ready=False)
    cfg = _pass_cfg()
    selected = select_review_records(bundle, records, cfg, pass_number=1)
    assert len(selected) == len(records)
    assert all(s.review_ref == "R1.1" for s in selected)
    assert all(s.base_kind == "translation" for s in selected)
    assert all(s.base_ref == "1.1" for s in selected)


def test_select_skips_records_with_current_pass1_review(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    proj = load_project(project_dir)
    records = {}
    first_id = None
    for path in sorted(proj.chunks_dir.glob("*.json")):
        chunk = json.loads(path.read_text("utf-8"))
        for rec in chunk["records"]:
            if first_id is None:
                first_id = rec["id"]
                review = TranslationReviewCandidate(
                    pass_number=1,
                    run_number=1,
                    review_ref="R1.1",
                    base_kind="translation",
                    base_ref="1.1",
                    base_target_sha256=sha256_text(rec["source"]),
                    target="polished",
                    target_sha256=sha256_text("polished"),
                    created_at="t",
                    updated_at="t",
                )
                records[rec["id"]] = _store_record(
                    rec["id"], rec["source"], reviews=[review], active_review="R1.1"
                )
            else:
                records[rec["id"]] = _store_record(rec["id"], rec["source"])
    write_translation_store(proj, TranslationStoreV2(records=records))
    _write_ledger(proj)
    proj = load_project(project_dir)
    bundle = build_status_snapshot(proj, context_exists=False, context_ready=False)
    selected = select_review_records(bundle, records, _pass_cfg(), pass_number=1)
    selected_ids = {s.record_id for s in selected}
    assert first_id not in selected_ids
    assert len(selected) == len(records) - 1


def test_select_pass2_blocked_when_pass1_missing(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    proj = load_project(project_dir)
    records = {}
    for path in sorted(proj.chunks_dir.glob("*.json")):
        chunk = json.loads(path.read_text("utf-8"))
        for rec in chunk["records"]:
            records[rec["id"]] = _store_record(rec["id"], rec["source"])
    write_translation_store(proj, TranslationStoreV2(records=records))
    _write_ledger(proj)
    proj = load_project(project_dir)
    bundle = build_status_snapshot(proj, context_exists=False, context_ready=False)
    cfg = QualityReviewConfig(
        enabled=True,
        active_passes=[1, 2],
        passes=[
            ReviewPassConfig(pass_number=1, enforce="warn"),
            ReviewPassConfig(
                pass_number=2,
                enforce="warn",
                base="active_review",
                required_base_pass=1,
            ),
        ],
    )
    selected = select_review_records(bundle, records, cfg, pass_number=2)
    # No record has a pass-1 review, so pass 2 has no eligible base.
    assert selected == []


def test_create_review_task_writes_artifacts_prefilled_with_base(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    proj = load_project(project_dir)
    records = {}
    for path in sorted(proj.chunks_dir.glob("*.json")):
        chunk = json.loads(path.read_text("utf-8"))
        for rec in chunk["records"]:
            records[rec["id"]] = _store_record(rec["id"], rec["source"])
    write_translation_store(proj, TranslationStoreV2(records=records))
    _write_ledger(proj)
    proj = load_project(project_dir)
    bundle = build_status_snapshot(proj, context_exists=False, context_ready=False)
    cfg = _pass_cfg()
    selected = select_review_records(bundle, records, cfg, pass_number=1)
    chapter = next(iter(bundle.index.chapters_by_id.values()))
    task = create_review_task(
        proj, bundle, cfg, selected, pass_number=1, chapter=chapter
    )
    # Task json and block files exist.
    assert translation_review_task_path(proj, task.review_task_id).is_file()
    src_block = translation_review_source_block_path(proj, task.review_task_id)
    ingest_block = translation_review_ingest_block_path(proj, task.review_task_id)
    assert src_block.is_file() and ingest_block.is_file()
    ingest_text = ingest_block.read_text("utf-8")
    # Ingest block is prefilled with the base target under each record header.
    for sel in selected:
        assert f">>> {sel.record_id}" in ingest_text
        assert sel.base_target in ingest_text
    # Source block references the review ref and base ref.
    src_text = src_block.read_text("utf-8")
    assert f"REVIEW {selected[0].review_ref} FROM {selected[0].base_ref}" in src_text


def test_select_skips_source_drift_records(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    proj = load_project(project_dir)
    records = {}
    drifted_id = None
    for path in sorted(proj.chunks_dir.glob("*.json")):
        chunk = json.loads(path.read_text("utf-8"))
        for rec in chunk["records"]:
            if drifted_id is None:
                drifted_id = rec["id"]
                # Store a stale source that no longer matches the extracted source.
                records[rec["id"]] = _store_record(rec["id"], "different source text")
            else:
                records[rec["id"]] = _store_record(rec["id"], rec["source"])
    write_translation_store(proj, TranslationStoreV2(records=records))
    _write_ledger(proj)
    proj = load_project(project_dir)
    bundle = build_status_snapshot(proj, context_exists=False, context_ready=False)
    selected = select_review_records(bundle, records, _pass_cfg(), pass_number=1)
    selected_ids = {s.record_id for s in selected}
    assert drifted_id not in selected_ids
