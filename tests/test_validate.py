"""Tests for booktx.validate: every hard rule from the translation contract."""

from __future__ import annotations

import json
from pathlib import Path

import tomli_w

from typer.testing import CliRunner

from booktx.cli import app
from booktx.config import (
    init_project,
    load_project,
    write_manifest,
    write_translation_store,
    write_translation_version_ledger,
)
from booktx.context import default_context, write_context
from booktx.models import (
    Chunk,
    EpubTemplateData,
    Manifest,
    ManifestSource,
    Placeholder,
    Record,
    StoredTranslationRecord,
    StoredTranslationRecordV2,
    TranslationStore,
    TranslationStoreV2,
    TranslationCandidate,
    TranslationSubversionLedgerEntry,
    TranslationTrackLedgerEntry,
    TranslationVersionLedger,
)
from booktx.progress import source_record_sha256
from booktx.validate import (
    Severity,
    validate_chunk_pair,
    validate_project,
    write_report,
)

runner = CliRunner()


def _rewrite_project_chunk_size(project_dir: Path, chunk_size: int) -> None:
    from booktx.config import tomllib

    config_path = project_dir / ".booktx" / "config.toml"
    with config_path.open("rb") as fh:
        data = tomllib.load(fh)
    data["chunk_size"] = chunk_size
    config_path.write_bytes(tomli_w.dumps(data).encode("utf-8"))



def _src_chunk(chunk_id: str = "0001") -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        source_language="en",
        target_language="de",
        records=[
            Record(
                id=f"{chunk_id}-000001",
                source="__NAME_001__ looked at __NAME_002__ here.",
                protected_terms=["Alice", "Mr. Smith"],
                placeholders=[
                    Placeholder(token="__NAME_001__", original="Alice", kind="name"),
                    Placeholder(
                        token="__NAME_002__", original="Mr. Smith", kind="name"
                    ),
                ],
            ),
            Record(
                id=f"{chunk_id}-000002",
                source="Run __TAG_001__ now.",
                protected_terms=[],
                placeholders=[
                    Placeholder(token="__TAG_001__", original="`code`", kind="tag")
                ],
            ),
        ],
    )


def _write_translation(tmp_path: Path, chunk_id: str, payload: object) -> Path:
    proj = init_project(tmp_path / "book", target_language="de")
    chunks_dir = proj.chunks_dir
    chunks_dir.mkdir(parents=True, exist_ok=True)
    (chunks_dir / f"{chunk_id}.json").write_text(
        _src_chunk(chunk_id).model_dump_json(), encoding="utf-8"
    )
    translated = proj.translated_dir / f"{chunk_id}.json"
    translated.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        translated.write_text(payload, encoding="utf-8")
    else:
        translated.write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path / "book"


def _valid_translation() -> dict:
    return {
        "chunk_id": "0001",
        "records": [
            {"id": "0001-000001", "target": "__NAME_001__ sah __NAME_002__ an."},
            {"id": "0001-000002", "target": "Führe __TAG_001__ aus."},
        ],
    }


def test_valid_translation_passes(tmp_path: Path):
    proj_path = _write_translation(tmp_path, "0001", _valid_translation())
    proj = load_project(proj_path)
    report = validate_project(proj)
    assert report.passed, [f.as_dict() for f in report.findings]
    assert report.errors == []


