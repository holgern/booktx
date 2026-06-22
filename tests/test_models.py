"""Tests for booktx.models: the translation-contract JSON shapes.

These tests pin the exact field names and ordering that cross the boundary to
the translating coding agent. If any of these break, the contract breaks.
"""

from __future__ import annotations

import json

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
    TranslatedChunk,
    TranslatedRecord,
    TranslationStore,
    TranslationTask,
    TranslationTaskRecord,
)


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
        "chunk_id",
        "source_language",
        "target_language",
        "records",
    ]
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


def test_translation_task_roundtrip():
    task = TranslationTask(
        task_id="bt-task-1",
        unit="paragraph",
        chapter_id="0006",
        chapter_title="Two",
        source_language="en",
        target_language="de",
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
    assert dumped["records"][0]["chunk_id"] == "0006"
    assert TranslationTask.model_validate_json(task.model_dump_json()) == task


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
    import pytest
    from pydantic import ValidationError

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
    )
    back = Manifest.model_validate_json(m.model_dump_json())
    assert back.source.filename == "book.md"
    assert back.record_count == 42


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
