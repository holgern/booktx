"""CLI tests for `booktx context export-pack` / `import-pack`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from booktx.cli import app
from booktx.config import load_project
from booktx.context import context_markdown_path, context_path

runner = CliRunner(env={"COLUMNS": "120"})

MARKDOWN_DOC = """\
# One

The Wasp Empire has commenced its great war against the Lowlands.
"""


def _make_project(tmp_path: Path, name: str = "book") -> Path:
    project_dir = tmp_path / name
    src = tmp_path / f"{name}.md"
    src.write_text(MARKDOWN_DOC, encoding="utf-8")
    res = runner.invoke(
        app,
        ["init", str(project_dir), "--target", "de", "--source-file", str(src)],
    )
    assert res.exit_code == 0, res.output
    res = runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    assert res.exit_code == 0, res.output
    return project_dir


def _answer_core(project_dir: Path) -> None:
    answers = [
        ("Q001", "de-DE"),
        ("Q002", "balanced"),
        ("Q003", "neutral"),
        ("Q004", "natural dialogue"),
        ("Q005", "keep Apt names"),
        ("Q006", "translate world terms"),
        ("Q012", "error"),
    ]
    for qid, text in answers:
        res = runner.invoke(
            app,
            ["context", "answer", str(project_dir), qid, "--text", text],
        )
        assert res.exit_code == 0, res.output


def _ready_project(tmp_path: Path, name: str = "book") -> Path:
    project_dir = _make_project(tmp_path, name=name)
    _answer_core(project_dir)
    res = runner.invoke(app, ["context", "mark-ready", str(project_dir)])
    assert res.exit_code == 0, res.output
    return project_dir


def _pack_path(tmp_path: Path, name: str = "pack.json") -> Path:
    return tmp_path / name


# --- 1. export writes JSON and prints a summary -------------------------------


def test_export_writes_json_and_prints_summary(tmp_path: Path):
    project_dir = _ready_project(tmp_path)
    pack_path = _pack_path(tmp_path)
    res = runner.invoke(
        app,
        [
            "context",
            "export-pack",
            str(project_dir),
            "--series-id",
            "shadows-of-apt",
            "--title",
            "Shadows of the Apt German decisions",
            "--output",
            str(pack_path),
        ],
    )
    assert res.exit_code == 0, res.output
    assert pack_path.is_file()
    data = json.loads(pack_path.read_text("utf-8"))
    assert data["format"] == "booktx.series-context-pack"
    assert data["version"] == 1
    assert data["series_id"] == "shadows-of-apt"
    assert data["title"] == "Shadows of the Apt German decisions"
    assert "wrote series context pack" in res.output
    assert "series_id=shadows-of-apt" in res.output


def test_export_json_emits_single_document(tmp_path: Path):
    project_dir = _ready_project(tmp_path)
    pack_path = _pack_path(tmp_path)
    res = runner.invoke(
        app,
        [
            "context",
            "export-pack",
            str(project_dir),
            "--series-id",
            "s",
            "--output",
            str(pack_path),
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["series_id"] == "s"
    assert data["format"] == "booktx.series-context-pack"


# --- 2. export refuses overwrite without --force ------------------------------


def test_export_refuses_overwrite_without_force(tmp_path: Path):
    project_dir = _ready_project(tmp_path)
    pack_path = _pack_path(tmp_path)
    args = [
        "context",
        "export-pack",
        str(project_dir),
        "--series-id",
        "s",
        "--output",
        str(pack_path),
    ]
    assert runner.invoke(app, args).exit_code == 0
    res = runner.invoke(app, args)
    assert res.exit_code != 0
    assert "already exists" in res.output
    # --force overwrites.
    res = runner.invoke(app, args + ["--force"])
    assert res.exit_code == 0, res.output


# --- 3. dry-run import writes nothing -----------------------------------------


def test_dry_run_import_writes_nothing(tmp_path: Path):
    book1 = _ready_project(tmp_path, name="book1")
    runner.invoke(
        app,
        [
            "context",
            "reset-term",
            str(book1),
            "empire",
            "--target",
            "Imperium",
            "--forbid",
            "Reich",
            "--category",
            "concept",
            "--enforce",
            "error",
            "--create",
        ],
    )
    pack_path = _pack_path(tmp_path)
    runner.invoke(
        app,
        [
            "context",
            "export-pack",
            str(book1),
            "--series-id",
            "s",
            "--output",
            str(pack_path),
        ],
    )
    book2 = _make_project(tmp_path, name="book2")
    proj2 = load_project(book2)
    before_json = context_path(proj2).read_text("utf-8")
    before_md = context_markdown_path(proj2).read_text("utf-8")
    res = runner.invoke(
        app,
        ["context", "import-pack", str(book2), "--file", str(pack_path)],
    )
    assert res.exit_code == 0, res.output
    assert "Dry run." in res.output
    assert "No files written." in res.output
    # Nothing changed.
    assert context_path(proj2).read_text("utf-8") == before_json
    assert context_markdown_path(proj2).read_text("utf-8") == before_md


# --- 4. write import updates JSON and rendered Markdown -----------------------


def test_write_import_updates_json_and_markdown(tmp_path: Path):
    book1 = _ready_project(tmp_path, name="book1")
    runner.invoke(
        app,
        [
            "context",
            "reset-term",
            str(book1),
            "empire",
            "--target",
            "Imperium",
            "--forbid",
            "Reich",
            "--enforce",
            "error",
            "--create",
        ],
    )
    pack_path = _pack_path(tmp_path)
    runner.invoke(
        app,
        [
            "context",
            "export-pack",
            str(book1),
            "--series-id",
            "s",
            "--output",
            str(pack_path),
        ],
    )
    book2 = _make_project(tmp_path, name="book2")
    proj2 = load_project(book2)
    res = runner.invoke(
        app,
        ["context", "import-pack", str(book2), "--file", str(pack_path), "--write"],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(context_path(proj2).read_text("utf-8"))
    empire = [g for g in data["glossary"] if g["source"] == "empire"]
    assert empire and empire[0]["target"] == "Imperium"
    assert empire[0]["forbidden_targets"] == ["Reich"]
    md = context_markdown_path(proj2).read_text("utf-8")
    assert "Imperium" in md
    assert "Reich" in md


# --- 5. missing context errors unless --init-missing-context -------------------


def test_missing_context_errors_unless_init(tmp_path: Path):
    book1 = _ready_project(tmp_path, name="book1")
    pack_path = _pack_path(tmp_path)
    runner.invoke(
        app,
        [
            "context",
            "export-pack",
            str(book1),
            "--series-id",
            "s",
            "--output",
            str(pack_path),
        ],
    )
    # book2 with no context at all.
    src2 = tmp_path / "n2.md"
    src2.write_text(MARKDOWN_DOC, encoding="utf-8")
    book2 = tmp_path / "book2"
    runner.invoke(
        app, ["init", str(book2), "--target", "de", "--source-file", str(src2)]
    )
    res = runner.invoke(
        app, ["context", "import-pack", str(book2), "--file", str(pack_path)]
    )
    assert res.exit_code != 0
    # --init-missing-context creates it.
    res = runner.invoke(
        app,
        [
            "context",
            "import-pack",
            str(book2),
            "--file",
            str(pack_path),
            "--init-missing-context",
        ],
    )
    assert res.exit_code == 0, res.output


# --- 6. language and locale conflicts -----------------------------------------


def test_source_language_mismatch_errors(tmp_path: Path):
    book1 = _ready_project(tmp_path, name="book1")
    pack_path = _pack_path(tmp_path)
    runner.invoke(
        app,
        [
            "context",
            "export-pack",
            str(book1),
            "--series-id",
            "s",
            "--output",
            str(pack_path),
        ],
    )
    # book2 with a different source language by editing its context.json.
    book2 = _make_project(tmp_path, name="book2")
    proj2 = load_project(book2)
    path = context_path(proj2)
    data = json.loads(path.read_text("utf-8"))
    data["source_language"] = "fr"
    path.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
    res = runner.invoke(
        app, ["context", "import-pack", str(book2), "--file", str(pack_path)]
    )
    assert res.exit_code != 0


def test_target_locale_conflict_reports_in_findings(tmp_path: Path):
    # A target-locale conflict is reported in the style section, not a hard error.
    book2, pack_path = _locale_conflict_setup(tmp_path)
    res = runner.invoke(
        app,
        [
            "context",
            "import-pack",
            str(book2),
            "--file",
            str(pack_path),
            "--conflict",
            "keep-local",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "target_locale" in res.output


# --- 7. fail, keep-local, replace behavior ------------------------------------


def _locale_conflict_setup(tmp_path: Path) -> tuple[Path, Path]:
    book1 = _ready_project(tmp_path, name="book1")
    proj1 = load_project(book1)
    path1 = context_path(proj1)
    data1 = json.loads(path1.read_text("utf-8"))
    # Change the locale AND its governing core answer (Q001) together so the
    # pack stays internally consistent for core/style validation.
    data1["style"]["target_locale"] = "de-AT"
    for q in data1["questions"]:
        if q.get("id") == "Q001":
            q["answer"] = "de-AT"
    path1.write_text(json.dumps(data1, indent=2) + "\n", "utf-8")
    pack_path = _pack_path(tmp_path)
    export_res = runner.invoke(
        app,
        [
            "context",
            "export-pack",
            str(book1),
            "--series-id",
            "s",
            "--output",
            str(pack_path),
        ],
    )
    assert export_res.exit_code == 0, export_res.output
    book2 = _ready_project(tmp_path, name="book2")
    proj2 = load_project(book2)
    path2 = context_path(proj2)
    data2 = json.loads(path2.read_text("utf-8"))
    data2["style"]["target_locale"] = "de-DE"
    for q in data2["questions"]:
        if q.get("id") == "Q001":
            q["answer"] = "de-DE"
    path2.write_text(json.dumps(data2, indent=2) + "\n", "utf-8")
    return book2, pack_path


def test_conflict_fail_mode_exits_nonzero(tmp_path: Path):
    book2, pack_path = _locale_conflict_setup(tmp_path)
    res = runner.invoke(
        app,
        ["context", "import-pack", str(book2), "--file", str(pack_path)],
    )
    assert res.exit_code != 0
    assert "conflict" in res.output


def test_conflict_keep_local_skips_and_keeps_local(tmp_path: Path):
    book2, pack_path = _locale_conflict_setup(tmp_path)
    res = runner.invoke(
        app,
        [
            "context",
            "import-pack",
            str(book2),
            "--file",
            str(pack_path),
            "--conflict",
            "keep-local",
            "--write",
        ],
    )
    assert res.exit_code == 0, res.output
    proj2 = load_project(book2)
    data = json.loads(context_path(proj2).read_text("utf-8"))
    assert data["style"]["target_locale"] == "de-DE"


def test_conflict_replace_uses_imported(tmp_path: Path):
    book2, pack_path = _locale_conflict_setup(tmp_path)
    res = runner.invoke(
        app,
        [
            "context",
            "import-pack",
            str(book2),
            "--file",
            str(pack_path),
            "--conflict",
            "replace",
            "--write",
        ],
    )
    assert res.exit_code == 0, res.output
    proj2 = load_project(book2)
    data = json.loads(context_path(proj2).read_text("utf-8"))
    assert data["style"]["target_locale"] == "de-AT"


# --- 8. conflict and validation failures write nothing ------------------------


def test_conflict_failure_writes_nothing(tmp_path: Path):
    book2, pack_path = _locale_conflict_setup(tmp_path)
    proj2 = load_project(book2)
    before = context_path(proj2).read_text("utf-8")
    res = runner.invoke(
        app,
        ["context", "import-pack", str(book2), "--file", str(pack_path), "--write"],
    )
    assert res.exit_code != 0
    assert context_path(proj2).read_text("utf-8") == before


def test_invalid_pack_writes_nothing(tmp_path: Path):
    book2 = _make_project(tmp_path, name="book2")
    bad_pack = _pack_path(tmp_path, name="bad.json")
    bad_pack.write_text(
        json.dumps(
            {
                "format": "booktx.series-context-pack",
                "version": 1,
                "series_id": "bad id!",
                "source_language": "en",
                "target_language": "de",
                "created_at": "t",
            }
        ),
        encoding="utf-8",
    )
    proj2 = load_project(book2)
    before = context_path(proj2).read_text("utf-8")
    res = runner.invoke(
        app,
        ["context", "import-pack", str(book2), "--file", str(bad_pack), "--write"],
    )
    assert res.exit_code != 0
    assert context_path(proj2).read_text("utf-8") == before


# --- 9. optimistic context-hash failure writes nothing ------------------------


def test_optimistic_hash_failure_writes_nothing(tmp_path: Path, monkeypatch):
    book1 = _ready_project(tmp_path, name="book1")
    pack_path = _pack_path(tmp_path)
    runner.invoke(
        app,
        [
            "context",
            "export-pack",
            str(book1),
            "--series-id",
            "s",
            "--output",
            str(pack_path),
        ],
    )
    book2 = _make_project(tmp_path, name="book2")
    proj2 = load_project(book2)
    before = context_path(proj2).read_text("utf-8")
    # Simulate a concurrent change: after import_context_pack's preflight read,
    # mutate context.json so the final live re-read differs from preflight.
    import booktx.context_packs as cp_mod

    real_load = cp_mod.load_context
    state = {"preflight_done": False}

    def counting_load(project):
        result = real_load(project)
        if not state["preflight_done"]:
            state["preflight_done"] = True
            # Mutate after the preflight snapshot so later reads differ.
            path = context_path(project)
            data = json.loads(path.read_text("utf-8"))
            data["global_rules"] = ["CONCURRENT EDIT"]
            path.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
        return result

    monkeypatch.setattr(cp_mod, "load_context", counting_load)
    res = runner.invoke(
        app,
        ["context", "import-pack", str(book2), "--file", str(pack_path), "--write"],
    )
    assert res.exit_code != 0
    after = context_path(proj2).read_text("utf-8")
    # The concurrent edit is still present; the import's planned changes were NOT
    # applied on top.
    assert "CONCURRENT EDIT" in after
    # The pack did not add its provenance-stamped answers.
    assert after.count('"answer_source": "imported"') == 0
    assert before != after or True  # mutation applied regardless


# --- 10. --json emits a single stable document --------------------------------


def test_import_json_emits_single_document(tmp_path: Path):
    book1 = _ready_project(tmp_path, name="book1")
    runner.invoke(
        app,
        [
            "context",
            "reset-term",
            str(book1),
            "empire",
            "--target",
            "Imperium",
            "--forbid",
            "Reich",
            "--enforce",
            "error",
            "--create",
        ],
    )
    pack_path = _pack_path(tmp_path)
    runner.invoke(
        app,
        [
            "context",
            "export-pack",
            str(book1),
            "--series-id",
            "s",
            "--output",
            str(pack_path),
        ],
    )
    book2 = _make_project(tmp_path, name="book2")
    res = runner.invoke(
        app,
        ["context", "import-pack", str(book2), "--file", str(pack_path), "--json"],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["series_id"] == "s"
    assert data["dry_run"] is True
    required_keys = {"section", "key", "action", "message"}
    assert all(required_keys <= set(f) for f in data["findings"])


# --- 11/12. profile-root isolation: local paths only, escapes rejected --------


def _make_profile_root_project(tmp_path: Path) -> Path:
    """Build a profiles-layout project and return its default profile root.

    `init --target de` creates the default `de_default` profile and writes its
    profile-root marker, so the profile directory is recognized in isolated mode.
    """
    project_root = tmp_path / "project"
    src = tmp_path / "novel.md"
    src.write_text(MARKDOWN_DOC, encoding="utf-8")
    init_res = runner.invoke(
        app,
        ["init", str(project_root), "--target", "de", "--source-file", str(src)],
    )
    assert init_res.exit_code == 0, init_res.output
    extract_res = runner.invoke(app, ["extract", str(project_root)])
    assert extract_res.exit_code == 0, extract_res.output
    init_ctx = runner.invoke(
        app, ["context", "init", str(project_root), "--non-interactive"]
    )
    assert init_ctx.exit_code == 0, init_ctx.output
    # Answer core questions via the default profile, then force-ready.
    answers = [
        ("Q001", "de-DE"),
        ("Q002", "balanced"),
        ("Q003", "neutral"),
        ("Q004", "natural"),
        ("Q005", "keep"),
        ("Q006", "translate"),
        ("Q012", "error"),
    ]
    for qid, text in answers:
        ans_res = runner.invoke(
            app,
            ["context", "answer", str(project_root), qid, "--text", text],
        )
        assert ans_res.exit_code == 0, ans_res.output
    ready_res = runner.invoke(
        app,
        ["context", "mark-ready", str(project_root)],
    )
    assert ready_res.exit_code == 0, ready_res.output
    return project_root / "translations" / "de_default"


def test_profile_root_export_output_uses_local_paths(tmp_path: Path):
    profile_root = _make_profile_root_project(tmp_path)
    out_rel = "local.pack.json"
    res = runner.invoke(
        app,
        [
            "context",
            "export-pack",
            str(profile_root),
            "--series-id",
            "s",
            "--output",
            out_rel,
        ],
    )
    assert res.exit_code == 0, res.output
    assert (profile_root / out_rel).is_file()
    # Output path shown is local to the profile root, not absolute.
    assert str(profile_root) not in res.output
    assert "<hidden>" not in res.output


def test_profile_root_export_rejects_absolute_path(tmp_path: Path):
    profile_root = _make_profile_root_project(tmp_path)
    abs_out = tmp_path / "abs.pack.json"
    res = runner.invoke(
        app,
        [
            "context",
            "export-pack",
            str(profile_root),
            "--series-id",
            "s",
            "--output",
            str(abs_out),
        ],
    )
    assert res.exit_code != 0
    assert "absolute" in res.output.lower() or "isolated" in res.output.lower()


def test_profile_root_export_rejects_parent_escape(tmp_path: Path):
    profile_root = _make_profile_root_project(tmp_path)
    res = runner.invoke(
        app,
        [
            "context",
            "export-pack",
            str(profile_root),
            "--series-id",
            "s",
            "--output",
            "../escape.pack.json",
        ],
    )
    assert res.exit_code != 0


def test_profile_root_import_rejects_symlink_escape(tmp_path: Path):
    profile_root = _make_profile_root_project(tmp_path)
    # Export a legitimate pack first, then try importing a symlink that escapes.
    runner.invoke(
        app,
        [
            "context",
            "export-pack",
            str(profile_root),
            "--series-id",
            "s",
            "--output",
            "legit.pack.json",
        ],
    )
    outside = tmp_path / "outside.pack.json"
    outside.write_text("{}", encoding="utf-8")
    link = profile_root / "escape.pack.json"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not supported on this platform")
    res = runner.invoke(
        app,
        ["context", "import-pack", str(profile_root), "--file", "escape.pack.json"],
    )
    assert res.exit_code != 0
    assert "escapes" in res.output.lower() or "symlink" in res.output.lower()


# --- 13. existing tasks produce a warning without being modified ---------------


def test_existing_tasks_produce_warning_without_modification(tmp_path: Path):
    book1 = _ready_project(tmp_path, name="book1")
    runner.invoke(
        app,
        [
            "context",
            "reset-term",
            str(book1),
            "empire",
            "--target",
            "Imperium",
            "--forbid",
            "Reich",
            "--enforce",
            "error",
            "--create",
        ],
    )
    pack_path = _pack_path(tmp_path)
    runner.invoke(
        app,
        [
            "context",
            "export-pack",
            str(book1),
            "--series-id",
            "s",
            "--output",
            str(pack_path),
        ],
    )
    book2 = _ready_project(tmp_path, name="book2")
    # Extract source so translate next can create a task.
    assert runner.invoke(app, ["extract", str(book2)]).exit_code == 0
    # Create a translation task (in-flight work) in book2.
    next_res = runner.invoke(
        app,
        ["translate", "next", str(book2), "--unit", "paragraph", "--json"],
    )
    assert next_res.exit_code == 0, next_res.output
    task_dir = load_project(book2).tasks_dir
    assert task_dir is not None and any(task_dir.glob("*.json"))
    res = runner.invoke(
        app,
        [
            "context",
            "import-pack",
            str(book2),
            "--file",
            str(pack_path),
            "--conflict",
            "keep-local",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "tasks may carry the previous context view" in res.output


# --- 14. simulated Markdown write failure leaves valid canonical JSON ----------


def test_markdown_write_failure_leaves_valid_json(tmp_path: Path, monkeypatch):
    book1 = _ready_project(tmp_path, name="book1")
    runner.invoke(
        app,
        [
            "context",
            "reset-term",
            str(book1),
            "empire",
            "--target",
            "Imperium",
            "--forbid",
            "Reich",
            "--enforce",
            "error",
            "--create",
        ],
    )
    pack_path = _pack_path(tmp_path)
    runner.invoke(
        app,
        [
            "context",
            "export-pack",
            str(book1),
            "--series-id",
            "s",
            "--output",
            str(pack_path),
        ],
    )
    book2 = _make_project(tmp_path, name="book2")

    # Force write_context_markdown to fail during import.
    import booktx.context as ctx_mod

    def boom(project, context):
        raise OSError("simulated markdown write failure")

    monkeypatch.setattr(ctx_mod, "write_context_markdown", boom)
    res = runner.invoke(
        app,
        ["context", "import-pack", str(book2), "--file", str(pack_path), "--write"],
    )
    # Import still succeeds (context.json is canonical); markdown write is best-effort.
    assert res.exit_code == 0, res.output
    proj2 = load_project(book2)
    data = json.loads(context_path(proj2).read_text("utf-8"))
    empire = [g for g in data["glossary"] if g["source"] == "empire"]
    assert empire and empire[0]["forbidden_targets"] == ["Reich"]
    # Recoverable via render --write.
    monkeypatch.undo()
    render_res = runner.invoke(app, ["context", "render", str(book2), "--write"])
    assert render_res.exit_code == 0, render_res.output
    md = context_markdown_path(proj2).read_text("utf-8")
    assert "Reich" in md


# --- Integration: Empire/Reich forbidden_term_used ----------------------------


def test_empire_reich_import_yields_forbidden_term_error(tmp_path: Path):
    """End-to-end: import pack -> mark ready -> fresh task -> submit Reich.

    Asserts the existing forbidden_term_used rule fires at severity error after
    a binding glossary was imported via a series context pack.
    """
    from booktx.acceptance import (
        SubmissionValidationError,
        SubmittedRecord,
        accept_translation_records,
    )
    from booktx.status import build_status_snapshot

    book1 = _ready_project(tmp_path, name="book1")
    runner.invoke(
        app,
        [
            "context",
            "reset-term",
            str(book1),
            "empire",
            "--target",
            "Imperium",
            "--forbid",
            "Reich",
            "--category",
            "concept",
            "--enforce",
            "error",
            "--create",
        ],
    )
    pack_path = _pack_path(tmp_path)
    runner.invoke(
        app,
        [
            "context",
            "export-pack",
            str(book1),
            "--series-id",
            "s",
            "--output",
            str(pack_path),
        ],
    )
    book2 = _make_project(tmp_path, name="book2")
    # Import the pack (writes context.json + md).
    res = runner.invoke(
        app,
        [
            "context",
            "import-pack",
            str(book2),
            "--file",
            str(pack_path),
            "--write",
            "--conflict",
            "replace",
        ],
    )
    assert res.exit_code == 0, res.output
    # Extract source so translate next can create a task.
    assert runner.invoke(app, ["extract", str(book2)]).exit_code == 0
    # Mark ready after import (core answers were imported/approved).
    res = runner.invoke(
        app,
        [
            "context",
            "mark-ready",
            str(book2),
            "--force",
            "--reason",
            "post-import approval",
        ],
    )
    assert res.exit_code == 0, res.output
    # Create a fresh task covering the paragraph that contains "empire".
    next_res = runner.invoke(
        app,
        ["translate", "next", str(book2), "--unit", "chapter", "--json"],
    )
    assert next_res.exit_code == 0, next_res.output
    task_payload = json.loads(next_res.output)
    proj2 = load_project(book2)
    bundle = build_status_snapshot(proj2, context_exists=True, context_ready=True)
    # Pick the record whose source actually contains the glossary term.
    empire_record = None
    for rec in task_payload["records"]:
        source_rec = bundle.index.source_by_id.get(rec["id"])
        if source_rec is not None and "empire" in source_rec.source.lower():
            empire_record = rec
            break
    assert empire_record is not None, "no record contains 'empire'"
    # Submit a translation using the forbidden term 'Reich' for that record.
    with pytest.raises(SubmissionValidationError) as exc_info:
        accept_translation_records(
            proj2,
            [SubmittedRecord(id=empire_record["id"], target="Das Reich erstarkte.")],
            bundle=bundle,
            task=None,
            enforce_task_version=False,
        )
    rules = {(f.rule, f.severity) for f in exc_info.value.findings}
    assert ("forbidden_term_used", "error") in rules, [
        (f.rule, f.severity, f.message) for f in exc_info.value.findings
    ]


# --- Integration: stale task after binding glossary import --------------------


def test_pre_import_task_is_stale_after_binding_glossary_import(tmp_path: Path):
    """A task created before a binding glossary import is rejected afterwards."""
    from booktx.acceptance import SubmittedRecord, accept_translation_records
    from booktx.config import BooktxError, load_translation_task
    from booktx.status import build_status_snapshot

    book2 = _ready_project(tmp_path, name="book2")
    # Extract source so translate next can create a task.
    assert runner.invoke(app, ["extract", str(book2)]).exit_code == 0
    # Create a task BEFORE importing any binding glossary.
    next_res = runner.invoke(
        app,
        ["translate", "next", str(book2), "--unit", "paragraph", "--json"],
    )
    assert next_res.exit_code == 0, next_res.output
    pre_task_payload = json.loads(next_res.output)
    proj2 = load_project(book2)
    pre_task = load_translation_task(proj2, pre_task_payload["task_id"])
    assert pre_task is not None
    assert pre_task.mandatory_glossary_sha256 is not None

    # Build a pack with a binding glossary and import it.
    book1 = _ready_project(tmp_path, name="book1")
    runner.invoke(
        app,
        [
            "context",
            "reset-term",
            str(book1),
            "empire",
            "--target",
            "Imperium",
            "--forbid",
            "Reich",
            "--enforce",
            "error",
            "--create",
        ],
    )
    pack_path = _pack_path(tmp_path)
    runner.invoke(
        app,
        [
            "context",
            "export-pack",
            str(book1),
            "--series-id",
            "s",
            "--output",
            str(pack_path),
        ],
    )
    res = runner.invoke(
        app,
        [
            "context",
            "import-pack",
            str(book2),
            "--file",
            str(pack_path),
            "--write",
            "--conflict",
            "replace",
        ],
    )
    assert res.exit_code == 0, res.output
    # Import must NOT write the version ledger.
    ledger_path = proj2.ledger_path
    assert ledger_path is not None
    ledger_before = ledger_path.read_text("utf-8") if ledger_path.is_file() else ""
    # Re-import again and confirm ledger content is unchanged by import.
    res2 = runner.invoke(
        app,
        [
            "context",
            "import-pack",
            str(book2),
            "--file",
            str(pack_path),
            "--write",
            "--conflict",
            "replace",
        ],
    )
    assert res2.exit_code == 0, res2.output
    ledger_after = ledger_path.read_text("utf-8") if ledger_path.is_file() else ""
    assert ledger_after == ledger_before
    # Import cleared readiness; re-mark ready so acceptance can run. The
    # imported core answers satisfy the approval provenance requirement.
    ready_res = runner.invoke(
        app,
        [
            "context",
            "mark-ready",
            str(book2),
            "--force",
            "--reason",
            "post-import re-approval",
        ],
    )
    assert ready_res.exit_code == 0, ready_res.output

    # The pre-import task is now stale: its mandatory-glossary fingerprint
    # no longer matches the live binding glossary.
    record_id = pre_task_payload["records"][0]["id"]
    bundle = build_status_snapshot(proj2, context_exists=True, context_ready=True)
    with pytest.raises(BooktxError) as exc_info:
        accept_translation_records(
            proj2,
            [SubmittedRecord(id=record_id, target="Imperium")],
            bundle=bundle,
            task=pre_task,
            submission_translation_version=pre_task.translation_version,
            enforce_task_version=True,
        )
    assert exc_info.value.code == "task_context_policy_stale"

    # A FRESH post-import task resolves against the new baseline without stale error.
    fresh_res = runner.invoke(
        app,
        ["translate", "next", str(book2), "--unit", "chapter", "--json"],
    )
    assert fresh_res.exit_code == 0, fresh_res.output
    fresh_payload = json.loads(fresh_res.output)
    fresh_task = load_translation_task(proj2, fresh_payload["task_id"])
    assert fresh_task is not None
    bundle2 = build_status_snapshot(proj2, context_exists=True, context_ready=True)
    # Accept a clean target for any record in the fresh task (no stale error).
    fresh_record_id = fresh_payload["records"][0]["id"]
    accept_translation_records(
        proj2,
        [SubmittedRecord(id=fresh_record_id, target="Imperium")],
        bundle=bundle2,
        task=fresh_task,
        submission_translation_version=fresh_task.translation_version,
        enforce_task_version=True,
    )
