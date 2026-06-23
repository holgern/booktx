"""Direct unit tests for the booktx.status service.

The status service was extracted out of booktx.cli. These tests exercise the
typed models and ``build_status_snapshot`` without going through Typer, and
they pin the public ``status --json`` v1 shape (no ``_private`` keys, nested
``record_range``).
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from booktx.cli import app
from booktx.config import (
    load_project,
    write_translation_store,
    write_translation_version_ledger,
)
from booktx.models import (
    StoredTranslationRecordV2,
    TranslationCandidate,
    TranslationStoreV2,
    TranslationSubversionLedgerEntry,
    TranslationTrackLedgerEntry,
    TranslationVersionLedger,
)
from booktx.progress import source_record_sha256
from booktx.status import (
    ChapterProgress,
    RecordRange,
    StatusBundle,
    StatusRuntimeIndex,
    StatusSnapshot,
    StatusTotals,
    build_status_snapshot,
    coverage_status,
    selected_chapter,
)

runner = CliRunner()

DOC = """\
# Chapter One

Alice met Bob. They were happy.

# Chapter Two

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


def _write_versioned_store(project_dir: Path) -> None:
    proj = load_project(project_dir)
    chunk = json.loads(sorted((proj.chunks_dir).glob("*.json"))[0].read_text("utf-8"))
    first_record = chunk["records"][0]
    write_translation_store(
        proj,
        TranslationStoreV2(
            records={
                first_record["id"]: StoredTranslationRecordV2(
                    chunk_id=1,
                    part_id=1,
                    source_sha256=source_record_sha256(first_record["source"]),
                    source=first_record["source"],
                    active_version="1.1",
                    versions=[
                        TranslationCandidate(
                            version=1,
                            subversion=1,
                            version_ref="1.1",
                            target=first_record["source"],
                            created_at="2026-06-22T12:00:00Z",
                            updated_at="2026-06-22T12:00:00Z",
                        ),
                        TranslationCandidate(
                            version=1,
                            subversion=2,
                            version_ref="1.2",
                            target="Andere Fassung",
                            created_at="2026-06-22T12:10:00Z",
                            updated_at="2026-06-22T12:10:00Z",
                        ),
                    ],
                )
            }
        ),
    )
    write_translation_version_ledger(
        proj,
        TranslationVersionLedger(
            active_version="1.2",
            tracks={
                "1": TranslationTrackLedgerEntry(
                    version=1,
                    actor="user:nahrstaedt",
                    harness="pi",
                    model="codex-openai/gpt-5.5@low",
                    label="gpt-5.5 low",
                    created_at="2026-06-22T12:00:00Z",
                    updated_at="2026-06-22T12:10:00Z",
                    subversions={
                        "1": TranslationSubversionLedgerEntry(
                            version=1,
                            subversion=1,
                            version_ref="1.1",
                            context_sha256="a" * 64,
                            created_at="2026-06-22T12:00:00Z",
                            updated_at="2026-06-22T12:00:00Z",
                        ),
                        "2": TranslationSubversionLedgerEntry(
                            version=1,
                            subversion=2,
                            version_ref="1.2",
                            context_sha256="b" * 64,
                            created_at="2026-06-22T12:10:00Z",
                            updated_at="2026-06-22T12:10:00Z",
                        ),
                    },
                )
            },
        ),
    )


def test_coverage_status_labels():
    assert coverage_status(total=3, translated=0, has_error=False) == "pending"
    assert coverage_status(total=3, translated=2, has_error=False) == "in_progress"
    assert coverage_status(total=3, translated=3, has_error=False) == "complete"
    assert coverage_status(total=3, translated=0, has_error=True) == "invalid"
    # error wins over complete
    assert coverage_status(total=3, translated=3, has_error=True) == "invalid"


def test_build_status_snapshot_returns_typed_bundle(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    proj = load_project(project_dir)

    bundle = build_status_snapshot(proj, context_exists=False, context_ready=False)

    assert isinstance(bundle, StatusBundle)
    assert isinstance(bundle.snapshot, StatusSnapshot)
    assert isinstance(bundle.index, StatusRuntimeIndex)
    assert isinstance(bundle.snapshot.totals, StatusTotals)

    # Runtime index carries the live lookup maps.
    assert bundle.index.source_chunks
    assert bundle.index.source_by_id
    assert bundle.index.record_to_chapter
    assert bundle.index.chunk_summaries


def test_snapshot_serializes_to_v1_shape_without_private_keys(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    proj = load_project(project_dir)

    bundle = build_status_snapshot(proj, context_exists=True, context_ready=True)
    dumped = bundle.snapshot.model_dump(mode="json")

    # Exactly the v1 public keys, nothing private leaks.
    assert set(dumped.keys()) == {
        "version",
        "project",
        "source",
        "context",
        "totals",
        "next",
        "chapters",
        "version_coverage",
        "track_coverage",
    }
    # Chapters use the nested record_range shape (v1 contract).
    nxt = dumped["next"]
    assert nxt is not None
    assert set(nxt["record_range"].keys()) == {"start", "end"}
    # The CLI JSON path must agree with this dump.
    res = runner.invoke(app, ["status", str(project_dir), "--json"])
    assert res.exit_code == 0, res.output
    cli_dumped = json.loads(res.output)
    assert cli_dumped["totals"] == dumped["totals"]
    assert cli_dumped["source"] == dumped["source"]


def test_status_json_includes_version_and_track_coverage(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    _write_versioned_store(project_dir)
    proj = load_project(project_dir)

    bundle = build_status_snapshot(proj, context_exists=True, context_ready=True)

    assert [item.version_ref for item in bundle.snapshot.version_coverage] == [
        "1.1",
        "1.2",
    ]
    assert bundle.snapshot.version_coverage[0].active_records == 1
    assert bundle.snapshot.track_coverage[0].label == "gpt-5.5 low"
    assert bundle.snapshot.track_coverage[0].latest_subversion == 2


def test_selected_chapter_returns_next_for_none(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    proj = load_project(project_dir)

    bundle = build_status_snapshot(proj, context_exists=False, context_ready=False)
    first = selected_chapter(bundle, None)
    assert isinstance(first, ChapterProgress)
    assert isinstance(first.record_range, RecordRange)
    assert first.records_remaining > 0

    # Unknown id resolves to None; the CLI wrapper owns the die-on-unknown UX.
    assert selected_chapter(bundle, "does-not-exist") is None
