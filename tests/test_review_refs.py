"""Tests for booktx.review_refs: parse, format, and order review references."""

from __future__ import annotations

import pytest

from booktx.review_refs import (
    ReviewRef,
    format_review_ref,
    parse_review_ref,
)


@pytest.mark.parametrize("value", ["R1.1", "R1.2", "R2.1", "R10.3"])
def test_parse_accepts_valid_refs(value: str) -> None:
    ref = parse_review_ref(value)
    assert ref.review_ref == value


@pytest.mark.parametrize(
    "value",
    [
        "1.1",
        "1.2",
        "2.1",
        "review-1",
        "R0.1",
        "R1.0",
        "R1",
        "R1.",
        "R.1",
        "r1.1",
        "R1.1.1",
        "R-1.1",
        "",
        "R 1.1",
    ],
)
def test_parse_rejects_malformed_refs(value: str) -> None:
    with pytest.raises(ValueError):
        parse_review_ref(value)


def test_parse_extracts_numeric_parts() -> None:
    ref = parse_review_ref("R2.3")
    assert ref.pass_number == 2
    assert ref.run_number == 3


def test_format_review_ref() -> None:
    assert format_review_ref(1, 1) == "R1.1"
    assert format_review_ref(2, 1) == "R2.1"


@pytest.mark.parametrize("pass_number, run_number", [(0, 1), (1, 0), (-1, 1), (1, -1)])
def test_format_rejects_nonpositive_parts(pass_number: int, run_number: int) -> None:
    with pytest.raises(ValueError):
        format_review_ref(pass_number, run_number)


def test_parse_strips_whitespace() -> None:
    # Leading/trailing whitespace is stripped, matching translation-version parsing.
    ref = parse_review_ref("  R1.1  ")
    assert ref.review_ref == "R1.1"


def test_ordering_is_numeric_pass_then_run() -> None:
    refs = [parse_review_ref(v) for v in ["R2.1", "R1.2", "R1.1", "R1.10"]]
    refs.sort()
    assert [r.review_ref for r in refs] == ["R1.1", "R1.2", "R1.10", "R2.1"]


def test_review_ref_str_renders_canonical() -> None:
    assert str(parse_review_ref("R1.1")) == "R1.1"


def test_review_ref_is_frozen_and_hashable() -> None:
    a = ReviewRef(pass_number=1, run_number=1)
    b = ReviewRef(pass_number=1, run_number=1)
    assert a == b
    assert hash(a) == hash(b)
    with pytest.raises(AttributeError):
        a.pass_number = 2  # type: ignore[misc]
