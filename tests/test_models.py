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
    ProfileConfig,
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


def test_profile_config_kind_defaults_and_validation():
    cfg = ProfileConfig(profile="de_default", target_language="de")
    assert cfg.kind == "translation"

    pt = ProfileConfig(profile="pt", target_language="en", kind="pass-through")
    assert pt.kind == "pass-through"

    with pytest.raises(ValidationError):
        ProfileConfig(profile="pt", target_language="en", kind="bogus")

    # A legacy config.toml without `kind` still loads.
    legacy = ProfileConfig.model_validate({"profile": "de", "target_language": "de"})
    assert legacy.kind == "translation"


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


def test_translation_todo_model_validates_without_rebuild_side_effects():
    """Regression: TranslationTodo must validate without importing agent_todo."""
    from booktx.models import TranslationTodo

    payload = {
        "version": 1,
        "todo_id": "bt-todo-test",
        "profile": "de_gpt5_5",
        "target_language": "de",
        "target_locale": "de-DE",
        "chapters_requested": 1,
        "batch_words": 800,
        "created_at": "2026-06-24T10:16:39Z",
        "start_totals": {
            "source_words": 10,
            "translated_words": 0,
            "remaining_words": 10,
            "records_total": 2,
            "records_translated": 0,
            "records_remaining": 2,
            "chunks_total": 1,
            "chunks_complete": 0,
            "chunks_partial": 0,
            "chunks_pending": 1,
            "chapters_total": 1,
            "chapters_complete": 0,
            "chapters_partial": 0,
            "chapters_pending": 1,
            "invalid_translation_files": 0,
            "stale_translation_files": 0,
        },
        "chapters": [],
    }

    todo = TranslationTodo.model_validate(payload)
    assert todo.start_totals.records_total == 2


# --- translation review candidates -----------------------------------------


def _review_candidate(**overrides):
    base = {
        "pass_number": 1,
        "run_number": 1,
        "review_ref": "R1.1",
        "base_kind": "translation",
        "base_ref": "1.1",
        "base_target_sha256": "h-base",
        "target": "target",
        "target_sha256": "h-target",
        "created_at": "2026-06-25T10:00:00Z",
        "updated_at": "2026-06-25T10:00:00Z",
    }
    base.update(overrides)
    return base


def _record_with_reviews(reviews, **overrides):
    payload = {
        "chunk_id": 2,
        "part_id": 17,
        "source_sha256": "src",
        "source": "He nodded slowly.",
        "active_version": "1.1",
        "versions": [
            {
                "version": 1,
                "subversion": 1,
                "version_ref": "1.1",
                "target": "Er nickte langsam.",
                "created_at": "2026-06-25T10:00:00Z",
                "updated_at": "2026-06-25T10:00:00Z",
            }
        ],
        "reviews": reviews,
    }
    payload.update(overrides)
    return payload


def test_review_candidate_accepts_r1_1():
    from booktx.models import TranslationReviewCandidate

    c = TranslationReviewCandidate.model_validate(_review_candidate())
    assert c.review_ref == "R1.1"
    assert c.status == "accepted"


@pytest.mark.parametrize(
    "ref,pass_number,run_number",
    [("1.1", 1, 1), ("R0.1", 1, 1), ("R1.0", 1, 1), ("R1.1", 2, 1), ("R1.1", 1, 2)],
)
def test_review_candidate_rejects_bad_ref_or_mismatch(
    ref: str, pass_number: int, run_number: int
) -> None:
    from booktx.models import TranslationReviewCandidate

    with pytest.raises(ValidationError):
        TranslationReviewCandidate.model_validate(
            _review_candidate(
                review_ref=ref, pass_number=pass_number, run_number=run_number
            )
        )


def test_record_rejects_duplicate_review_refs():
    from booktx.models import StoredTranslationRecordV2

    revs = [_review_candidate(), _review_candidate(run_number=1)]
    with pytest.raises(ValidationError):
        StoredTranslationRecordV2.model_validate(_record_with_reviews(revs))


def test_record_rejects_review_with_missing_translation_base():
    from booktx.models import StoredTranslationRecordV2

    revs = [_review_candidate(base_ref="9.9")]
    with pytest.raises(ValidationError):
        StoredTranslationRecordV2.model_validate(_record_with_reviews(revs))


