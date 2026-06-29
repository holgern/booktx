"""Domain workflow functions called by :mod:`booktx.commands`.

A workflow is a plain Python function (no :mod:`typer` arguments) that takes
validated inputs and returns a domain result. Workflows own the mutation
logic that previously lived in ``booktx/cli.py``: store writes, ledger
updates, context mutations, filesystem path effects, and so on. The Typer
commands in :mod:`booktx.commands` are thin wrappers that parse options,
invoke one workflow, render the result, and map :class:`booktx.errors.BooktxError`
to exit codes.

This is the boundary enforced by ``tests/test_cli_command_boundary.py``:

- Workflow functions may import and use the lower-level store/ledger/context
  modules and filesystem helpers.
- Typer commands under :mod:`booktx.commands` must not.

The decomposition proceeds slice by slice (identity, source, epub,
profile/version, context, review, translate, root). New workflow modules
land here as their slice is extracted from ``booktx/cli.py``.
"""

from __future__ import annotations
