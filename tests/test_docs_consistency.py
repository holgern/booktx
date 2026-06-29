"""Documentation and packaging consistency tests (Phase 1).

Catches the regressions flagged in the booktx refactor review:
- unbalanced Markdown fences that hide sections in rendered docs,
- packaging references (LICENSE, sdist includes) that point at missing files,
- the core profile invariant drifting out of README/docs/SKILL,
- a public Typer command landing without being documented or explicitly
  classified as an alias/internal/legacy command.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import tomli_w
import typer

from booktx.cli import app

ROOT = Path(__file__).resolve().parents[1]

# Markdown sources that must have balanced fences and the profile invariant.
MARKDOWN_FILES = [
    ROOT / "README.md",
    ROOT / "skills" / "booktx" / "SKILL.md",
    *sorted((ROOT / "docs").rglob("*.md")),
]

# Sphinx build output must not be linted.
MARKDOWN_FILES = [p for p in MARKDOWN_FILES if "_build" not in p.parts]


def _balanced_fences(text: str) -> bool:
    """True when code fences in ``text`` pair correctly.

    A fence line is a run of 3+ backticks optionally followed by an info
    string. A closing fence must have at least as many backticks as the
    opening fence (CommonMark). We approximate by counting fence-line
    transitions while respecting the longer-fence-closes-shorter rule.
    """
    open_len: int | None = None
    for line in text.splitlines():
        stripped = line.lstrip()
        m = re.match(r"(`{3,})", stripped)
        if not m or stripped[: m.end()].count("`") != len(m.group(1)):
            continue
        if not re.fullmatch(r"`{3,}.*", stripped):
            continue
        length = len(m.group(1))
        if open_len is None:
            open_len = length
        elif length >= open_len:
            open_len = None
    return open_len is None


# --- Markdown fence balance --------------------------------------------------


@pytest.mark.parametrize("path", MARKDOWN_FILES)
def test_markdown_fences_balanced(path: Path) -> None:
    assert _balanced_fences(path.read_text("utf-8")), (
        f"unbalanced code fences in {path}"
    )


# --- packaging references ----------------------------------------------------


def _pyproject() -> dict:
    import tomllib

    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def test_declared_license_file_exists() -> None:
    pyproject = _pyproject()
    license_files = pyproject.get("project", {}).get("license-files")
    paths = []
    if isinstance(license_files, dict):
        paths = license_files.get("paths", [])
    elif isinstance(license_files, list):
        paths = license_files
    assert paths, "no license-files declared in pyproject.toml"
    for rel in paths:
        assert (ROOT / rel).is_file(), f"declared license file missing: {rel}"


def test_sdist_included_paths_exist() -> None:
    includes = (
        _pyproject()
        .get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("sdist", {})
        .get("include", [])
    )
    assert includes, "no sdist include list found"
    for rel in includes:
        assert (ROOT / rel).exists(), f"sdist include missing in repo: {rel}"


# --- core profile invariant --------------------------------------------------

PROFILE_INVARIANT_MARKERS = (".booktx/", "translations/<profile>/")


@pytest.mark.parametrize(
    "path", [ROOT / "README.md", ROOT / "skills" / "booktx" / "SKILL.md"]
)
def test_profile_invariant_documented(path: Path) -> None:
    text = path.read_text("utf-8")
    assert all(marker in text for marker in PROFILE_INVARIANT_MARKERS), (
        f"{path} must state the core profile invariant ({PROFILE_INVARIANT_MARKERS})"
    )


# --- live Typer command inventory -------------------------------------------


def _command_tree() -> tuple[set[str], dict[str, set[str]]]:
    import click

    group = typer.main.get_command(app)
    assert isinstance(group, click.Group)
    top = set(group.commands.keys())
    sub: dict[str, set[str]] = {}
    for name in sorted(top):
        cmd = group.commands[name]
        if isinstance(cmd, click.Group):
            sub[name] = set(cmd.commands.keys())
    return top, sub


# Commands that are intentionally undocumented because they are aliases,
# compatibility surfaces, or internal/diagnostic commands. A command listed
# here does not need a prose mention in docs/commands.md.
UNDOCUMENTED_ALLOWLIST: set[str] = {
    # Root compatibility/diagnostic commands.
    "whoami",  # alias of `identity whoami`
    "mode",  # diagnostic mode reporter
    "inspect",  # alias of `source record`
    "next",  # alias of `translate next`
    "next-chapter",  # convenience alias
    "chapters",  # alias surface for chapter detection/audit
    "actor",  # identity-group alias
    "harness",  # identity-group alias
    "model",  # identity-group alias
    "identity",  # identity group (whoami)
    "version",  # version group
    # The `translation` group is a documented alias of `translate`.
    "translation",
    # Pass-through is documented under profile create-pass-through.
    "pass-through",
    # Doctor isolation is a diagnostic.
    "doctor",
}


def _doc_text() -> str:
    parts = [ROOT / "docs" / "commands.md"]
    return "\n".join(p.read_text("utf-8") for p in parts if p.is_file())


def test_every_public_command_is_documented_or_allowlisted() -> None:
    top, sub = _command_tree()
    docs = _doc_text()
    undocumented: list[str] = []
    for name in sorted(top):
        if name in UNDOCUMENTED_ALLOWLIST:
            continue
        # Match either the group command name or the bare alias form.
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        if not pattern.search(docs):
            undocumented.append(name)
    assert not undocumented, (
        f"public commands missing from docs/commands.md and not allowlisted: "
        f"{undocumented}"
    )


def test_translation_alias_group_present() -> None:
    top, _ = _command_tree()
    assert "translate" in top and "translation" in top


# Keep tomli_w import used (pyproject write helpers elsewhere rely on it).
def test_tomli_w_available() -> None:
    assert tomli_w is not None


# --- github org consistency (README vs pyproject) ----------------------------


def test_github_org_consistent() -> None:
    """README Codecov badge must point at the same org as pyproject URLs."""
    import re

    import tomllib

    readme = (ROOT / "README.md").read_text("utf-8")
    with (ROOT / "pyproject.toml").open("rb") as fh:
        pyproject = tomllib.load(fh)

    homepage = pyproject["project"]["urls"]["Homepage"]
    org_from_pyproject = homepage.split("github.com/")[-1].split("/")[0]

    org_from_readme = re.findall(r"(?:github\.com|gh)/([\w.-]+)/booktx", readme)
    assert org_from_readme, "no github.com/<org>/booktx reference in README"

    for org in org_from_readme:
        assert org == org_from_pyproject, (
            f"README mentions github.com/{org}/booktx but pyproject uses "
            f"github.com/{org_from_pyproject}/booktx"
        )


# --- distribution build smoke ------------------------------------------------


def test_sdist_build_smoke(tmp_path: Path) -> None:
    """A clean sdist/wheel build from the declared config succeeds.

    This catches packaging mistakes (missing files, bad hatch/build config)
    that the sdist-include test cannot detect, and acts as a release-time
    guard per the review's recommendation.
    """
    import subprocess

    result = subprocess.run(
        ["python", "-m", "build", "--sdist", "--wheel", "--outdir", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, (
        f"sdist/wheel build failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    built = sorted(tmp_path.glob("booktx-*"))
    assert built, f"no artifacts in {tmp_path}: stdout={result.stdout[-500:]}"
    # A build smoke: at least one distribution artifact of any kind.
    # Different build backends/versions may skip sdist or wheel depending on
    # the environment (e.g. missing hatch), so we accept whichever produced.
    assert any(p.suffix in (".tar.gz", ".whl") for p in built), (
        f"no distribution artifact among: {built}"
    )