def test_record_rejects_review_with_missing_review_base():
    from booktx.models import StoredTranslationRecordV2

    revs = [_review_candidate(base_kind="review", base_ref="R9.9")]
    with pytest.raises(ValidationError):
        StoredTranslationRecordV2.model_validate(_record_with_reviews(revs))


def test_record_rejects_active_review_without_match():
    from booktx.models import StoredTranslationRecordV2

    with pytest.raises(ValidationError):
        StoredTranslationRecordV2.model_validate(
            _record_with_reviews([_review_candidate()], active_review="R5.5")
        )


def test_review_pass_order_helper_rejects_non_increasing_pass():
    from booktx.models import (
        TranslationReviewCandidate,
        _validate_review_pass_order,
    )

    r1 = TranslationReviewCandidate.model_validate(_review_candidate())
    # A review-based candidate whose pass is not strictly greater than its base.
    r_same = TranslationReviewCandidate.model_validate(
        _review_candidate(
            pass_number=1,
            run_number=2,
            review_ref="R1.2",
            base_kind="review",
            base_ref="R1.1",
        )
    )
    with pytest.raises(ValueError):
        _validate_review_pass_order([r1, r_same])


def test_review_graph_acyclic_helper_rejects_cycle():
    from booktx.models import (
        TranslationReviewCandidate,
        _validate_review_graph_is_acyclic,
    )

    # Build a cycle the pass-order rule cannot catch (constructed directly,
    # not via the record validator) to exercise the defensive graph check.
    a = TranslationReviewCandidate.model_validate(
        _review_candidate(review_ref="R1.1", base_kind="review", base_ref="R2.1")
    )
    b = TranslationReviewCandidate.model_validate(
        _review_candidate(
            pass_number=2,
            run_number=1,
            review_ref="R2.1",
            base_kind="review",
            base_ref="R1.1",
        )
    )
    with pytest.raises(ValueError):
        _validate_review_graph_is_acyclic([a, b])


def test_record_rejects_review_pass_not_greater_than_base():
    from booktx.models import StoredTranslationRecordV2

    revs = [
        _review_candidate(),
        _review_candidate(
            pass_number=1,
            run_number=2,
            review_ref="R1.2",
            base_kind="review",
            base_ref="R1.1",
        ),
    ]
    with pytest.raises(ValidationError):
        StoredTranslationRecordV2.model_validate(_record_with_reviews(revs))


def test_record_review_example_round_trips_json():
    from booktx.models import StoredTranslationRecordV2

    payload = _record_with_reviews(
        [
            _review_candidate(run_number=1, review_ref="R1.1"),
            _review_candidate(
                pass_number=2,
                run_number=1,
                review_ref="R2.1",
                base_kind="review",
                base_ref="R1.1",
            ),
        ],
        active_review="R2.1",
    )
    rec = StoredTranslationRecordV2.model_validate(payload)
    round_trip = StoredTranslationRecordV2.model_validate_json(rec.model_dump_json())
    assert [r.review_ref for r in round_trip.reviews] == ["R1.1", "R2.1"]
    assert round_trip.active_review == "R2.1"


def test_profile_config_without_quality_review_has_no_key():

    cfg = ProfileConfig(profile="p", target_language="de")
    dumped = cfg.model_dump(mode="json", exclude_none=True)
    assert "quality_review" not in dumped


def test_profile_config_with_quality_review_round_trips():
    from booktx.models import QualityReviewConfig, ReviewPassConfig

    cfg = ProfileConfig(
        profile="p",
        target_language="de",
        quality_review=QualityReviewConfig(
            enabled=True,
            active_passes=[1, 2],
            passes=[
                ReviewPassConfig(pass_number=1, name="Flow", enforce="warn"),
                ReviewPassConfig(
                    pass_number=2,
                    name="Polish",
                    base="active_review",
                    required_base_pass=1,
                ),
            ],
        ),
    )
    dumped = cfg.model_dump(mode="json", exclude_none=True)
    assert dumped["quality_review"]["enabled"] is True
    assert [p["pass_number"] for p in dumped["quality_review"]["passes"]] == [1, 2]
