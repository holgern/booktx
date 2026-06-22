"""Placeholder protection and restoration for names and inline non-translatable spans.

booktx never sends raw protected names or inline markup to the translating
agent. Instead it replaces them with stable, collision-free tokens before
segmentation, and restores the originals verbatim after the agent returns the
translated text.

Two placeholder kinds are used (see ``booktx_coding_agent_start.md``):

- ``__NAME_NNN__`` — a manually protected term from ``names.json``
  (e.g. ``Alice`` -> ``__NAME_001__``).
- ``__TAG_NNN__``  — an inline non-translatable span extracted from the document
  (inline code, URLs/link destinations) -> ``__TAG_001__``.

Tokens are unique within a document and round-trip safe: applying
:func:`protect_names` then :func:`restore` (or :func:`protect_tags` then
:func:`restore`) returns the original text exactly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from booktx.models import Placeholder

__all__ = [
    "NAME_TOKEN_RE",
    "TAG_TOKEN_RE",
    "TOKEN_RE",
    "ProtectResult",
    "protect_names",
    "protect_tags",
    "restore",
]

# Tokens look like __NAME_001__ / __TAG_001__. The id is 1-based, zero-padded
# to at least 3 digits.
NAME_TOKEN_RE = re.compile(r"__NAME_(\d+)__")
TAG_TOKEN_RE = re.compile(r"__TAG_(\d+)__")
TOKEN_RE = re.compile(r"__(?:NAME|TAG)_(\d+)__")

# Any string that already looks like one of our tokens is never itself
# protected (prevents pathological double-encoding).
_LOOKS_LIKE_TOKEN = re.compile(r"__(?:NAME|TAG)_\d+__")


@dataclass(slots=True)
class ProtectResult:
    """Outcome of a protect pass.

    ``text`` has originals replaced by tokens. ``placeholders`` is ordered by
    first appearance and each ``Placeholder.original`` is the verbatim string to
    restore.
    """

    text: str
    placeholders: list[Placeholder]


def _next_token(prefix: str, taken: set[str], idx: int) -> str:
    """Return the next token not already in ``taken``."""
    while True:
        token = f"__{prefix}_{idx:03d}__"
        if token not in taken:
            return token
        idx += 1


def protect_names(
    text: str, terms: list[str], *, start_index: int = 1
) -> ProtectResult:
    """Replace each protected term in ``text`` with a ``__NAME_NNN__" token.

    Matching is case-sensitive and whole-term (no word-boundary tricks) so
    multi-word names like ``Mr. Smith`` win over ``Mr.``. Tokens are numbered
    by **first appearance** in the text (longest terms are matched first to
    avoid sub-string collisions, but the token id follows text order).
    """
    if not terms:
        return ProtectResult(text=text, placeholders=[])

    # Longest-first matching prevents "Mr." stealing "Mr. Smith".
    unique_terms = sorted(
        {t for t in terms if t and not _LOOKS_LIKE_TOKEN.fullmatch(t)},
        key=len,
        reverse=True,
    )
    # Token ids follow first-appearance order in the source text.
    appearances: list[tuple[int, str]] = []
    for term in unique_terms:
        pos = text.find(term)
        if pos >= 0:
            appearances.append((pos, term))
    appearances.sort(key=lambda x: (x[0], -len(x[1])))

    placeholders: list[Placeholder] = []
    taken_tokens: set[str] = set()
    seen_originals: dict[str, str] = {}  # original -> token
    out = text

    for idx, (_, term) in enumerate(appearances, start=start_index):
        if term in seen_originals:
            out = out.replace(term, seen_originals[term])
            continue
        token = _next_token("NAME", taken_tokens, idx)
        taken_tokens.add(token)
        seen_originals[term] = token
        out = out.replace(term, token)
        placeholders.append(Placeholder(token=token, original=term, kind="name"))

    return ProtectResult(text=out, placeholders=placeholders)


def protect_tags(
    text: str, originals: list[str], *, start_index: int = 1
) -> ProtectResult:
    """Replace each inline non-translatable span with a ``__TAG_NNN__`` token.

    ``originals`` is the list of verbatim spans to hide (inline code, URLs, …)
    as discovered by the format extractor. They are replaced longest-first and
    only if the span actually occurs in ``text``.
    """
    placeholders: list[Placeholder] = []
    taken_tokens: set[str] = set()
    seen: dict[str, str] = {}
    idx = start_index
    out = text

    for span in sorted({s for s in originals if s}, key=len, reverse=True):
        if _LOOKS_LIKE_TOKEN.fullmatch(span):
            continue
        if span in seen:
            out = out.replace(span, seen[span])
            continue
        if span not in out:
            continue
        token = _next_token("TAG", taken_tokens, idx)
        idx += 1
        taken_tokens.add(token)
        seen[span] = token
        out = out.replace(span, token)
        placeholders.append(Placeholder(token=token, original=span, kind="tag"))

    return ProtectResult(text=out, placeholders=placeholders)


def restore(text: str, placeholders: list[Placeholder]) -> str:
    """Replace each token in ``text`` with its recorded original.

    Used by the build step on the *agent's translated* text. Tokens are restored
    verbatim; missing tokens are left in place (the validator flags those).
    """
    out = text
    # Restore longest originals first to avoid partial-token collisions; the
    # token form is fixed-width so ordering does not actually matter for tokens,
    # but sorting keeps behaviour deterministic across runs.
    for ph in sorted(placeholders, key=lambda p: p.token, reverse=True):
        out = out.replace(ph.token, ph.original)
    return out


def collect_tokens(text: str) -> list[str]:
    """Return all placeholder tokens found in ``text``, in order of appearance."""
    return [m.group(0) for m in re.finditer(r"__(?:NAME|TAG)_\d+__", text)]
