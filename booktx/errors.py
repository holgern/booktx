"""User-facing error type for booktx.

``BooktxError`` carries a stable ``code`` attribute so callers (CLI, validation,
workflows) can branch on the error kind without parsing prose. It is defined
here rather than in :mod:`booktx.config` so that low-level helpers such as
:mod:`booktx.path_ids` can raise it without importing ``config`` (which would
create an import cycle).

:mod:`booktx.config` re-exports ``BooktxError`` and ``_err`` for backwards
compatibility; existing ``from booktx.config import BooktxError`` imports keep
working.
"""

from __future__ import annotations


class BooktxError(Exception):
    """User-facing error from booktx. Carries a stable ``code`` attribute."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _err(code: str, message: str) -> BooktxError:
    """Construct a :class:`BooktxError` (helper kept for concise call sites)."""
    return BooktxError(code, message)
