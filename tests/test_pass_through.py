"""Tests for booktx.pass_through: identity translated chunk generation.

Covers the unit/service workflow and the ``pass-through`` /
``profile create-pass-through`` CLI commands. The pass-through profile is a
generated reconstruction fixture: each record's target equals its source text.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from text2epub.validation import sha256_path
from typer.testing import CliRunner

import tests.test_epub_io as epub_fixtures
from booktx.cli import app
from booktx.config import (
    BooktxError,
    load_profile_config,
    load_translation_store,
)
from booktx.io_utils import utc_timestamp
from booktx.models import (
    Chunk,
    Record,
    StoredTranslationRecordV2,
    TranslatedChunk,
    TranslationCandidate,
    TranslationStoreV2,
)
from booktx.pass_through import (
    _identity_translated_chunk,
    ensure_pass_through_profile,
    run_pass_through,
    write_pass_through_chunks,
)
from booktx.record_refs import canonical_record_id

runner = CliRunner()

MARKDOWN_DOC = """\
# Hello

Alice met Bob. They were happy.
"""


def _make_markdown_project(tmp_path: Path) -> Path:
    src = tmp_path / "book.md"
    src.write_text(MARKDOWN_DOC, encoding="utf-8")
    project_dir = tmp_path / "book"
    res = runner.invoke(
        app,
        [
            "init",
            str(project_dir),
            "--source-file",
            str(src),
            "--source-lang",
            "en",
        ],
    )
    assert res.exit_code == 0, res.output
    res = runner.invoke(app, ["extract", str(project_dir)])
    assert res.exit_code == 0, res.output
    return project_dir


def _make_epub_project(tmp_path: Path) -> tuple[Path, Path]:
    project_dir = tmp_path / "book"
    res = runner.invoke(app, ["init", str(project_dir)])
    assert res.exit_code == 0, res.output
    epub_path = project_dir / "source" / "book.epub"
    epub_fixtures._make_epub(epub_path)
    res = runner.invoke(app, ["extract", str(project_dir)])
    assert res.exit_code == 0, res.output
    return project_dir, epub_path


def _seed_store_record(project) -> None:
    """Write one accepted V2 store record so effective translations can override."""
    rid = canonical_record_id(1, 1)
    ts = utc_timestamp()
    store = TranslationStoreV2(
        records={
            rid: StoredTranslationRecordV2(
                chunk_id=1,
                part_id=1,
                source_sha256="seed",
                source="seed record",
                active_version="1.1",
                versions=[
                    TranslationCandidate(
                        version=1,
                        subversion=1,
                        version_ref="1.1",
                        target="seed target",
                        created_at=ts,
                        updated_at=ts,
                    )
                ],
            )
        }
    )
    from booktx.config import write_translation_store

    write_translation_store(project, store)


# --- unit / service tests ---------------------------------------------------


def test_identity_translated_chunk_preserves_ids_order_and_source():
    chunk = Chunk(
        chunk_id="0001",
        source_language="en",
        records=[
            Record(id="0001-000001", source="Alice met Bob."),
            Record(id="0001-000002", source="They were happy."),
        ],
    )

    translated = _identity_translated_chunk(chunk)

    assert translated.chunk_id == "0001"
    assert [record.id for record in translated.records] == [
        "0001-000001",
        "0001-000002",
    ]
    assert translated.records[0].target == "Alice met Bob."
    assert translated.records[1].target == "They were happy."


def test_write_pass_through_chunks_writes_profile_local_translated_files(
    tmp_path: Path,
):
    project_dir = _make_markdown_project(tmp_path)
    proj = ensure_pass_through_profile(project_dir, "pt", create=True)

    chunks_written, records_written, stale_removed = write_pass_through_chunks(proj)

    assert chunks_written >= 1
    assert records_written >= 1
    assert stale_removed == 0

    translated_files = sorted(proj.translated_dir.glob("*.json"))
    assert translated_files
    for path in translated_files:
        # Profile-local path, never the shared .booktx/translated tree.
        assert path.relative_to(project_dir).parts[0] == "translations"
        assert "translated" in path.relative_to(project_dir).parts
        # Uses the translated chunk schema.
        chunk = TranslatedChunk.model_validate_json(path.read_text("utf-8"))
        assert chunk.chunk_id
        assert chunk.records


def test_pass_through_refuses_normal_translation_profile(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    res = runner.invoke(
        app, ["profile", "create", str(project_dir), "de_real", "--target", "de"]
    )
    assert res.exit_code == 0, res.output

    with pytest.raises(BooktxError) as exc:
        ensure_pass_through_profile(project_dir, "de_real")

    assert exc.value.code == "not_pass_through_profile"


def test_pass_through_refuses_store_override(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    proj = ensure_pass_through_profile(project_dir, "pt", create=True)
    _seed_store_record(proj)

    with pytest.raises(BooktxError) as exc:
        run_pass_through(proj, clear_store=False)

    assert exc.value.code == "pass_through_store_not_empty"


def test_pass_through_clear_store_allows_run(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    proj = ensure_pass_through_profile(project_dir, "pt", create=True)
    _seed_store_record(proj)

    result = run_pass_through(proj, clear_store=True)

    assert result.chunks_written >= 1
    assert not load_translation_store(proj).records

    res = runner.invoke(app, ["status", str(project_dir), "--profile", "pt", "--json"])
    assert res.exit_code == 0, res.output
    totals = json.loads(res.output)["totals"]
    assert totals["records_translated"] == totals["records_total"]
    assert totals["records_total"] > 0


def test_pass_through_prunes_stale_translated_chunk(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    proj = ensure_pass_through_profile(project_dir, "pt", create=True)

    stale_path = proj.translated_dir / "9999.json"
    stale_path.parent.mkdir(parents=True, exist_ok=True)
    stale_path.write_text(
        json.dumps({"chunk_id": "9999", "records": []}), encoding="utf-8"
    )

    chunks_written, _records_written, stale_removed = write_pass_through_chunks(proj)

    assert stale_removed == 1
    assert not stale_path.exists()
    assert chunks_written == len(proj.chunks())


# --- CLI tests --------------------------------------------------------------


def test_pass_through_cli_create_validate_build_markdown(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)

    res = runner.invoke(
        app,
        ["pass-through", str(project_dir), "--profile", "passthrough_en", "--create"],
    )

    assert res.exit_code == 0, res.output
    out_dir = project_dir / "translations" / "passthrough_en" / "output"
    assert any(out_dir.glob("*.md"))

    res = runner.invoke(
        app, ["status", str(project_dir), "--profile", "passthrough_en", "--json"]
    )
    assert res.exit_code == 0, res.output
    totals = json.loads(res.output)["totals"]
    assert totals["records_translated"] == totals["records_total"]
    assert totals["records_total"] > 0


def test_pass_through_cli_requires_explicit_profile(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    runner.invoke(
        app,
        [
            "profile",
            "create",
            str(project_dir),
            "de_real",
            "--target",
            "de",
            "--select",
        ],
    )

    # Missing required --profile must be a Typer usage error, never an
    # accidental write into the real translation profile.
    res = runner.invoke(app, ["pass-through", str(project_dir)])

    assert res.exit_code != 0


def test_pass_through_cli_epub_identity_output_matches_fixture_expectation(
    tmp_path: Path,
):
    project_dir, epub_path = _make_epub_project(tmp_path)

    res = runner.invoke(
        app,
        [
            "pass-through",
            str(project_dir),
            "--profile",
            "pt",
            "--create",
            "--json",
        ],
    )

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    out_path = Path(payload["output_path"])
    assert payload["format"] == "epub"
    assert sha256_path(out_path) == sha256_path(epub_path)


def test_profile_create_pass_through_command(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)

    res = runner.invoke(
        app,
        ["profile", "create-pass-through", str(project_dir), "pt", "--select"],
    )

    assert res.exit_code == 0, res.output
    cfg = load_profile_config(project_dir, "pt")
    assert cfg.kind == "pass-through"
    assert cfg.target_language == cfg.source_language

    identity = json.loads(
        (project_dir / "translations" / "pt" / "identity.json").read_text("utf-8")
    )
    assert identity["model"] == "booktx/pass-through"


def test_profile_list_and_show_include_kind(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    runner.invoke(
        app, ["profile", "create", str(project_dir), "de_real", "--target", "de"]
    )
    runner.invoke(app, ["profile", "create-pass-through", str(project_dir), "pt"])

    res = runner.invoke(app, ["profile", "list", str(project_dir), "--json"])
    assert res.exit_code == 0, res.output
    kinds = {p["profile"]: p["kind"] for p in json.loads(res.output)["profiles"]}
    assert kinds["de_real"] == "translation"
    assert kinds["pt"] == "pass-through"

    show = runner.invoke(app, ["profile", "show", str(project_dir), "pt", "--json"])
    assert show.exit_code == 0, show.output
    assert json.loads(show.output)["kind"] == "pass-through"

    human = runner.invoke(app, ["profile", "show", str(project_dir), "pt"]).output
    assert "kind: pass-through" in human
