"""Direct unit tests for the booktx.acceptance service.

Exercises the shared validate-and-persist flow without going through Typer,
and pins the behavior that batch and single-record acceptance share one
implementation: context is loaded once, ERROR findings block the store write,
and unknown/duplicate/out-of-task ids raise BooktxError.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from booktx.acceptance import (
    AcceptResult,
    SubmissionValidationError,
    SubmittedRecord,
    accept_one_record,
    accept_translation_records,
)
from booktx.cli import app
from booktx.config import (
    BooktxError,
    load_project,
    load_translation_task,
    translation_store_path,
    write_identity,
)
from booktx.context import GlossaryEntry, default_context, load_context, write_context
from booktx.models import TranslationIdentity
from booktx.status import build_status_snapshot

runner = CliRunner()

DOC = """\
# Chapter One

Alice met Bob. They were happy. Bob waved.
"""


def _make_project(tmp_path: Path) -> Path:
    src = tmp_path / "book.md"
    src.write_text(DOC, encoding="utf-8")
    project_dir = tmp_path / "book"
    res = runner.invoke(
        app,
        [
            "init",
            str(project_dir),
            "--target",
            "de",
            "--source-file",
            str(src),
            "--chunk-size",
            "5",
        ],
    )
    assert res.exit_code == 0, res.output
    assert runner.invoke(app, ["extract", str(project_dir)]).exit_code == 0
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    runner.invoke(
        app,
        [
            "context",
            "mark-ready",
            str(project_dir),
            "--force",
            "--reason",
            "test setup",
        ],
    )
    return project_dir


def _first_record_id(project_dir: Path) -> str:
    chunks = sorted((project_dir / ".booktx" / "chunks").glob("*.json"))
    chunk = json.loads(chunks[0].read_text("utf-8"))
    return chunk["records"][0]["id"]


def _record_ids(project_dir: Path) -> list[str]:
    chunks = sorted((project_dir / ".booktx" / "chunks").glob("*.json"))
    chunk = json.loads(chunks[0].read_text("utf-8"))
    return [record["id"] for record in chunk["records"]]


def _make_glossary_project(tmp_path: Path) -> Path:
    src = tmp_path / "glossary.md"
    src.write_text("# Lowlands\n\nFar away.\n", encoding="utf-8")
    project_dir = tmp_path / "glossary-book"
    res = runner.invoke(
        app,
        [
            "init",
            str(project_dir),
            "--target",
            "de",
            "--source-file",
            str(src),
            "--chunk-size",
            "5",
        ],
    )
    assert res.exit_code == 0, res.output
    assert runner.invoke(app, ["extract", str(project_dir)]).exit_code == 0
    proj = load_project(project_dir)
    ctx = default_context(proj)
    ctx.ready = True
    ctx.ready_forced = True
    ctx.glossary.append(
        GlossaryEntry(
            source="Lowlands",
            forbidden_targets=["Niederlande"],
            enforce="error",
        )
    )
    write_context(proj, ctx)
    return project_dir


def test_accept_one_record_persists_and_reports_chapter(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    proj = load_project(project_dir)
    rid = _first_record_id(project_dir)
    bundle = build_status_snapshot(proj, context_exists=True, context_ready=True)

    result = accept_one_record(proj, rid, "Alice traf Bob.", bundle=bundle)

    assert isinstance(result, AcceptResult)
    assert result.accepted_records == 1
    assert result.target_words >= 1
    assert result.version_ref == "1.1"
    assert result.chapter_id  # mapped to a chapter

    store = json.loads(translation_store_path(proj).read_text("utf-8"))
    assert store["records"][rid]["active_version"] == "1.1"
    assert store["records"][rid]["versions"][0]["target"] == "Alice traf Bob."


def test_batch_and_single_record_share_implementation(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    proj = load_project(project_dir)
    rid = _first_record_id(project_dir)
    bundle = build_status_snapshot(proj, context_exists=True, context_ready=True)

    batch = accept_translation_records(
        proj, [SubmittedRecord(id=rid, target="Alice traf Bob.")], bundle=bundle
    )
    # target_words count must match the single-record path for the same text.
    single = accept_one_record(proj, rid, "Alice traf Bob.", bundle=bundle)
    assert batch.target_words == single.target_words


def test_same_version_reaccept_updates_existing_candidate_and_preserves_created_at(
    tmp_path: Path,
):
    project_dir = _make_project(tmp_path)
    proj = load_project(project_dir)
    write_identity(
        proj,
        TranslationIdentity(
            actor="user:nahrstaedt",
            harness="pi",
            model="codex-openai/gpt-5.5@low",
        ),
    )
    rid = _first_record_id(project_dir)
    bundle = build_status_snapshot(proj, context_exists=True, context_ready=True)

    first = accept_one_record(proj, rid, "Alice traf Bob.", bundle=bundle)
    before = json.loads(translation_store_path(proj).read_text("utf-8"))
    second = accept_one_record(proj, rid, "Alice begegnete Bob.", bundle=bundle)
    after = json.loads(translation_store_path(proj).read_text("utf-8"))
    before_version = before["records"][rid]["versions"][0]
    after_version = after["records"][rid]["versions"][0]

    assert first.version_ref == "1.1"
    assert second.version_ref == "1.1"
    assert len(after["records"][rid]["versions"]) == 1
    assert after_version["target"] == "Alice begegnete Bob."
    assert after_version["created_at"] == before_version["created_at"]


def test_changed_context_creates_next_subversion_without_auto_switching_active(
    tmp_path: Path,
):
    project_dir = _make_project(tmp_path)
    proj = load_project(project_dir)
    write_identity(
        proj,
        TranslationIdentity(
            actor="user:nahrstaedt",
            harness="pi",
            model="codex-openai/gpt-5.5@low",
        ),
    )
    rid = _first_record_id(project_dir)
    bundle = build_status_snapshot(proj, context_exists=True, context_ready=True)

    first = accept_one_record(proj, rid, "Alice traf Bob.", bundle=bundle)
    ctx = load_context(proj)
    assert ctx is not None
    ctx.global_rules.append("Prefer shorter German clauses.")
    write_context(proj, ctx)
    second = accept_one_record(proj, rid, "Alice begegnete Bob.", bundle=bundle)
    store = json.loads(translation_store_path(proj).read_text("utf-8"))
    record = store["records"][rid]
    version_refs = [candidate["version_ref"] for candidate in record["versions"]]

    assert first.version_ref == "1.1"
    assert second.version_ref == "1.2"
    assert version_refs == ["1.1", "1.2"]
    assert record["active_version"] == "1.1"


def test_task_acceptance_uses_task_version_after_live_baseline_changes(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    next_res = runner.invoke(
        app,
        ["translate", "next", str(project_dir), "--unit", "paragraph", "--json"],
    )
    assert next_res.exit_code == 0, next_res.output
    task_payload = json.loads(next_res.output)

    proj = load_project(project_dir)
    task = load_translation_task(proj, task_payload["task_id"])
    assert task is not None
    bundle = build_status_snapshot(proj, context_exists=True, context_ready=True)

    ctx = load_context(proj)
    assert ctx is not None
    ctx.global_rules.append("Prefer shorter German clauses.")
    write_context(proj, ctx)

    result = accept_translation_records(
        proj,
        [
            SubmittedRecord(
                id=task.records[0].id,
                target="Alice traf Bob.",
            )
        ],
        bundle=bundle,
        task=task,
        submission_translation_version=task.translation_version,
        enforce_task_version=True,
    )

    store = json.loads(translation_store_path(proj).read_text("utf-8"))
    candidate = store["records"][task.records[0].id]["versions"][0]
    assert result.version_ref == task.translation_version
    assert candidate["version_ref"] == task.translation_version
    assert candidate["baseline_ref"] == task.baseline_ref
    assert candidate["baseline_sha256"] == task.baseline_sha256
    assert candidate["context_view_sha256"] == task.context_view_sha256
    assert candidate["context_view_path"] == task.context_view_path


def test_task_validation_uses_task_context_view_before_live_context(tmp_path: Path):
    project_dir = _make_glossary_project(tmp_path)
    next_res = runner.invoke(
        app,
        ["translate", "next", str(project_dir), "--unit", "paragraph", "--json"],
    )
    assert next_res.exit_code == 0, next_res.output
    task_payload = json.loads(next_res.output)

    proj = load_project(project_dir)
    task = load_translation_task(proj, task_payload["task_id"])
    assert task is not None
    bundle = build_status_snapshot(proj, context_exists=True, context_ready=True)

    ctx = load_context(proj)
    assert ctx is not None
    ctx.glossary[0].enforce = "off"
    ctx.glossary[0].forbidden_targets = []
    write_context(proj, ctx)

    try:
        accept_translation_records(
            proj,
            [SubmittedRecord(id=task.records[0].id, target="Die Niederlande")],
            bundle=bundle,
            task=task,
            submission_translation_version=task.translation_version,
            enforce_task_version=True,
        )
    except SubmissionValidationError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected submission validation failure from task view")


def test_legacy_task_without_context_view_uses_live_context_fallback(tmp_path: Path):
    project_dir = _make_glossary_project(tmp_path)
    next_res = runner.invoke(
        app,
        ["translate", "next", str(project_dir), "--unit", "paragraph", "--json"],
    )
    assert next_res.exit_code == 0, next_res.output
    task_payload = json.loads(next_res.output)

    proj = load_project(project_dir)
    task = load_translation_task(proj, task_payload["task_id"])
    assert task is not None
    bundle = build_status_snapshot(proj, context_exists=True, context_ready=True)

    ctx = load_context(proj)
    assert ctx is not None
    ctx.glossary[0].enforce = "off"
    ctx.glossary[0].forbidden_targets = []
    write_context(proj, ctx)

    legacy_task = task.model_copy(deep=True)
    legacy_task.context_view_path = None
    legacy_task.context_view_sha256 = None
    legacy_task.context_notes_scope = None
    legacy_task.context_target_chapter_id = None
    legacy_task.context_notes_through_chapter_id = None

    result = accept_translation_records(
        proj,
        [SubmittedRecord(id=task.records[0].id, target="Die Niederlande")],
        bundle=bundle,
        task=legacy_task,
        submission_translation_version=legacy_task.translation_version,
        enforce_task_version=True,
    )

    assert result.version_ref == legacy_task.translation_version


def test_changed_model_creates_next_major_track(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    proj = load_project(project_dir)
    write_identity(
        proj,
        TranslationIdentity(
            actor="user:nahrstaedt",
            harness="pi",
            model="codex-openai/gpt-5.5@low",
        ),
    )
    first_id, second_id = _record_ids(project_dir)[:2]
    bundle = build_status_snapshot(proj, context_exists=True, context_ready=True)

    first = accept_one_record(proj, first_id, "Alice traf Bob.", bundle=bundle)
    write_identity(
        proj,
        TranslationIdentity(
            actor="user:nahrstaedt",
            harness="pi",
            model="codex-openai/gpt-5.4-mini@low",
        ),
    )
    second = accept_one_record(proj, second_id, "Sie waren froh.", bundle=bundle)

    assert first.version_ref == "1.1"
    assert second.version_ref == "2.1"


def test_unknown_record_id_raises_booktx_error(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    proj = load_project(project_dir)
    bundle = build_status_snapshot(proj, context_exists=True, context_ready=True)

    try:
        accept_one_record(proj, "nope-r0001", "x", bundle=bundle)
    except BooktxError as exc:
        assert "unknown source record id" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected BooktxError for unknown record id")


def test_empty_target_raises_booktx_error(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    proj = load_project(project_dir)
    rid = _first_record_id(project_dir)
    bundle = build_status_snapshot(proj, context_exists=True, context_ready=True)

    try:
        accept_one_record(proj, rid, "   ", bundle=bundle)
    except BooktxError as exc:
        assert "empty target" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected BooktxError for empty target")


def test_duplicate_id_raises_before_store_write(tmp_path: Path):
    project_dir = _make_project(tmp_path)
    proj = load_project(project_dir)
    rid = _first_record_id(project_dir)
    bundle = build_status_snapshot(proj, context_exists=True, context_ready=True)

    try:
        accept_translation_records(
            proj,
            [SubmittedRecord(id=rid, target="x"), SubmittedRecord(id=rid, target="y")],
            bundle=bundle,
        )
    except BooktxError as exc:
        assert "duplicate record id" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected BooktxError for duplicate id")

    # Store must not have been written for the failed submission.
    store_path = translation_store_path(proj)
    if store_path.exists():
        store = json.loads(store_path.read_text("utf-8"))
        assert rid not in store.get("records", {})
