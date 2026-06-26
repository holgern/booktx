"""Tests for ``booktx translate export-index`` editor QA indexes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from booktx.cli import app
from booktx.config import (
    load_project,
    translation_source_index_path,
    translation_source_target_index_path,
    translation_target_index_path,
    write_translation_store,
    write_translation_version_ledger,
)
from booktx.models import (
    StoredTranslationRecordV2,
    TranslationCandidate,
    TranslationReviewCandidate,
    TranslationStoreV2,
    TranslationSubversionLedgerEntry,
    TranslationTrackLedgerEntry,
    TranslationVersionLedger,
)
from booktx.progress import load_source_chunks, source_record_sha256
from booktx.translation_store import sha256_text

runner = CliRunner()

TS = "2026-06-22T12:00:00Z"

DOC = "# Chapter One\n\nThe Wasp scout arrived. Untranslated source.\n"


# --- fixture helpers ---------------------------------------------------------


def _make_project(tmp_path: Path, doc: str = DOC, chunk_size: int = 10):
    src = tmp_path / "book.md"
    src.write_text(doc, encoding="utf-8")
    project_dir = tmp_path / "book"
    init_res = runner.invoke(
        app,
        [
            "init",
            str(project_dir),
            "--target",
            "de",
            "--source-file",
            str(src),
            "--chunk-size",
            str(chunk_size),
        ],
    )
    assert init_res.exit_code == 0, init_res.output
    ext = runner.invoke(app, ["extract", str(project_dir)])
    assert ext.exit_code == 0, ext.output
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
    proj = load_project(project_dir, profile="de_default")
    return proj, load_source_chunks(proj)


def _ledger(proj, version_ref: str = "1.1") -> None:
    major, minor = version_ref.split(".")
    write_translation_version_ledger(
        proj,
        TranslationVersionLedger(
            active_version=version_ref,
            tracks={
                major: TranslationTrackLedgerEntry(
                    version=int(major),
                    actor="user:test",
                    harness="pi",
                    model="human",
                    created_at=TS,
                    updated_at=TS,
                    subversions={
                        minor: TranslationSubversionLedgerEntry(
                            version=int(major),
                            subversion=int(minor),
                            version_ref=version_ref,
                            context_sha256="a" * 64,
                            created_at=TS,
                            updated_at=TS,
                        )
                    },
                )
            },
        ),
    )


def _source_text(chunks, record_id: str) -> str:
    for chunk in chunks:
        for record in chunk.records:
            if record.id == record_id:
                return record.source
    raise AssertionError(f"unknown record id {record_id!r}")


def _record(
    chunks,
    record_id: str,
    target: str,
    *,
    active_version: str | None = "1.1",
    active_review: str | None = None,
    reviews=None,
    base_target: str | None = None,
) -> StoredTranslationRecordV2:
    chunk = next(c for c in chunks for r in c.records if r.id == record_id)
    source = _source_text(chunks, record_id)
    return StoredTranslationRecordV2(
        chunk_id=int(chunk.chunk_id),
        part_id=int(record_id.split("-")[1]),
        source_sha256=source_record_sha256(source),
        source=source,
        active_version=active_version,
        active_review=active_review,
        versions=[
            TranslationCandidate(
                version=1,
                subversion=1,
                version_ref="1.1",
                target=target,
                created_at=TS,
                updated_at=TS,
            )
        ],
        reviews=reviews or [],
    )


def _review(
    *,
    review_ref="R1.1",
    pass_number=1,
    run_number=1,
    base_kind="translation",
    base_ref="1.1",
    base_target: str,
    target: str,
    status="accepted",
) -> TranslationReviewCandidate:
    return TranslationReviewCandidate(
        pass_number=pass_number,
        run_number=run_number,
        review_ref=review_ref,
        base_kind=base_kind,
        base_ref=base_ref,
        base_target_sha256=sha256_text(base_target),
        target=target,
        target_sha256=sha256_text(target),
        status=status,
        created_at=TS,
        updated_at=TS,
    )


def _write_store(proj, records: dict[str, StoredTranslationRecordV2]) -> None:
    write_translation_store(proj, TranslationStoreV2(records=records))
    _ledger(proj)


def _ids_for(chunks) -> dict[str, str]:
    """Map a content slug to its record id for the default fixture."""
    mapping: dict[str, str] = {}
    for chunk in chunks:
        for record in chunk.records:
            mapping[record.source] = record.id
    return mapping


def _export(project_dir, *extra, profile="de_default") -> object:
    args = ["translate", "export-index", str(project_dir)]
    if profile is not None:
        args += ["--profile", profile]
    args += list(extra)
    return runner.invoke(app, args)


# --- tests -------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path):
    proj, chunks = _make_project(tmp_path)
    wasp = next(r for r in (_ids_for(chunks)) if "Wasp scout arrived" in r)
    untranslated = next(r for r in _ids_for(chunks) if "Untranslated source" in r)
    wasp_id = _ids_for(chunks)[wasp]
    untranslated_id = _ids_for(chunks)[untranslated]
    _write_store(
        proj,
        {
            wasp_id: _record(chunks, wasp_id, "Der Wespen-Späher traf ein."),
        },
    )
    return proj.root, wasp_id, untranslated_id


def test_1_writes_all_three_files_by_default(project):
    project_dir, _, _ = project
    res = _export(project_dir)
    assert res.exit_code == 0, res.output
    proj = load_project(project_dir, profile="de_default")
    assert translation_source_index_path(proj).is_file()
    assert translation_target_index_path(proj).is_file()
    assert translation_source_target_index_path(proj).is_file()


def test_2_source_index_has_source_only_and_complete(project):
    project_dir, _, _ = project
    res = _export(project_dir, "--kind", "source")
    assert res.exit_code == 0, res.output
    proj = load_project(project_dir, profile="de_default")
    text = translation_source_index_path(proj).read_text("utf-8")
    assert "The Wasp scout arrived" in text
    assert "Der Wespen-Späher traf ein" not in text
    payload = json.loads(text)
    assert payload["record_count"] == len(payload["records"]) >= 1


def test_3_target_index_has_target_only_no_source(project):
    project_dir, _, _ = project
    res = _export(project_dir, "--kind", "target")
    assert res.exit_code == 0, res.output
    proj = load_project(project_dir, profile="de_default")
    text = translation_target_index_path(proj).read_text("utf-8")
    assert "Wespen-Späher" in text
    assert "The Wasp scout arrived" not in text
    assert "Wasp" not in text


def test_4_source_target_index_is_slim_with_source_and_target(project):
    project_dir, wasp_id, _ = project
    res = _export(project_dir, "--kind", "source-target")
    assert res.exit_code == 0, res.output
    proj = load_project(project_dir, profile="de_default")
    payload = json.loads(translation_source_target_index_path(proj).read_text("utf-8"))
    item = payload["records"][wasp_id]
    assert item["source"] == "The Wasp scout arrived."
    assert item["target"] == "Der Wespen-Späher traf ein."
    assert item["active_version"] == "1.1"
    assert item["active_review"] is None
    assert "versions" not in item
    assert "reviews" not in item


def test_5_source_target_index_marks_untranslated_null(project):
    project_dir, _, untranslated_id = project
    res = _export(project_dir, "--kind", "source-target")
    assert res.exit_code == 0, res.output
    proj = load_project(project_dir, profile="de_default")
    payload = json.loads(translation_source_target_index_path(proj).read_text("utf-8"))
    item = payload["records"][untranslated_id]
    assert item["source"] == "Untranslated source."
    assert item["target"] is None
    assert item["selected_kind"] is None
    assert item["selected_ref"] is None


def test_6_record_and_chapter_metadata_present(project):
    project_dir, wasp_id, _ = project
    res = _export(project_dir)
    assert res.exit_code == 0, res.output
    proj = load_project(project_dir, profile="de_default")
    source = json.loads(translation_source_index_path(proj).read_text("utf-8"))
    target = json.loads(translation_target_index_path(proj).read_text("utf-8"))
    for payload in (source, target):
        item = payload["records"][wasp_id]
        assert item["chunk_id"] == wasp_id.split("-")[0]
        assert item["part_id"] == wasp_id.split("-")[1]
        assert item["chapter_id"] == "0001"
        assert "chapter_title" in item
    target_item = target["records"][wasp_id]
    assert target_item["version"] == "1.1"
    assert target_item["review"] is None
    assert target_item["selected_kind"] == "translation"
    assert target_item["selected_ref"] == "1.1"


def test_7_active_review_output_exported(tmp_path: Path):
    proj, chunks = _make_project(tmp_path)
    ids = _ids_for(chunks)
    wasp_id = ids["The Wasp scout arrived."]
    base = "Rohfassung"
    polished = "Polierte Fassung"
    _write_store(
        proj,
        {
            wasp_id: _record(
                chunks,
                wasp_id,
                base,
                active_review="R1.1",
                reviews=[
                    _review(base_target=base, target=polished),
                ],
            )
        },
    )
    res = _export(proj.root)
    assert res.exit_code == 0, res.output
    target = json.loads(translation_target_index_path(proj).read_text("utf-8"))
    st = json.loads(translation_source_target_index_path(proj).read_text("utf-8"))
    for payload in (target, st):
        item = payload["records"][wasp_id]
        assert item["target"] == polished
        assert item["version"] == "1.1"
        assert item["review"] == "R1.1"
        assert item["selected_kind"] == "review"
        assert item["selected_ref"] == "R1.1"
        assert item["review_chain"] == ["R1.1"]


def test_8_stale_active_review_blocks_target_indexes(tmp_path: Path):
    proj, chunks = _make_project(tmp_path)
    ids = _ids_for(chunks)
    wasp_id = ids["The Wasp scout arrived."]
    # Review records a base hash that no longer matches the 1.1 target.
    _write_store(
        proj,
        {
            wasp_id: _record(
                chunks,
                wasp_id,
                "Neue Übersetzung",
                active_review="R1.1",
                reviews=[
                    _review(base_target="alte-baseline", target="Polierte Fassung"),
                ],
            )
        },
    )
    # Default run: source written, target-based blocked, exit non-zero.
    res = _export(proj.root)
    assert res.exit_code != 0, res.output
    assert translation_source_index_path(proj).is_file()
    assert not translation_target_index_path(proj).is_file()
    assert not translation_source_target_index_path(proj).is_file()
    assert "active_review_base_drift" in res.output
    # --kind source still succeeds on its own.
    res_src = _export(proj.root, "--kind", "source")
    assert res_src.exit_code == 0, res_src.output


def test_9_source_order_output(tmp_path: Path):
    proj, chunks = _make_project(tmp_path)
    ordered = [r.id for c in chunks for r in c.records]
    # Insert store records in reverse order.
    reversed_records = {
        rid: _record(chunks, rid, _source_text(chunks, rid))
        for rid in reversed(ordered)
        if _source_text(chunks, rid) != "Untranslated source."
    }
    _write_store(proj, reversed_records)
    res = _export(proj.root, "--kind", "source-target")
    assert res.exit_code == 0, res.output
    payload = json.loads(translation_source_target_index_path(proj).read_text("utf-8"))
    assert list(payload["records"].keys()) == ordered


def test_10_compact_one_record_per_line(project):
    project_dir, wasp_id, _ = project
    res = _export(project_dir)
    assert res.exit_code == 0, res.output
    proj = load_project(project_dir, profile="de_default")

    source_lines = translation_source_index_path(proj).read_text("utf-8").splitlines()
    match = next(line for line in source_lines if "The Wasp scout arrived" in line)
    assert wasp_id in match

    target_lines = translation_target_index_path(proj).read_text("utf-8").splitlines()
    match = next(line for line in target_lines if "Wespen-Späher" in line)
    assert wasp_id in match

    st_path = translation_source_target_index_path(proj)
    st_lines = st_path.read_text("utf-8").splitlines()
    match = next(line for line in st_lines if "The Wasp scout arrived" in line)
    assert wasp_id in match
    assert "Der Wespen-Späher traf ein" in match


def test_11_kind_selects_outputs(project, tmp_path: Path):
    project_dir, _, _ = project
    proj = load_project(project_dir, profile="de_default")
    paths = {
        "source": translation_source_index_path(proj),
        "target": translation_target_index_path(proj),
        "source-target": translation_source_target_index_path(proj),
    }
    for kind, path in paths.items():
        # Remove all, then export one kind, assert only that file appears.
        for p in paths.values():
            p.unlink(missing_ok=True)
        res = _export(project_dir, "--kind", kind)
        assert res.exit_code == 0, res.output
        assert path.is_file()
        others = [p for k, p in paths.items() if k != kind]
        assert all(not p.is_file() for p in others)

    for p in paths.values():
        p.unlink(missing_ok=True)
    res = _export(project_dir, "--kind", "source", "--kind", "target")
    assert res.exit_code == 0, res.output
    assert paths["source"].is_file()
    assert paths["target"].is_file()
    assert not paths["source-target"].is_file()


def test_12_command_summary_json(project):
    project_dir, _, _ = project
    res = _export(project_dir, "--json")
    assert res.exit_code == 0, res.output
    summary = json.loads(res.output)
    assert summary["source_path"].endswith("source-index.json")
    assert summary["target_path"].endswith("target-index.json")
    assert summary["source_target_path"].endswith("source-target-index.json")
    assert summary["source_record_count"] >= 2
    assert summary["target_record_count"] == 1
    assert summary["source_target_record_count"] == summary["source_record_count"]
    assert summary["translated_count"] == 1
    assert summary["missing_count"] == summary["source_record_count"] - 1
    assert summary["warning_count"] == 0
    assert summary["error_count"] == 0
    assert summary["written"] == ["source", "target", "source-target"]


def test_13_profile_root_isolation(monkeypatch, tmp_path: Path):
    proj, chunks = _make_project(tmp_path)
    ids = _ids_for(chunks)
    wasp_id = ids["The Wasp scout arrived."]
    wasp_rec = _record(chunks, wasp_id, "Der Wespen-Späher traf ein.")
    _write_store(proj, {wasp_id: wasp_rec})
    profile_root = proj.root / "translations" / "de_default"
    assert (profile_root / ".booktx-profile.json").is_file()
    monkeypatch.chdir(profile_root)

    res = runner.invoke(app, ["translate", "export-index", ".", "--json"])
    assert res.exit_code == 0, res.output
    summary = json.loads(res.output)
    for key in ("source_path", "target_path", "source_target_path"):
        value = summary[key]
        assert value is not None
        assert not value.startswith("/")
        assert "translations/" not in value
        assert "../" not in value
    # Human output paths are also profile-local.
    res_human = runner.invoke(app, ["translate", "export-index", "."])
    assert res_human.exit_code == 0, res_human.output
    assert "translations/" not in res_human.output
    assert "../" not in res_human.output
    assert "source-index.json" in res_human.output


def test_14_legacy_translated_chunk_guard(tmp_path: Path):
    proj, chunks = _make_project(tmp_path)
    ids = _ids_for(chunks)
    wasp_id = ids["The Wasp scout arrived."]
    untranslated_id = ids["Untranslated source."]
    # A legacy translated chunk covers every record in the chunk (so it passes
    # structural validation) but contributes targets the store cannot account
    # for: the heading and the untranslated record have no store selection, so
    # the editor indexes would disagree with build output.
    chunk_id = untranslated_id.split("-")[0]
    source_chunk = next(c for c in chunks if c.chunk_id == chunk_id)
    legacy = {
        "chunk_id": chunk_id,
        "records": [
            {"id": record.id, "target": record.source}
            for record in source_chunk.records
        ],
    }
    assert proj.translated_dir is not None
    (proj.translated_dir / f"{chunk_id}.json").write_text(
        json.dumps(legacy), encoding="utf-8"
    )
    wasp_rec = _record(chunks, wasp_id, "Der Wespen-Späher traf ein.")
    _write_store(proj, {wasp_id: wasp_rec})

    res = _export(proj.root)
    assert res.exit_code != 0, res.output
    assert translation_source_index_path(proj).is_file()
    assert not translation_target_index_path(proj).is_file()
    assert not translation_source_target_index_path(proj).is_file()
    assert "editor_index_legacy_contribution" in res.output


def test_15_no_custom_output_path_options(project):
    project_dir, _, _ = project
    res = _export(project_dir, "--source-output", "x.json")
    assert res.exit_code != 0
    help_res = runner.invoke(app, ["translate", "export-index", "--help"])
    assert "--source-output" not in help_res.output
    assert "--target-output" not in help_res.output
    assert "--source-target-output" not in help_res.output


def test_kind_rejects_invalid_value(project):
    project_dir, _, _ = project
    res = _export(project_dir, "--kind", "bogus")
    assert res.exit_code != 0
    assert "invalid --kind" in res.output
