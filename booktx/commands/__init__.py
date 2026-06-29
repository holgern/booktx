"""Typer command registration per domain slice.

Each module under this package defines a thin :mod:`typer` app that
parses options, delegates the actual work to a function in
:mod:`booktx.workflows.<domain>`, renders the result, and maps
:class:`booktx.errors.BooktxError` to exit codes.

The boundary rule (enforced by ``tests/test_cli_command_boundary.py``):

- A Typer command may parse options, call one workflow function, render the
  result, and map ``BooktxError`` to exit codes.
- A Typer command must NOT directly mutate
  :class:`booktx.models.TranslationContext`,
  :class:`booktx.translation_store.TranslationStoreV2`,
  :class:`booktx.models.TranslationVersionLedger`, or filesystem paths except
  through a workflow / service module.

Intentionally public imports from ``booktx.cli`` continue to be re-exported
by ``booktx/cli.py`` (the stable app assembly entrypoint) so downstream code
that does ``from booktx.cli import app`` keeps working.
"""

from __future__ import annotations
