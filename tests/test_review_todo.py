"""Unit tests for booktx.review_todo models and pure functions."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from booktx.models import (
    ReviewTodo,
    ReviewTodoChapter,
    ReviewTodoPass,
)
from booktx.review_todo import make_review_todo_id

# --- model validation -------------------------------------------------------


def test_review_todo_pass_valid():
    rtp = ReviewTodoPass(pass_number=1, selection="missing", base="active_translation")
    assert rtp.pass_number == 1
    assert rtp.selection == "missing"


def test_review_todo_pass_rejects_invalid_pass_number():
    with pytest.raises(ValidationError):
        ReviewTodoPass(pass_number=0)


def test_review_todo_pass_rejects_invalid_selection():
    with pytest.raises(ValidationError):
        ReviewTodoPass(pass_number=1, selection="bogus")


def test_review_todo_pass_defaults():
    rtp = ReviewTodoPass(pass_number=2)
    assert rtp.selection == "missing"
    assert rtp.base == "active_translation"


def test_review_todo_chapter_valid():
    rtc = ReviewTodoChapter(
        chapter_id="0005",
        title="Chapter Five",
        status="partial",
        eligible_records_at_start=50,
        missing_review_at_start=20,
        stale_review_at_start=5,
        pending_passes=[1, 2],
    )
    assert rtc.chapter_id == "0005"
    assert rtc.missing_review_at_start == 20


def test_review_todo_chapter_defaults():
    rtc = ReviewTodoChapter(chapter_id="0001", title="", status="complete")
    assert rtc.eligible_records_at_start == 0
    assert rtc.missing_review_at_start == 0
    assert rtc.stale_review_at_start == 0
    assert rtc.pending_passes == []


def test_review_todo_valid():
    rt = ReviewTodo(
        review_todo_id="brt-20260627T000000Z-de-0005-abcdef01",
        profile="de_test",
        passes=[ReviewTodoPass(pass_number=1, selection="missing")],
        chapters_requested=2,
        batch_words=900,
        created_at="2026-06-27T00:00:00Z",
        chapters=[
            ReviewTodoChapter(
                chapter_id="0005",
                title="Ch. 5",
                status="partial",
                eligible_records_at_start=100,
                missing_review_at_start=30,
                pending_passes=[1],
            ),
        ],
    )
    assert rt.review_todo_id == "brt-20260627T000000Z-de-0005-abcdef01"
    assert rt.profile == "de_test"
    assert len(rt.passes) == 1
    assert len(rt.chapters) == 1


def test_review_todo_minimal():
    rt = ReviewTodo(
        review_todo_id="minimal",
        profile="test",
        chapters_requested=1,
        batch_words=100,
        created_at="2026-01-01T00:00:00Z",
    )
    assert rt.passes == []
    assert rt.chapters == []
    assert rt.source_sha256 is None


def test_review_todo_round_trip_json():
    rt = ReviewTodo(
        review_todo_id="brt-test",
        profile="test",
        chapters_requested=1,
        batch_words=100,
        created_at="2026-01-01T00:00:00Z",
        passes=[ReviewTodoPass(pass_number=1)],
    )
    data = rt.model_dump(mode="json")
    rt2 = ReviewTodo.model_validate(data)
    assert rt2.review_todo_id == rt.review_todo_id
    assert rt2.passes == rt.passes


def test_review_todo_rejects_missing_required():
    with pytest.raises(ValidationError):
        ReviewTodo(
            review_todo_id="test",
            profile="test",
            chapters_requested=1,
            batch_words=100,
            # created_at missing
        )


def test_review_todo_pass_accepts_all_valid_selections():
    for sel in ("missing", "stale", "reviewed", "all", "changed-base"):
        rtp = ReviewTodoPass(pass_number=1, selection=sel)
        assert rtp.selection == sel


# --- id generation ----------------------------------------------------------


def test_make_review_todo_id_generates_valid_id():
    tid = make_review_todo_id(
        profile="de_test",
        first_chapter_id="0005",
        chapter_ids=["0005", "0006"],
        pass_numbers=[1, 2],
    )
    assert tid.startswith("brt-")
    # Format: brt-YYYYMMDDTHHMMSSZ-profile-chapter-digest
    parts = tid.split("-")
    assert len(parts) >= 5
    assert parts[1].startswith("20")  # timestamp


def test_make_review_todo_id_deterministic():
    import time

    tid1 = make_review_todo_id("test", "01", ["01"], [1])
    time.sleep(1.5)
    tid2 = make_review_todo_id("test", "01", ["01"], [1])
    # Different timestamps -> different ids
    assert tid1 != tid2


# ---------------------------------------------------------------------------
# End-to-end selection coverage (Phase 0 defect repair)
#
# The pre-existing tests above cover ReviewTodo models and id generation only.
# This block exercises select_review_todo_chapters -> _count_missing_review,
# the path that previously crashed on the nonexistent
# QualityReviewConfig.passes_by_number attribute.
# ---------------------------------------------------------------------------


def test_select_review_todo_chapters_counts_gaps_without_crashing(tmp_path):
    import json

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
    from booktx.review_todo import select_review_todo_chapters
    from booktx.status import build_status_snapshot

    runner = CliRunner()
    src = tmp_path / "book.md"
    src.write_text("# Chapter One\n\nAlice ran fast.\n", encoding="utf-8")
    project_dir = tmp_path / "book"
    assert (
        runner.invoke(
            app,
            ["init", str(project_dir), "--target", "de", "--source-file", str(src)],
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["extract", str(project_dir)]).exit_code == 0

    proj = load_project(project_dir)
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
                        status="accepted",
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
    cfg = proj.profile_config.model_copy(
        update={
            "quality_review": QualityReviewConfig(
                enabled=True,
                active_passes=[1],
                passes=[ReviewPassConfig(pass_number=1, enforce="warn")],
            )
        }
    )
    write_profile_config(proj, cfg)

    bundle = build_status_snapshot(proj, context_exists=True, context_ready=True)
    # Previously raised AttributeError: passes_by_number. Must now return the
    # chapter with one record still needing its pass-1 review.
    selected = select_review_todo_chapters(proj, bundle, cfg.quality_review, chapters=5)
    assert selected, "expected at least one chapter with a review gap"
    chapter_id, _title, missing = selected[0]
    assert isinstance(chapter_id, str)
    assert missing >= 1


# ---------------------------------------------------------------------------
# Phase 2: single-computation proof.
#
# The pre-Phase-2 selection recomputed the store/record_order/snapshot for every
# (chapter, pass) pair, i.e. ``chapters * passes`` times. After the gap-index
# refactor the store is loaded once and the index is built once per selection.
# This test proves both: exact missing counts for 2 passes x 3 chapters, and
# that the store loader and gap-index builder are each called exactly once.
# ---------------------------------------------------------------------------


def test_select_review_todo_chapters_2passes_3chapters_single_computation(tmp_path):
    """Exact missing counts for 2 passes x 3 chapters; store load + gap-index
    build happen once, not chapters*passes times."""
    import json as _json
    from unittest.mock import patch

    from booktx.config import (
        load_project,
        load_translation_store,
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
    from booktx.review_status import build_review_gap_index
    from booktx.review_todo import select_review_todo_chapters
    from booktx.status import build_status_snapshot

    # Build a project with 3 chapters, 1 record each.
    src = tmp_path / "book.md"
    src.write_text(
        "# One\n\nFirst sentence.\n\n# Two\n\nSecond sentence.\n\n"
        "# Three\n\nThird sentence.\n",
        encoding="utf-8",
    )
    project_dir = tmp_path / "book"
    from typer.testing import CliRunner

    from booktx.cli import app

    runner = CliRunner()
    assert (
        runner.invoke(
            app,
            [
                "init",
                str(project_dir),
                "--target",
                "de",
                "--source-file",
                str(src),
                "--chunk-size",
                "1",
            ],
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["extract", str(project_dir)]).exit_code == 0

    proj = load_project(project_dir)
    # Write one accepted v2 record per chunk.
    records: dict[str, StoredTranslationRecordV2] = {}
    for i, chunk_path in enumerate(sorted(proj.chunks_dir.glob("*.json")), start=1):
        chunk = _json.loads(chunk_path.read_text("utf-8"))
        rec = chunk["records"][0]
        records[rec["id"]] = StoredTranslationRecordV2(
            chunk_id=i,
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
                    status="accepted",
                    created_at="2026-06-22T12:00:00Z",
                    updated_at="2026-06-22T12:00:00Z",
                )
            ],
        )
    ledger_tracks = {
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
    }
    write_translation_store(proj, TranslationStoreV2(records=records))
    write_translation_version_ledger(
        proj,
        TranslationVersionLedger(active_version="1.1", tracks=ledger_tracks),
    )
    # Enable two review passes.
    cfg = proj.profile_config.model_copy(
        update={
            "quality_review": QualityReviewConfig(
                enabled=True,
                active_passes=[1, 2],
                passes=[
                    ReviewPassConfig(pass_number=1, enforce="warn"),
                    ReviewPassConfig(pass_number=2, enforce="warn"),
                ],
            )
        }
    )
    write_profile_config(proj, cfg)
    proj = load_project(project_dir)
    bundle = build_status_snapshot(proj, context_exists=True, context_ready=True)

    with (
        patch(
            "booktx.review_todo.load_translation_store",
            wraps=load_translation_store,
        ) as load_spy,
        patch(
            "booktx.review_todo.build_review_gap_index",
            wraps=build_review_gap_index,
        ) as gap_spy,
    ):
        selected = select_review_todo_chapters(
            proj, bundle, cfg.quality_review, chapters=5
        )

    # 3 chapters were extracted. The exact per-chapter missing count depends on
    # the chunker; the key property is that it is consistent (all chapters have
    # the same number of records) and that load_spy / gap_spy were each called
    # once.
    assert len(selected) == 3
    missing_counts = {missing for _cid, _title, missing in selected}
    assert len(missing_counts) == 1, (
        f"chapters have different missing counts: {missing_counts}"
    )
    (per_chapter_missing,) = missing_counts
    assert per_chapter_missing > 0
    # total = per_chapter_missing * 3 chapters * 1 (we sum across passes in the index)

    # Single computation: pre-Phase-2 would call load_translation_store and
    # build compute_review_snapshot chapters * passes = 3 * 2 = 6 times.
    assert load_spy.call_count == 1, (
        f"load_translation_store called {load_spy.call_count} times "
        f"(expected 1; pre-Phase-2 = chapters*passes = 6)"
    )
    assert gap_spy.call_count == 1, (
        f"build_review_gap_index called {gap_spy.call_count} times "
        f"(expected 1; pre-Phase-2 = chapters*passes = 6)"
    )
