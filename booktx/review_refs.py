"""Helpers for translation review pass references.

A review reference identifies one quality-improved candidate stored separately
from translation versions. It uses a separate namespace from dotted translation
versions (``1.1``) so the two can never be confused:

    R<pass_number>.<run_number>

Examples:

    R1.1  first review pass, first run
    R1.2  first review pass, second alternative run
    R2.1  second review pass, first run

Review refs must be visibly different from translation versions and must use
positive integer parts, so ``R0.1`` and ``R1.0`` are rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "ReviewRef",
    "format_review_ref",
    "parse_review_ref",
]

_REVIEW_REF_RE = re.compile(r"^R(?P<pass>[1-9]\d*)\.(?P<run>[1-9]\d*)$")


def format_review_ref(pass_number: int, run_number: int) -> str:
    """Return the canonical review reference."""
    if pass_number <= 0 or run_number <= 0:
        raise ValueError("review references must use positive integer ids")
    return f"R{pass_number}.{run_number}"


@dataclass(frozen=True, order=True, slots=True)
class ReviewRef:
    """Parsed review reference with numeric ordering.

    Ordering follows ``R1.1 < R1.2 < R2.1`` by comparing pass number first,
    then run number.
    """

    pass_number: int
    run_number: int

    def __post_init__(self) -> None:
        format_review_ref(self.pass_number, self.run_number)

    @property
    def review_ref(self) -> str:
        return format_review_ref(self.pass_number, self.run_number)

    def __str__(self) -> str:
        return self.review_ref


def parse_review_ref(value: str) -> ReviewRef:
    """Parse and validate a review reference such as ``R1.1``.

    Accepts only the ``R<pass>.<run>`` shape with positive integer parts.
    Rejects translation-version-like refs (``1.1``), unprefixed or hyphenated
    forms (``review-1``), and zero parts (``R0.1``, ``R1.0``).
    """
    text = value.strip()
    match = _REVIEW_REF_RE.fullmatch(text)
    if match is None:
        raise ValueError(f"invalid review reference: {value!r}")
    return ReviewRef(
        pass_number=int(match.group("pass")),
        run_number=int(match.group("run")),
    )
