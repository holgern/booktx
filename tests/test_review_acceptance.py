"""Direct unit tests for booktx.review_acceptance atomic insert flow."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from booktx.cli import app
from booktx.config import (
    load_project,
    load_translation_store,
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
from booktx.review_acceptance import (
    SubmittedReview,
    accept_review_submission,
)
from booktx.review_tasks import create_review_task, select_review_records
from booktx.status import build_status_snapshot

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


def _store_record(rid: str, source: str):
    cid, pid = (int(x) for x in rid.split("-"))
    return StoredTranslationRecordV2(
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


def _build_review_task(tmp_path: Path):
    """Create a project, store, and a pass-1 review task over all records."""
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
        active_passes=[1],
        passes=[ReviewPassConfig(pass_number=1, enforce="warn")],
    )
    selected = select_review_records(bundle, records, cfg, pass_number=1)
    chapter = next(iter(bundle.index.chapters_by_id.values()))
    task = create_review_task(
        proj, bundle, cfg, selected, pass_number=1, chapter=chapter
    )
    return proj, bundle, cfg, task, records


def test_unchanged_target_creates_review_candidate(tmp_path: Path):
    proj, bundle, cfg, task, records = _build_review_task(tmp_path)
    submitted = [SubmittedReview(id=r.id, target=r.base_target) for r in task.records]
    result = accept_review_submission(
        proj, task, submitted, bundle=bundle, quality_cfg=cfg
    )
    assert result.accepted_records == len(submitted)
    store = load_translation_store(proj)
    for rec in task.records:
        stored = store.records[rec.id]
        assert any(rv.review_ref == rec.review_ref for rv in stored.reviews)
        # active_review set conservatively (no prior active review).
        assert stored.active_review == rec.review_ref


def test_changed_target_does_not_alter_base_version(tmp_path: Path):
    proj, bundle, cfg, task, records = _build_review_task(tmp_path)
    submitted = [
        SubmittedReview(id=r.id, target=r.base_target + " polished")
        for r in task.records
    ]
    accept_review_submission(proj, task, submitted, bundle=bundle, quality_cfg=cfg)
    store = load_translation_store(proj)
    for rec in task.records:
        stored = store.records[rec.id]
        # Base translation version target is unchanged.
        assert stored.versions[0].target == rec.base_target
        # Review candidate carries the polished target.
        review = next(rv for rv in stored.reviews if rv.review_ref == rec.review_ref)
        assert review.target == rec.base_target + " polished"


def test_base_drift_rejects_atomically(tmp_path: Path):
    proj, bundle, cfg, task, records = _build_review_task(tmp_path)
    # Drift the base translation target after the task was created.
    store = load_translation_store(proj)
    first_id = task.records[0].id
    store.records[first_id].versions[0].target = "drifted base target"
    write_translation_store(proj, store)
    submitted = [SubmittedReview(id=r.id, target=r.base_target) for r in task.records]
    from booktx.config import BooktxError

    with pytest.raises(BooktxError) as excinfo:
        accept_review_submission(proj, task, submitted, bundle=bundle, quality_cfg=cfg)
    assert excinfo.value.code == "review_base_drift"
    # Store unchanged on rejection: no review candidate written.
    store2 = load_translation_store(proj)
    assert all(len(st.reviews) == 0 for st in store2.records.values())


def test_no_activate_leaves_active_review_unchanged(tmp_path: Path):
    proj, bundle, cfg, task, records = _build_review_task(tmp_path)
    submitted = [SubmittedReview(id=r.id, target=r.base_target) for r in task.records]
    result = accept_review_submission(
        proj, task, submitted, bundle=bundle, quality_cfg=cfg, no_activate=True
    )
    assert result.activated is False
    store = load_translation_store(proj)
    # Candidates exist but active_review stays None.
    for rec in task.records:
        stored = store.records[rec.id]
        assert any(rv.review_ref == rec.review_ref for rv in stored.reviews)
        assert stored.active_review is None


def test_idempotent_resubmission_is_noop(tmp_path: Path):
    proj, bundle, cfg, task, records = _build_review_task(tmp_path)
    submitted = [SubmittedReview(id=r.id, target=r.base_target) for r in task.records]
    accept_review_submission(proj, task, submitted, bundle=bundle, quality_cfg=cfg)
    # Resubmit identical targets: no error, no duplicate candidates.
    result = accept_review_submission(
        proj, task, submitted, bundle=bundle, quality_cfg=cfg
    )
    assert result.accepted_records == len(submitted)
    store = load_translation_store(proj)
    for rec in task.records:
        stored = store.records[rec.id]
        assert sum(1 for rv in stored.reviews if rv.review_ref == rec.review_ref) == 1


def test_conflicting_review_ref_rejected(tmp_path: Path):
    proj, bundle, cfg, task, records = _build_review_task(tmp_path)
    submitted = [SubmittedReview(id=r.id, target=r.base_target) for r in task.records]
    accept_review_submission(proj, task, submitted, bundle=bundle, quality_cfg=cfg)
    # Resubmit same review_ref with a different target -> conflict.
    conflict = [
        SubmittedReview(id=r.id, target=r.base_target + " different")
        for r in task.records
    ]
    from booktx.config import BooktxError

    with pytest.raises(BooktxError) as excinfo:
        accept_review_submission(proj, task, conflict, bundle=bundle, quality_cfg=cfg)
    assert excinfo.value.code == "review_ref_conflict"
