"""Tests for booktx.models: the translation-contract JSON shapes.

These tests pin the exact field names and ordering that cross the boundary to
the translating coding agent. If any of these break, the contract breaks.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from booktx.models import (
    Chunk,
    EpubNavigationRef,
    EpubSpanRef,
    EpubTemplateData,
    Manifest,
    ManifestSource,
    NamesFile,
    Placeholder,
    ProjectConfig,
    Record,
    StoredTranslationRecord,
    StoredTranslationRecordV2,
    TranslatedChunk,
    TranslatedRecord,
    TranslationCandidate,
    TranslationIdentity,
    TranslationStore,
    TranslationStoreV2,
    TranslationSubversionLedgerEntry,
    TranslationTask,
    TranslationTaskRecord,
    TranslationTrackLedgerEntry,
    TranslationVersionLedger,
)
from booktx.record_refs import parse_record_ref, parse_version_ref


def _sample_record() -> Record:
    return Record(
        id="0001-000001",
        source="Alice looked at Mr. Smith.",
        protected_terms=["Alice", "Mr. Smith"],
        placeholders=[],
    )


def test_record_has_contract_fields():
    r = _sample_record()
    dumped = json.loads(r.model_dump_json())
    assert list(dumped.keys()) == [
        "id",
        "source",
        "protected_terms",
        "placeholders",
        "span_index",
        "span_record_index",
    ]


def test_placeholder_has_contract_fields():
    p = Placeholder(token="__NAME_001__", original="Alice", kind="name")
    dumped = json.loads(p.model_dump_json())
    assert list(dumped.keys()) == ["token", "original", "kind"]


def test_chunk_matches_contract_example():
    """Mirrors the exact JSON in booktx_coding_agent_start.md."""
    chunk = Chunk(
        chunk_id="0001",
        chunk_size=50,
        source_language="en",
        target_language="de",
        records=[
            Record(
                id="0001-000001",
                source="Alice looked at Mr. Smith.",
                protected_terms=["Alice", "Mr. Smith"],
                placeholders=[],
            )
        ],
    )
    dumped = json.loads(chunk.model_dump_json())
    assert list(dumped.keys()) == [
        "schema_version",
        "chunk_id",
        "chunk_size",
        "record_id_scheme",
        "source_language",
        "target_language",
        "records",
    ]
    assert dumped["schema_version"] == 2
    assert dumped["chunk_size"] == 50
    assert dumped["record_id_scheme"] == "chunk-local:v1"
    assert dumped["records"][0]["id"] == "0001-000001"


def test_translated_chunk_matches_contract_example():
    tc = TranslatedChunk(
        chunk_id="0001",
        records=[TranslatedRecord(id="0001-000001", target="Alice sah Mr. Smith an.")],
    )
    dumped = json.loads(tc.model_dump_json())
    # Translated contract only exposes chunk_id and records[{id, target}].
    assert dumped["chunk_id"] == "0001"
    assert dumped["records"][0] == {
        "id": "0001-000001",
        "target": "Alice sah Mr. Smith an.",
    }


def test_translated_record_accepts_optional_version():
    rec = TranslatedRecord(
        id="0002-000013",
        version="1.1",
        target="Zeit für das Übliche.",
    )
    assert rec.version == "1.1"


def test_translated_record_rejects_bad_version():
    with pytest.raises(ValidationError):
        TranslatedRecord(id="0002-000013", version="1", target="x")


def test_translated_record_legacy_without_version_is_valid():
    rec = TranslatedRecord(id="0002-000013", target="Zeit für das Übliche.")
    assert rec.version is None


def test_translation_store_roundtrip():
    store = TranslationStore(
        source_sha256="abc123",
        records={
            "0001-000001": StoredTranslationRecord(
                chunk_id="0001",
                source_sha256="def456",
                target="Hallo Welt.",
                updated_at="2026-06-22T12:00:00Z",
            )
        },
    )

    dumped = json.loads(store.model_dump_json())

    assert dumped["version"] == 1
    assert dumped["records"]["0001-000001"]["chunk_id"] == "0001"
    assert TranslationStore.model_validate_json(store.model_dump_json()) == store


def test_record_ref_parses_supported_forms():
    compact = parse_record_ref("74@38")
    padded_compact = parse_record_ref("0074@000038")
    canonical = parse_record_ref("0074-000038")
    shorthand = parse_record_ref("74-38")

    assert compact.chunk_id == 74
    assert compact.part_id == 38
    assert compact.canonical_id == "0074-000038"
    assert padded_compact == compact
    assert canonical == compact
    assert shorthand == compact


def test_invalid_record_ref_fails_concisely():
    with pytest.raises(ValueError, match="invalid record reference"):
        parse_record_ref("74")


def test_version_ref_parses_and_sorts_numerically():
    parsed = parse_version_ref("1.1")
    later = parse_version_ref("1.10")

    assert parsed.version == 1
    assert parsed.subversion == 1
    assert parsed.version_ref == "1.1"
    assert parsed < later


def test_invalid_version_ref_fails():
    for bad in ("1", "1.0", "01.1"):
        with pytest.raises(ValueError, match="invalid version reference"):
            parse_version_ref(bad)


def test_translation_store_v2_roundtrip():
    store = TranslationStoreV2(
        source_sha256="abc123",
        records={
            "0074-000038": StoredTranslationRecordV2(
                chunk_id=74,
                part_id=38,
                source_sha256="def456",
                source="'But which choice do we have?'",
                active_version="1.1",
                versions=[
                    TranslationCandidate(
                        version=1,
                        subversion=1,
                        version_ref="1.1",
                        target="Aber welche Wahl haben wir?",
                        created_at="2026-06-22T12:00:00Z",
                        updated_at="2026-06-22T12:00:00Z",
                    )
                ],
            )
        },
    )

    dumped = json.loads(store.model_dump_json())

    assert dumped["version"] == 2
    assert dumped["records"]["0074-000038"]["chunk_id"] == 74
    assert dumped["records"]["0074-000038"]["part_id"] == 38
    assert dumped["records"]["0074-000038"]["versions"][0]["version_ref"] == "1.1"
    assert TranslationStoreV2.model_validate_json(store.model_dump_json()) == store


def test_translation_store_v2_rejects_mismatched_record_key():
    with pytest.raises(ValidationError, match="canonical id"):
        TranslationStoreV2(
            records={
                "0074-000039": StoredTranslationRecordV2(
                    chunk_id=74,
                    part_id=38,
                    source_sha256="def456",
                    source="Hello",
                    versions=[],
                )
            }
        )


def test_translation_candidate_rejects_mismatched_version_ref():
    with pytest.raises(ValidationError, match="must equal '1.2'"):
        TranslationCandidate(
            version=1,
            subversion=2,
            version_ref="1.3",
            target="Hallo",
            created_at="2026-06-22T12:00:00Z",
            updated_at="2026-06-22T12:00:00Z",
        )


def test_ledger_roundtrip_and_validates_keys():
    ledger = TranslationVersionLedger(
        source_sha256="abc123",
        active_version="1.1",
        tracks={
            "1": TranslationTrackLedgerEntry(
                version=1,
                actor="user:nahrstaedt",
                harness="pi",
                model="codex-openai/gpt-5.5@low",
                created_at="2026-06-22T12:00:00Z",
                updated_at="2026-06-22T12:00:00Z",
                subversions={
                    "1": TranslationSubversionLedgerEntry(
                        version=1,
                        subversion=1,
                        version_ref="1.1",
                        context_sha256="a" * 64,
                        baseline_sha256="a" * 64,
                        baseline_path="translations/de/context.json",
                        created_at="2026-06-22T12:00:00Z",
                        updated_at="2026-06-22T12:00:00Z",
                    )
                },
            )
        },
    )

    dumped = json.loads(ledger.model_dump_json())
    assert dumped["tracks"]["1"]["subversions"]["1"]["version_ref"] == "1.1"
    assert (
        TranslationVersionLedger.model_validate_json(ledger.model_dump_json()) == ledger
    )


def test_ledger_rejects_mismatched_track_key():
    with pytest.raises(ValidationError, match="track key"):
        TranslationVersionLedger(
            tracks={
                "2": TranslationTrackLedgerEntry(
                    version=1,
                    actor="user:test",
                    harness="pi",
                    model="human",
                    created_at="2026-06-22T12:00:00Z",
                    updated_at="2026-06-22T12:00:00Z",
                )
            }
        )


def test_translation_identity_roundtrip():
    identity = TranslationIdentity(
        actor="user:nahrstaedt",
        harness="pi",
        model="codex-openai/gpt-5.5@low",
    )
    assert json.loads(identity.model_dump_json())["actor"] == "user:nahrstaedt"


def test_translation_task_roundtrip():
    task = TranslationTask(
        task_id="bt-task-1",
        unit="paragraph",
        chapter_id="0006",
        chapter_title="Two",
        source_language="en",
        target_language="de",
        translation_version="1.1",
        baseline_ref="1.1",
        baseline_sha256="c" * 64,
        context_sha256="a" * 64,
        context_view_sha256="a" * 64,
        context_view_path="translations/de/context-history/views/aaaaaaaa/context.json",
        context_notes_scope="before_target_chapter",
        context_target_chapter_id="0006",
        context_notes_through_chapter_id="0005",
        source_sha256="b" * 64,
        source_words=12,
        record_count=1,
        records=[
            TranslationTaskRecord(
                id="0006-000001",
                chunk_id="0006",
                source="Hello __NAME_001__.",
                protected_terms=["Alice"],
                placeholders=[
                    Placeholder(token="__NAME_001__", original="Alice", kind="name")
                ],
            )
        ],
    )

    dumped = json.loads(task.model_dump_json())

    assert dumped["task_id"] == "bt-task-1"
    assert dumped["translation_version"] == "1.1"
    assert dumped["baseline_ref"] == "1.1"
    assert dumped["baseline_sha256"] == "c" * 64
    assert dumped["context_sha256"] == "a" * 64
    assert dumped["context_view_sha256"] == "a" * 64
    assert dumped["context_notes_scope"] == "before_target_chapter"
    assert dumped["source_sha256"] == "b" * 64
    assert dumped["records"][0]["chunk_id"] == "0006"
    assert TranslationTask.model_validate_json(task.model_dump_json()) == task


def test_translation_task_legacy_without_metadata_is_valid():
    task = TranslationTask.model_validate(
        {
            "task_id": "bt-task-legacy",
            "unit": "batch",
            "source_language": "en",
            "target_language": "de",
            "records": [],
        }
    )
    assert task.translation_version is None
    assert task.baseline_ref is None
    assert task.baseline_sha256 is None
    assert task.context_sha256 is None
    assert task.context_view_sha256 is None
    assert task.source_sha256 is None


def test_chunk_roundtrip_through_json():
    chunk = Chunk(
        chunk_id="0002",
        source_language="en",
        target_language="fr",
        records=[
            Record(
                id="0002-000001",
                source="Hello __NAME_001__.",
                protected_terms=["Alice"],
                placeholders=[
                    Placeholder(token="__NAME_001__", original="Alice", kind="name")
                ],
            )
        ],
    )
    js = chunk.model_dump_json()
    back = Chunk.model_validate_json(js)
    assert back == chunk
    assert back.records[0].placeholders[0].original == "Alice"


def test_old_chunk_without_metadata_loads():
    chunk = Chunk.model_validate(
        {
            "chunk_id": "0001",
            "source_language": "en",
            "target_language": "de",
            "records": [],
        }
    )
    assert chunk.schema_version == 2
    assert chunk.chunk_size == 50
    assert chunk.record_id_scheme == "chunk-local:v1"


def test_project_config_defaults_and_validation():
    cfg = ProjectConfig(
        target_language="de",
        source_file="book.md",
        format="markdown",
    )
    assert cfg.source_language == "en"
    assert cfg.chunk_size == 50
    assert cfg.format == "markdown"


def test_project_config_rejects_unknown_format():
    with pytest.raises(ValidationError):
        ProjectConfig(
            target_language="de",
            source_file="book.pdf",
            format="pdf",  # unsupported in v1
        )


def test_names_file_default():
    nf = NamesFile()
    assert nf.protected_terms == []
    nf2 = NamesFile.model_validate({"protected_terms": ["Alice", "Baker Street"]})
    assert nf2.protected_terms == ["Alice", "Baker Street"]


def test_manifest_roundtrip():
    m = Manifest(
        source=ManifestSource(
            filename="book.md",
            format="markdown",
            source_language="en",
            target_language="de",
            sha256="abc123",
        ),
        chunk_count=3,
        record_count=42,
        chunk_size=50,
        record_id_scheme="chunk-local:v1",
        segmenter={"name": "phrasplit"},
        names_sha256="abc123",
    )
    back = Manifest.model_validate_json(m.model_dump_json())
    assert back.source.filename == "book.md"
    assert back.record_count == 42
    assert back.chunk_size == 50
    assert back.record_id_scheme == "chunk-local:v1"
    assert back.segmenter["name"] == "phrasplit"
    assert back.names_sha256 == "abc123"


def test_epub_template_data_roundtrip():
    template = EpubTemplateData(
        pipeline="epub2text+text2epub",
        epub2text_schema="epub2text.structured.v1",
        text2epub_manifest={"schema_version": 1, "source_sha256": "abc", "entries": []},
        spans=[
            EpubSpanRef(
                span_index=0,
                block_id="spine-0001:block-000001",
                document_href="OEBPS/Text/ch1.xhtml",
                spine_index=1,
                tag_name="p",
                source_text="Hello world.",
                source_text_sha256="deadbeef",
                placeholders=[
                    Placeholder(token="__NAME_001__", original="Alice", kind="name")
                ],
                protected_terms=["Alice"],
            )
        ],
        navigation=[
            EpubNavigationRef(
                id="nav:0:test",
                title="Chapter One",
                document_href="OEBPS/Text/ch1.xhtml",
                spine_index=1,
                order=0,
            )
        ],
    )

    dumped = json.loads(template.model_dump_json())
    assert dumped["pipeline"] == "epub2text+text2epub"
    assert dumped["spans"][0]["block_id"] == "spine-0001:block-000001"
    assert dumped["navigation"][0]["title"] == "Chapter One"
