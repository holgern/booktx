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
from booktx.context import context_markdown_path, default_context, write_context
from booktx.models import (
    Chunk,
    EpubTemplateData,
    Manifest,
    ManifestSource,
    Placeholder,
    Record,
    StoredTranslationRecord,
    StoredTranslationRecordV2,
    TranslationCandidate,
    TranslationStore,
    TranslationStoreV2,
    TranslationSubversionLedgerEntry,
    TranslationTrackLedgerEntry,
    TranslationVersionLedger,
)
from booktx.progress import source_record_sha256
from booktx.validate import (
    Severity,
    validate_chunk_pair,
    validate_project,
    validate_record_pair,
    write_report,
)

runner = CliRunner()


def _rewrite_project_chunk_size(project_dir: Path, chunk_size: int) -> None:
    from booktx.config import tomllib

    config_path = project_dir / ".booktx" / "source-config.toml"
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
    (proj.chunks_dir / "0001.json").write_text(
        source.model_dump_json(), encoding="utf-8"
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
    (proj.chunks_dir / "0001.json").write_text(
        _src_chunk().model_dump_json(), encoding="utf-8"
    )
    ctx = default_context(proj)
    ctx.ready = True
    write_context(proj, ctx)
    context_markdown_path(proj).write_text("stale render\n", encoding="utf-8")

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
        f.rule == "chunk_manifest_record_id_scheme_mismatch" for f in report.findings
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


def test_validate_errors_when_required_em_tag_missing():
    source = Record(
        id="0001-000001",
        source="A <em>title</em> here.",
        source_markup="epub-inline-xhtml:v1",
    )
    target = {"id": source.id, "target": "Ein Titel hier."}
    from booktx.models import TranslatedRecord

    findings = validate_record_pair(
        source, TranslatedRecord.model_validate(target), "0001"
    )

    assert any(
        f.rule == "inline_xhtml_preserved" and f.severity == Severity.ERROR
        for f in findings
    )


def test_validate_accepts_translated_text_inside_preserved_em_tag():
    source = Record(
        id="0001-000001",
        source="A <em>title</em> here.",
        source_markup="epub-inline-xhtml:v1",
    )
    from booktx.models import TranslatedRecord

    findings = validate_record_pair(
        source,
        TranslatedRecord(id=source.id, target="Ein <em>Titel</em> hier."),
        "0001",
    )

    assert not [f for f in findings if f.severity == Severity.ERROR]


def test_validate_rejects_new_inline_attribute():
    source = Record(
        id="0001-000001", source="<em>Title</em>", source_markup="epub-inline-xhtml:v1"
    )
    from booktx.models import TranslatedRecord

    findings = validate_record_pair(
        source,
        TranslatedRecord(id=source.id, target='<em class="new">Titel</em>'),
        "0001",
    )

    assert any(f.rule == "inline_xhtml_no_new_attributes" for f in findings)


def test_validate_rejects_block_tag_in_inline_xhtml_target():
    source = Record(
        id="0001-000001", source="<em>Title</em>", source_markup="epub-inline-xhtml:v1"
    )
    from booktx.models import TranslatedRecord

    findings = validate_record_pair(
        source, TranslatedRecord(id=source.id, target="<p><em>Titel</em></p>"), "0001"
    )

    assert any(f.rule == "inline_xhtml_no_block_tags" for f in findings)


def test_validate_rejects_changed_code_opaque_element():
    source = Record(
        id="0001-000001",
        source="Use <code>pip install booktx</code>.",
        source_markup="epub-inline-xhtml:v1",
    )
    from booktx.models import TranslatedRecord

    findings = validate_record_pair(
        source,
        TranslatedRecord(id=source.id, target="Nutze <code>pip install other</code>."),
        "0001",
    )

    assert any(f.rule == "inline_xhtml_opaque_preserved" for f in findings)


def test_validate_warns_when_dash_semantic_cue_missing():
    source = Record(
        id="0001-000001",
        source="<em>Again – now!</em>",
        source_markup="epub-inline-xhtml:v1",
    )
    from booktx.models import TranslatedRecord

    findings = validate_record_pair(
        source, TranslatedRecord(id=source.id, target="<em>Wieder jetzt!</em>"), "0001"
    )

    assert any(
        f.rule == "dash_semantic_cue_missing" and f.severity == Severity.WARN
        for f in findings
    )


def test_validate_accepts_plain_v1_records_without_xhtml():
    source = Record(id="0001-000001", source="A title here.")
    from booktx.models import TranslatedRecord

    findings = validate_record_pair(
        source, TranslatedRecord(id=source.id, target="Ein Titel hier."), "0001"
    )

    assert not [f for f in findings if f.rule.startswith("inline_xhtml_")]


# --- effective review output and review-coverage findings ----------------


def _store_with_review(
    tmp_path: Path, *, reviews, active_review=None, active_version="1.1"
):
    """Build a one-chunk project backed by a v2 store with review candidates."""

    proj = init_project(tmp_path / "book", target_language="de")
    proj.chunks_dir.mkdir(parents=True, exist_ok=True)
    source = _src_chunk()
    (proj.chunks_dir / "0001.json").write_text(
        source.model_dump_json(), encoding="utf-8"
    )
    base_target = "__NAME_001__ sah __NAME_002__ an."
    versions = [
        TranslationCandidate(
            version=1,
            subversion=1,
            version_ref="1.1",
            target=base_target,
            created_at="2026-06-22T12:00:00Z",
            updated_at="2026-06-22T12:00:00Z",
        )
    ]
    write_translation_store(
        proj,
        TranslationStoreV2(
            records={
                "0001-000001": StoredTranslationRecordV2(
                    chunk_id=1,
                    part_id=1,
                    source_sha256=source_record_sha256(source.records[0].source),
                    source=source.records[0].source,
                    active_version=active_version,
                    active_review=active_review,
                    versions=versions,
                    reviews=reviews,
                )
            }
        ),
    )
    # A v2 store requires the translation version to exist in the ledger when
    # the effective output falls back to the active translation version.
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
    return load_project(proj.root), base_target


def _review_candidate(
    *, review_ref="R1.1", target, base_kind="translation", base_ref="1.1", base_target
):
    from booktx.models import TranslationReviewCandidate
    from booktx.translation_store import sha256_text

    pass_number = int(review_ref.split("R")[1].split(".")[0])
    run_number = int(review_ref.split(".")[1])
    return TranslationReviewCandidate(
        pass_number=pass_number,
        run_number=run_number,
        review_ref=review_ref,
        base_kind=base_kind,
        base_ref=base_ref,
        base_target_sha256=sha256_text(base_target),
        target=target,
        target_sha256=sha256_text(target),
        created_at="2026-06-22T12:00:00Z",
        updated_at="2026-06-22T12:00:00Z",
    )


def test_effective_output_uses_active_review_when_valid(tmp_path: Path):
    from booktx.validate import load_effective_translated_chunks

    base = "__NAME_001__ sah __NAME_002__ an."
    polished = "__NAME_001__ sah __NAME_002__ hier an."
    reviews = [_review_candidate(target=polished, base_target=base)]
    proj, _ = _store_with_review(tmp_path, reviews=reviews, active_review="R1.1")
    effective = load_effective_translated_chunks(proj)
    chunk = effective.chunks["0001"]
    assert chunk.records[0].target == polished


def test_effective_output_falls_back_to_version_when_review_stale(tmp_path: Path):
    from booktx.validate import load_effective_translated_chunks

    reviews = [_review_candidate(target="polished-but-stale", base_target="different")]
    proj, base_target = _store_with_review(
        tmp_path, reviews=reviews, active_review="R1.1"
    )
    effective = load_effective_translated_chunks(proj)
    # Stale active review falls back to the active translation version.
    assert effective.chunks["0001"].records[0].target == base_target
    rules = {f.rule for f in effective.findings}
    assert "active_review_base_drift" in rules


def test_effective_output_reports_not_accepted_active_review(tmp_path: Path):
    from booktx.validate import load_effective_translated_chunks

    base = "__NAME_001__ sah __NAME_002__ an."
    review = _review_candidate(target="polished", base_target=base)
    review = review.model_copy(update={"status": "rejected"})
    proj, _ = _store_with_review(tmp_path, reviews=[review], active_review="R1.1")
    effective = load_effective_translated_chunks(proj)
    rules = {f.rule for f in effective.findings}
    # The selected review is rejected: output falls back to the version and the
    # unusable active_review is reported as an error.
    assert "active_review_not_accepted" in rules
    assert effective.chunks["0001"].records[0].target == base


def test_review_coverage_findings_missing_pass_when_enforced():
    from booktx.models import QualityReviewConfig, ReviewPassConfig
    from booktx.validate import Severity, review_coverage_findings

    base = "__NAME_001__ sah __NAME_002__ an."
    rec = StoredTranslationRecordV2(
        chunk_id=1,
        part_id=1,
        source_sha256="x",
        source="src",
        active_version="1.1",
        versions=[
            TranslationCandidate(
                version=1,
                subversion=1,
                version_ref="1.1",
                target=base,
                created_at="t",
                updated_at="t",
            )
        ],
    )
    cfg = QualityReviewConfig(
        enabled=True,
        active_passes=[1],
        passes=[
            ReviewPassConfig(pass_number=1, enforce="error"),
        ],
    )
    findings = review_coverage_findings(rec, cfg, "0001", "0001-000001")
    rules = {f.rule for f in findings}
    assert "missing_review_candidate" in rules
    assert all(f.severity == Severity.ERROR for f in findings)


def test_review_coverage_findings_off_when_enforce_off():
    from booktx.models import QualityReviewConfig, ReviewPassConfig
    from booktx.validate import review_coverage_findings

    base = "x"
    rec = StoredTranslationRecordV2(
        chunk_id=1,
        part_id=1,
        source_sha256="x",
        source="src",
        active_version="1.1",
        versions=[
            TranslationCandidate(
                version=1,
                subversion=1,
                version_ref="1.1",
                target=base,
                created_at="t",
                updated_at="t",
            )
        ],
    )
    cfg = QualityReviewConfig(
        enabled=True,
        active_passes=[1],
        passes=[
            ReviewPassConfig(pass_number=1, enforce="off"),
        ],
    )
    assert review_coverage_findings(rec, cfg, "0001", "0001-000001") == []


def test_review_coverage_findings_force_error_overrides_enforce():
    from booktx.models import QualityReviewConfig, ReviewPassConfig
    from booktx.validate import Severity, review_coverage_findings

    base = "x"
    rec = StoredTranslationRecordV2(
        chunk_id=1,
        part_id=1,
        source_sha256="x",
        source="src",
        active_version="1.1",
        versions=[
            TranslationCandidate(
                version=1,
                subversion=1,
                version_ref="1.1",
                target=base,
                created_at="t",
                updated_at="t",
            )
        ],
    )
    cfg = QualityReviewConfig(
        enabled=True,
        active_passes=[1],
        passes=[
            ReviewPassConfig(pass_number=1, enforce="off"),
        ],
    )
    findings = review_coverage_findings(
        rec, cfg, "0001", "0001-000001", force_error=True
    )
    assert findings and all(f.severity == Severity.ERROR for f in findings)


def test_review_coverage_findings_blocked_when_required_prior_pass_missing():
    from booktx.models import QualityReviewConfig, ReviewPassConfig
    from booktx.validate import review_coverage_findings

    base = "x"
    rec = StoredTranslationRecordV2(
        chunk_id=1,
        part_id=1,
        source_sha256="x",
        source="src",
        active_version="1.1",
        versions=[
            TranslationCandidate(
                version=1,
                subversion=1,
                version_ref="1.1",
                target=base,
                created_at="t",
                updated_at="t",
            )
        ],
    )
    cfg = QualityReviewConfig(
        enabled=True,
        active_passes=[1, 2],
        passes=[
            ReviewPassConfig(pass_number=1, enforce="error"),
            ReviewPassConfig(
                pass_number=2,
                enforce="error",
                base="active_review",
                required_base_pass=1,
            ),
        ],
    )
    findings = review_coverage_findings(rec, cfg, "0001", "0001-000001")
    rules = {f.rule for f in findings}
    # pass 1 is missing, pass 2 is blocked behind it.
    assert "missing_review_candidate" in rules
    assert "review_pass_blocked" in rules


def test_review_coverage_findings_satisfied_when_review_present():
    from booktx.models import QualityReviewConfig, ReviewPassConfig
    from booktx.validate import review_coverage_findings

    base = "x"
    reviews = [_review_candidate(target="polished", base_target=base)]
    rec = StoredTranslationRecordV2(
        chunk_id=1,
        part_id=1,
        source_sha256="x",
        source="src",
        active_version="1.1",
        active_review="R1.1",
        versions=[
            TranslationCandidate(
                version=1,
                subversion=1,
                version_ref="1.1",
                target=base,
                created_at="t",
                updated_at="t",
            )
        ],
        reviews=reviews,
    )
    cfg = QualityReviewConfig(
        enabled=True,
        active_passes=[1],
        passes=[
            ReviewPassConfig(pass_number=1, enforce="error"),
        ],
    )
    assert review_coverage_findings(rec, cfg, "0001", "0001-000001") == []


# --- EPUB inline-XHTML preflight (build-grade) ---------------------------


def _extract_epub_for_preflight(tmp_path: Path):
    import tests.test_epub_io as epub_fixtures
    from booktx.config import find_source_file

    proj = init_project(tmp_path / "book", target_language="de")
    epub_path = proj.source_dir / "book.epub"
    epub_fixtures._make_epub(epub_path)
    find_source_file(proj)
    res = CliRunner().invoke(app, ["extract", str(proj.root)])
    assert res.exit_code == 0, res.output
    return load_project(proj.root)


def _simulate_old_project_plain_records(proj) -> None:
    """Reproduce the brief's bug: old projects whose chunk records lack
    record-level ``source_markup`` (plain:v1) while the EPUB span manifest
    still marks spans ``epub-inline-xhtml:v1``. The manifest is the authority."""
    for path in proj.chunks():
        chunk = json.loads(path.read_text("utf-8"))
        for record in chunk["records"]:
            record.pop("source_markup", None)
        path.write_text(json.dumps(chunk, ensure_ascii=False), encoding="utf-8")


def _write_chunk_translations(proj, transform) -> None:
    proj.translated_dir.mkdir(parents=True, exist_ok=True)
    for path in proj.chunks():
        chunk = json.loads(path.read_text("utf-8"))
        records = [
            {"id": record["id"], "target": transform(record["source"])}
            for record in chunk["records"]
        ]
        payload = {"chunk_id": chunk["chunk_id"], "records": records}
        (proj.translated_dir / f"{chunk['chunk_id']}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )


def _broken_record_ids(proj) -> set[str]:
    return {
        record["id"]
        for path in proj.chunks()
        for record in json.loads(path.read_text("utf-8"))["records"]
        if "<strong>" in record["source"]
    }


def _drop_strong_target(src: str) -> str:
    """Translation transform that drops <strong>...</strong>; identity otherwise."""
    return "Hallo Welt." if "<strong>" in src else src


def test_preflight_catches_missing_tag_with_plain_record_markup(tmp_path: Path):
    # ac-0006: manifest is authoritative. Even when Record.source_markup is
    # plain:v1 (old project), the preflight catches the missing inline tag.
    from booktx.epub_preflight import validate_epub_inline_preflight

    proj = _extract_epub_for_preflight(tmp_path)
    _simulate_old_project_plain_records(proj)
    _write_chunk_translations(proj, _drop_strong_target)

    findings = validate_epub_inline_preflight(proj)

    assert any(
        f.rule == "inline_xhtml_preserved" and f.severity == "error" for f in findings
    ), findings
    assert any(f.record_id for f in findings), "expected an attributed record id"
    assert any(f.span_index is not None for f in findings)
    assert any(f.block_id for f in findings)
    assert any(f.document_href for f in findings)


def test_validate_epub_preflight_clean_when_tags_preserved(tmp_path: Path):
    from booktx.epub_preflight import validate_epub_inline_preflight

    proj = _extract_epub_for_preflight(tmp_path)
    _simulate_old_project_plain_records(proj)
    _write_chunk_translations(proj, lambda src: src)  # identity -> source-as-target

    findings = validate_epub_inline_preflight(proj)
    assert findings == []


def test_validate_epub_preflight_chapter_scope_filters_unrelated(tmp_path: Path):
    from booktx.cli import _project_status_snapshot
    from booktx.epub_preflight import validate_epub_inline_preflight

    proj = _extract_epub_for_preflight(tmp_path)
    _simulate_old_project_plain_records(proj)
    _write_chunk_translations(proj, _drop_strong_target)

    bundle = _project_status_snapshot(proj)
    all_chapter_ids = [c.chapter_id for c in bundle.index.chapter_summaries]
    broken_ids = _broken_record_ids(proj)
    broken_chapter = next(
        (
            cid
            for cid in all_chapter_ids
            if any(
                rid in broken_ids
                for rid in bundle.index.record_ids_by_chapter.get(cid, [])
            )
        ),
        None,
    )
    other_chapter = next(
        (cid for cid in all_chapter_ids if cid != broken_chapter), None
    )
    if other_chapter is not None:
        scoped = validate_epub_inline_preflight(proj, chapter_id=other_chapter)
        assert scoped == [], scoped
    if broken_chapter is not None:
        scoped_broken = validate_epub_inline_preflight(proj, chapter_id=broken_chapter)
        assert any(f.rule == "inline_xhtml_preserved" for f in scoped_broken)


def test_validate_epub_preflight_record_id_scope(tmp_path: Path):
    from booktx.epub_preflight import validate_epub_inline_preflight

    proj = _extract_epub_for_preflight(tmp_path)
    _simulate_old_project_plain_records(proj)
    _write_chunk_translations(proj, _drop_strong_target)
    broken_ids = _broken_record_ids(proj)
    # Scoping to the broken record's id surfaces the span finding.
    scoped = validate_epub_inline_preflight(proj, record_ids=broken_ids)
    assert any(f.rule == "inline_xhtml_preserved" for f in scoped)

    # Scoping to a record in a DIFFERENT span (ch2 "The end") yields nothing,
    # because that span is not touched by the broken record.
    other_span_id = next(
        record["id"]
        for path in proj.chunks()
        for record in json.loads(path.read_text("utf-8"))["records"]
        if record["source"] == "The end."
    )
    scoped_other = validate_epub_inline_preflight(proj, record_ids={other_span_id})
    assert scoped_other == []


def test_validate_project_catches_missing_inline_tag_via_preflight(tmp_path: Path):
    # End-to-end: validate_project uses the shared EPUB preflight so it catches
    # the skeleton mismatch before build, even for old projects without
    # record-level source_markup.
    proj = _extract_epub_for_preflight(tmp_path)
    _simulate_old_project_plain_records(proj)
    _write_chunk_translations(proj, _drop_strong_target)

    report = validate_project(proj)

    assert any(
        f.rule == "inline_xhtml_preserved" and f.severity == "error"
        for f in report.findings
    ), report.findings
    assert not report.passed


def test_check_chapter_reports_exact_inline_location(tmp_path: Path):
    # CLI-level test for 'booktx check --chapter 0005'.
    # Assert output includes chapter id, record id, source snippet, rule.
    proj = _extract_epub_for_preflight(tmp_path)
    _simulate_old_project_plain_records(proj)
    _write_chunk_translations(proj, _drop_strong_target)

    # Get the broken chapter id.
    broken_ids = _broken_record_ids(proj)
    from booktx.cli import _project_status_snapshot

    bundle = _project_status_snapshot(proj)
    broken_chapter = next(
        (
            c.chapter_id
            for c in bundle.index.chapter_summaries
            if any(
                rid in broken_ids
                for rid in bundle.index.record_ids_by_chapter.get(c.chapter_id, [])
            )
        ),
        None,
    )
    if broken_chapter is None:
        # No chapter map available; skip scoped CLI test.
        return
    res = runner.invoke(app, ["check", str(proj.root), "--chapter", broken_chapter])
    assert res.exit_code == 1
    output = res.output
    assert "inline_xhtml_preserved" in output
    assert broken_chapter in output or "inline_xhtml" in output


def test_audit_inline_uses_manifest_when_record_markup_missing(tmp_path: Path):
    # ac-0010: audit_inline returns nonzero records_with_inline_source for
    # EPUB projects even when Record.source_markup is plain:v1.
    from booktx.inline_audit import audit_inline_xhtml

    proj = _extract_epub_for_preflight(tmp_path)
    _simulate_old_project_plain_records(proj)
    _write_chunk_translations(proj, _drop_strong_target)

    result = audit_inline_xhtml(proj)

    assert result.records_with_inline_source > 0
    assert any(f["rule"] == "inline_xhtml_preserved" for f in result.findings), (
        result.findings
    )