def test_store_backed_translation_passes(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    proj.chunks_dir.mkdir(parents=True, exist_ok=True)
    source = _src_chunk()
    (proj.chunks_dir / "0001.json").write_text(
        source.model_dump_json(),
        encoding="utf-8",
    )
    write_translation_store(
        proj,
        TranslationStoreV2(
            records={
                "0001-000001": StoredTranslationRecordV2(
                    chunk_id=1,
                    part_id=1,
                    source_sha256=source_record_sha256(source.records[0].source),
                    source=source.records[0].source,
                    active_version="1.1",
                    versions=[
                        TranslationCandidate(
                            version=1,
                            subversion=1,
                            version_ref="1.1",
                            target="__NAME_001__ sah __NAME_002__ an.",
                            created_at="2026-06-22T12:00:00Z",
                            updated_at="2026-06-22T12:00:00Z",
                        )
                    ],
                ),
                "0001-000002": StoredTranslationRecordV2(
                    chunk_id=1,
                    part_id=2,
                    source_sha256=source_record_sha256(source.records[1].source),
                    source=source.records[1].source,
                    active_version="1.1",
                    versions=[
                        TranslationCandidate(
                            version=1,
                            subversion=1,
                            version_ref="1.1",
                            target="Führe __TAG_001__ aus.",
                            created_at="2026-06-22T12:00:00Z",
                            updated_at="2026-06-22T12:00:00Z",
                        )
                    ],
                ),
            }
        ),
    )
    write_translation_version_ledger(
        proj,
        TranslationVersionLedger(
            active_version="1.1",
            tracks={
                "1": TranslationTrackLedgerEntry(
                    version=1,
                    actor="user:test",
                    harness="pi",
                    model="human",
                    created_at="2026-06-22T12:00:00Z",
                    updated_at="2026-06-22T12:00:00Z",
                    subversions={
                        "1": TranslationSubversionLedgerEntry(
                            version=1,
                            subversion=1,
                            version_ref="1.1",
                            context_sha256="a" * 64,
                            created_at="2026-06-22T12:00:00Z",
                            updated_at="2026-06-22T12:00:00Z",
                        )
                    },
                )
            },
        ),
    )

    report = validate_project(load_project(proj.root))

    assert report.passed, [f.as_dict() for f in report.findings]
    assert report.chunks_passed == 1


def test_rule_invalid_json(tmp_path: Path):
    proj_path = _write_translation(tmp_path, "0001", "{not valid json")
    report = validate_project(load_project(proj_path))
    rules = {f.rule for f in report.findings}
    assert "invalid_json_or_commentary" in rules
    assert not report.passed


def test_rule_record_count_changed(tmp_path: Path):
    payload = _valid_translation()
    payload["records"].append({"id": "0001-000003", "target": "Extra."})
    proj_path = _write_translation(tmp_path, "0001", payload)
    report = validate_project(load_project(proj_path))
    assert "record_count_changed" in {f.rule for f in report.findings}
    assert not report.passed


def test_rule_record_id_changed(tmp_path: Path):
    payload = _valid_translation()
    payload["records"][0]["id"] = "0001-999999"
    proj_path = _write_translation(tmp_path, "0001", payload)
    report = validate_project(load_project(proj_path))
    rules = {f.rule for f in report.findings}
    assert "record_id_removed" in rules or "record_id_added" in rules
    assert not report.passed


def test_rule_empty_target(tmp_path: Path):
    payload = _valid_translation()
    payload["records"][0]["target"] = "   "
    proj_path = _write_translation(tmp_path, "0001", payload)
    report = validate_project(load_project(proj_path))
    assert "empty_target" in {f.rule for f in report.findings}
    assert not report.passed


def test_rule_placeholder_removed(tmp_path: Path):
    payload = _valid_translation()
    payload["records"][1]["target"] = "Führe aus."  # dropped __TAG_001__
    proj_path = _write_translation(tmp_path, "0001", payload)
    report = validate_project(load_project(proj_path))
    assert "placeholder_removed_or_changed" in {f.rule for f in report.findings}
    assert not report.passed


def test_rule_placeholder_added(tmp_path: Path):
    payload = _valid_translation()
    payload["records"][0]["target"] = "__NAME_001__ sah __NAME_002__ __TAG_099__ an."
    proj_path = _write_translation(tmp_path, "0001", payload)
    report = validate_project(load_project(proj_path))
    assert "placeholder_added" in {f.rule for f in report.findings}
    assert not report.passed


def test_placeholder_metadata_only_tokens_do_not_require_target_tokens(tmp_path: Path):
    chunk = Chunk(
        chunk_id="0001",
        source_language="en",
        target_language="de",
        records=[
            Record(
                id="0001-000001",
                source="No visible placeholder here.",
                protected_terms=[],
                placeholders=[
                    Placeholder(token="__TAG_001__", original="<i>", kind="tag"),
                    Placeholder(token="__TAG_002__", original="</i>", kind="tag"),
                ],
            )
        ],
    )
    translated = tmp_path / "0001.json"
    translated.write_text(
        json.dumps(
            {
                "chunk_id": "0001",
                "records": [
                    {
                        "id": "0001-000001",
                        "target": "Hier ist kein sichtbarer Platzhalter.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    findings = validate_chunk_pair(chunk, translated)

    assert [f.as_dict() for f in findings] == []


def test_placeholder_visible_source_tokens_still_required(tmp_path: Path):
    chunk = Chunk(
        chunk_id="0001",
        source_language="en",
        target_language="de",
        records=[
            Record(
                id="0001-000001",
                source="Run __TAG_001__ now.",
                protected_terms=[],
                placeholders=[],
            )
        ],
    )
    translated = tmp_path / "0001.json"
    translated.write_text(
        json.dumps(
            {
                "chunk_id": "0001",
                "records": [{"id": "0001-000001", "target": "Jetzt ausführen."}],
            }
        ),
        encoding="utf-8",
    )

    findings = validate_chunk_pair(chunk, translated)

    assert "placeholder_removed_or_changed" in {f.rule for f in findings}


def test_rule_protected_name_translated(tmp_path: Path):
    payload = _valid_translation()
    # Alice placeholder dropped and the name rendered in target language.
    payload["records"][0]["target"] = "Aliza sah __NAME_002__ an."
    proj_path = _write_translation(tmp_path, "0001", payload)
    report = validate_project(load_project(proj_path))
    rules = {f.rule for f in report.findings}
    assert "protected_name_translated_or_removed" in rules
    assert "placeholder_removed_or_changed" in rules
    assert not report.passed


def test_rule_commentary_outside_json(tmp_path: Path):
    raw = json.dumps(_valid_translation())
    proj_path = _write_translation(tmp_path, "0001", raw + "\n\n// hope this helps!")
    report = validate_project(load_project(proj_path))
    assert "invalid_json_or_commentary" in {f.rule for f in report.findings}
    assert not report.passed


def test_missing_translation_is_not_an_error(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    proj.chunks_dir.mkdir(parents=True, exist_ok=True)
    (proj.chunks_dir / "0001.json").write_text(
        _src_chunk().model_dump_json(), encoding="utf-8"
    )
    report = validate_project(proj)
    assert report.passed  # no translated file yet
    assert report.chunks_missing_translation == 1


def test_stale_translation_is_a_warning(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    proj.chunks_dir.mkdir(parents=True, exist_ok=True)
    proj.translated_dir.mkdir(parents=True, exist_ok=True)
    # translated file with no matching chunk
    (proj.translated_dir / "9999.json").write_text(
        json.dumps({"chunk_id": "9999", "records": []}), encoding="utf-8"
    )
    report = validate_project(proj)
    assert report.passed  # warnings do not fail
    assert any(f.rule == "stale_translation" for f in report.warnings)


def test_stale_store_record_is_an_error(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    proj.chunks_dir.mkdir(parents=True, exist_ok=True)
    (proj.chunks_dir / "0001.json").write_text(
        _src_chunk().model_dump_json(),
        encoding="utf-8",
    )
    write_translation_store(
        proj,
        TranslationStore(
            records={
                "0001-000099": StoredTranslationRecord(
                    chunk_id="0001",
                    source_sha256="abc123",
                    target="Ghost record.",
                    updated_at="2026-06-22T12:00:00Z",
                )
            }
        ),
    )

    report = validate_project(load_project(proj.root))

    assert not report.passed
    assert any(f.rule == "stale_store_record" for f in report.findings)


def test_missing_ledger_version_is_an_error_for_v2_store(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    proj.chunks_dir.mkdir(parents=True, exist_ok=True)
    source = _src_chunk()
    (proj.chunks_dir / "0001.json").write_text(source.model_dump_json(), encoding="utf-8")
    write_translation_store(
        proj,
        TranslationStoreV2(
            records={
                "0001-000001": StoredTranslationRecordV2(
                    chunk_id=1,
                    part_id=1,
                    source_sha256=source_record_sha256(source.records[0].source),
                    source=source.records[0].source,
                    active_version="1.1",
                    versions=[
                        TranslationCandidate(
                            version=1,
                            subversion=1,
                            version_ref="1.1",
                            target="__NAME_001__ sah __NAME_002__ an.",
                            created_at="2026-06-22T12:00:00Z",
                            updated_at="2026-06-22T12:00:00Z",
                        )
                    ],
                )
            }
        ),
    )

    report = validate_project(load_project(proj.root))

    assert not report.passed
    assert any(f.rule == "missing_ledger_version" for f in report.findings)


def test_context_render_drift_is_a_warning(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    proj.chunks_dir.mkdir(parents=True, exist_ok=True)
    (proj.chunks_dir / "0001.json").write_text(_src_chunk().model_dump_json(), encoding="utf-8")
    ctx = default_context(proj)
    ctx.ready = True
    write_context(proj, ctx)
    (proj.booktx_dir / "context.md").write_text("stale render\n", encoding="utf-8")

    report = validate_project(proj)

    assert any(f.rule == "context_render_drift" for f in report.findings)


def test_validate_warns_on_config_chunk_size_drift(tmp_path: Path):
    src = tmp_path / "story.md"
    src.write_text("# One\n\nHello there.\n", encoding="utf-8")
    proj = init_project(tmp_path / "book", target_language="de", source_file=src)
    assert runner.invoke(app, ["extract", str(proj.root)]).exit_code == 0

    _rewrite_project_chunk_size(proj.root, 25)
    report = validate_project(load_project(proj.root))

    assert report.passed
    assert any(f.rule == "manifest_chunk_size_drift" for f in report.findings)


def test_validate_errors_on_chunk_manifest_record_id_scheme_mismatch(tmp_path: Path):
    src = tmp_path / "story.md"
    src.write_text("# One\n\nHello there.\n", encoding="utf-8")
    proj = init_project(tmp_path / "book", target_language="de", source_file=src)
    assert runner.invoke(app, ["extract", str(proj.root)]).exit_code == 0

    chunk_path = proj.chunks_dir / "0001.json"
    chunk = json.loads(chunk_path.read_text("utf-8"))
    chunk["record_id_scheme"] = "opaque:v9"
    chunk_path.write_text(json.dumps(chunk), encoding="utf-8")

    report = validate_project(load_project(proj.root))

    assert not report.passed
    assert any(
        f.rule == "chunk_manifest_record_id_scheme_mismatch"
        for f in report.findings
    )


def test_validate_errors_on_unsupported_chunk_schema_version(tmp_path: Path):
    src = tmp_path / "story.md"
    src.write_text("# One\n\nHello there.\n", encoding="utf-8")
    proj = init_project(tmp_path / "book", target_language="de", source_file=src)
    assert runner.invoke(app, ["extract", str(proj.root)]).exit_code == 0

    chunk_path = proj.chunks_dir / "0001.json"
    chunk = json.loads(chunk_path.read_text("utf-8"))
    chunk["schema_version"] = 99
    chunk_path.write_text(json.dumps(chunk), encoding="utf-8")

    report = validate_project(load_project(proj.root))

    assert not report.passed
    assert any(f.rule == "unsupported_chunk_schema_version" for f in report.findings)


def test_write_report_creates_json(tmp_path: Path):
    proj_path = _write_translation(tmp_path, "0001", _valid_translation())
    proj = load_project(proj_path)
    report = validate_project(proj)
    out = write_report(proj, report)
    assert out.is_file()
    data = json.loads(out.read_text("utf-8"))
    assert data["passed"] is True
    assert data["chunks_checked"] == 1


def test_validate_chunk_pair_directly():
    # Pair-level API works without a project on disk.
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "0001.json"
        p.write_text(json.dumps(_valid_translation()), encoding="utf-8")
        findings = validate_chunk_pair(_src_chunk(), p)
        assert all(f.severity != Severity.ERROR for f in findings)


def test_new_epub_source_chunk_with_tag_tokens_fails(tmp_path: Path):
    source = tmp_path / "source.epub"
    source.write_bytes(b"PK\x03\x04dummy")
    proj = init_project(tmp_path / "book", target_language="de", source_file=source)
    manifest = Manifest(
        version=2,
        source=ManifestSource(
            filename="source.epub",
            format="epub",
            source_language="en",
            target_language="de",
            sha256="abc123",
        ),
        template=EpubTemplateData(
            pipeline="epub2text+text2epub",
            epub2text_schema="epub2text.structured.v1",
            text2epub_manifest={
                "schema_version": 1,
                "source_sha256": "abc123",
                "entries": [],
            },
            spans=[],
            navigation=[],
        ).model_dump(mode="json"),
    )
    write_manifest(proj, manifest)
    proj.chunks_dir.mkdir(parents=True, exist_ok=True)
    (proj.chunks_dir / "0001.json").write_text(
        Chunk(
            chunk_id="0001",
            source_language="en",
            target_language="de",
            records=[
                Record(
                    id="0001-000001",
                    source="Hallo __TAG_001__ Welt.",
                    protected_terms=[],
                    placeholders=[],
                )
            ],
        ).model_dump_json(),
        encoding="utf-8",
    )

    report = validate_project(proj)

    assert any(
        finding.rule == "epub_source_contains_legacy_placeholders"
        for finding in report.findings
    )
    assert not report.passed
