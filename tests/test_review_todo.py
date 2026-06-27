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
