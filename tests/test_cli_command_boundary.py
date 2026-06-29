"""CLI command/workflow boundary guard (Phase 3).

Enforces the rule documented in ``booktx/commands/__init__.py``:

- A Typer command module under ``booktx/commands/`` may parse options, call
  one workflow function from ``booktx.workflows``, render the result, and
  map ``BooktxError`` to exit codes.
- It must not directly import the low-level store/ledger/context writers
  or filesystem path helpers that perform writes.

The guard is intentionally a static AST scan so it can run without booting
the full Typer app. It is exercised against every module under
``booktx/commands/`` (excluding ``__init__.py``). Until Phase 3 slices are
extracted, the command package is empty and the guard passes vacuously; the
guard is the invariant that protects the boundary as new command modules
land here.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

# Forbidden import substrings: any import (from X import Y) whose Y matches
# one of these is a direct-mutation escape hatch that the workflow layer
# must own. Tuned to be conservative -- a false positive (legitimate helper
# sharing a name) is acceptable, false negatives are not.
FORBIDDEN_IMPORT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Translation store writers / mutators.
    re.compile(r"^write_translation_store$"),
    re.compile(r"^write_translation_version_ledger$"),
    re.compile(r"^load_translation_store$"),
    # Context writer (commands must go through workflows/context.py).
    re.compile(r"^write_context$"),
    re.compile(r"^write_context_json$"),
    re.compile(r"^write_context_md$"),
    # Filesystem path writers.
    re.compile(r"^write_text_atomic$"),
    re.compile(r"^write_json_atomic$"),
    re.compile(r"^write_json_text_atomic$"),
    re.compile(r"^write_json_model_atomic$"),
    # Profile / store path constructors that perform writes implicitly.
    re.compile(r"^write_profile_config$"),
    re.compile(r"^write_translation_task$"),
    re.compile(r"^write_translation_review_task$"),
    re.compile(r"^write_review_todo$"),
    re.compile(r"^write_translation_audit$"),
)

# Direct module imports that commands must not pull in (they are the
# mutation layer).
FORBIDDEN_MODULE_IMPORTS: tuple[str, ...] = (
    "booktx.translation_store",  # store internals; use workflow.load_translation_store
    "booktx.config",  # config holds many writers; workflow layer wraps it
)

COMMANDS_DIR = Path(__file__).resolve().parents[1] / "booktx" / "commands"


def _iter_command_modules() -> list[Path]:
    if not COMMANDS_DIR.is_dir():
        return []
    return [p for p in sorted(COMMANDS_DIR.glob("*.py")) if p.name != "__init__.py"]


def _extract_imports(path: Path) -> tuple[set[str], set[str]]:
    """Return (module_names, imported_names) imported by ``path``."""
    tree = ast.parse(path.read_text("utf-8"))
    modules: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
            for alias in node.names:
                names.add(alias.name)
    return modules, names


def test_commands_and_workflows_packages_exist_and_documented() -> None:
    """The boundary contract is in place before any slice is extracted."""
    assert (COMMANDS_DIR / "__init__.py").is_file()
    assert (COMMANDS_DIR.parent / "workflows" / "__init__.py").is_file()
    text = (COMMANDS_DIR / "__init__.py").read_text("utf-8")
    assert (
        "must not directly mutate" in text.lower() or "must NOT directly mutate" in text
    )
    assert "workflow" in text.lower()


def test_no_command_module_imports_forbidden_mutation_helpers() -> None:
    """Every booktx/commands/*.py respects the boundary."""
    violations: list[str] = []
    for path in _iter_command_modules():
        modules, names = _extract_imports(path)
        for mod in modules:
            for forbidden in FORBIDDEN_MODULE_IMPORTS:
                if mod == forbidden or mod.startswith(forbidden + "."):
                    violations.append(
                        f"{path.name}: forbidden direct import of {mod!r}"
                    )
        for name in names:
            for pattern in FORBIDDEN_IMPORT_PATTERNS:
                if pattern.search(name):
                    violations.append(
                        f"{path.name}: forbidden direct import of {name!r}"
                    )
    assert not violations, "boundary violations:\n" + "\n".join(violations)


def test_guard_self_test_catches_a_known_violation(tmp_path: Path) -> None:
    """The scanner detects a synthetic violation. Protects the guard itself
    from silent rot (e.g. someone weakening the deny-list to make the test
    pass)."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from booktx.config import write_translation_store\n"
        "from booktx.translation_store import load_translation_store\n",
        encoding="utf-8",
    )
    modules, names = _extract_imports(bad)
    found = False
    for name in names:
        if any(p.search(name) for p in FORBIDDEN_IMPORT_PATTERNS):
            found = True
            break
    if not found:
        for mod in modules:
            if any(
                mod == fm or mod.startswith(fm + ".") for fm in FORBIDDEN_MODULE_IMPORTS
            ):
                found = True
                break
    assert found, "guard self-test must detect a known violation"
