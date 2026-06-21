"""Tests for spinetx.chunking: segmentation and chunk packing."""

from __future__ import annotations

from spinetx.chunking import ProseSpan, pack_chunks, segment_spans, spans_to_chunks
from spinetx.models import Placeholder, Record


def _rec(id_: str, source: str) -> Record:
    return Record(id=id_, source=source)


def test_segment_one_record_per_sentence():
    spans = [
        ProseSpan(
            text="Hello world. This is a test! Right?",
            placeholders=[],
            protected_terms=[],
        )
    ]
    records = segment_spans(spans, language="en")
    assert [r.source for r in records] == ["Hello world.", "This is a test!", "Right?"]


def test_segment_drops_blank_sentences():
    spans = [
        ProseSpan(text="   \n\nFirst.   \n   ", placeholders=[], protected_terms=[])
    ]
    records = segment_spans(spans, language="en")
    assert [r.source for r in records] == ["First."]


def test_segment_carries_placeholders_and_terms():
    spans = [
        ProseSpan(
            text="__NAME_001__ met Bob.",
            placeholders=[
                Placeholder(token="__NAME_001__", original="Alice", kind="name")
            ],
            protected_terms=["Alice"],
        )
    ]
    records = segment_spans(spans, language="en")
    assert len(records) == 1
    rec = records[0]
    assert rec.protected_terms == ["Alice"]
    assert rec.placeholders[0].original == "Alice"


def test_segment_filters_placeholders_to_visible_record_tokens():
    spans = [
        ProseSpan(
            text=(
                "__TAG_001__First__TAG_002__ sentence. "
                "Second sentence without tags."
            ),
            placeholders=[
                Placeholder(token="__TAG_001__", original="<i>", kind="tag"),
                Placeholder(token="__TAG_002__", original="</i>", kind="tag"),
            ],
            protected_terms=[],
        )
    ]

    records = segment_spans(spans, language="en")

    assert [p.token for p in records[0].placeholders] == [
        "__TAG_001__",
        "__TAG_002__",
    ]
    assert records[1].source == "Second sentence without tags."
    assert records[1].placeholders == []


def test_segment_protected_terms_follow_visible_name_tokens():
    spans = [
        ProseSpan(
            text="__NAME_001__ arrived. Nobody mentioned the other name.",
            placeholders=[
                Placeholder(token="__NAME_001__", original="Alice", kind="name"),
                Placeholder(token="__NAME_002__", original="Bob", kind="name"),
            ],
            protected_terms=["Alice", "Bob"],
        )
    ]

    records = segment_spans(spans, language="en")

    assert records[0].protected_terms == ["Alice"]
    assert [p.token for p in records[0].placeholders] == ["__NAME_001__"]
    assert records[1].protected_terms == []
    assert records[1].placeholders == []


def test_pack_assigns_contract_ids():
    records = [_rec("000001", f"Sentence {i}.") for i in range(3)]
    chunks = pack_chunks(
        records, source_language="en", target_language="de", chunk_size=2
    )
    assert [c.chunk_id for c in chunks] == ["0001", "0002"]
    assert [r.id for r in chunks[0].records] == ["0001-000001", "0001-000002"]
    assert [r.id for r in chunks[1].records] == ["0002-000001"]
    assert chunks[0].source_language == "en"
    assert chunks[0].target_language == "de"


def test_pack_respects_chunk_size():
    records = [_rec("000001", f"s{i}.") for i in range(7)]
    chunks = pack_chunks(
        records, source_language="en", target_language="de", chunk_size=3
    )
    assert [len(c.records) for c in chunks] == [3, 3, 1]


def test_pack_empty_records():
    chunks = pack_chunks([], source_language="en", target_language="de")
    assert chunks == []


def test_pack_rejects_invalid_size():
    import pytest

    with pytest.raises(ValueError):
        pack_chunks(
            [_rec("000001", "x.")],
            source_language="en",
            target_language="de",
            chunk_size=0,
        )


def test_spans_to_chunks_end_to_end():
    spans = [
        ProseSpan(text="One. Two. Three.", placeholders=[], protected_terms=[]),
        ProseSpan(text="Four.", placeholders=[], protected_terms=[]),
    ]
    chunks = spans_to_chunks(
        spans, source_language="en", target_language="de", chunk_size=2
    )
    assert len(chunks) == 2
    assert chunks[0].records[0].source == "One."
    assert chunks[1].records[-1].source == "Four."
    # all record ids unique
    ids = [r.id for c in chunks for r in c.records]
    assert len(ids) == len(set(ids))


def test_segment_phrasplit_abbreviations():
    spans = [
        ProseSpan(
            text="Dr. Smith met Mr. Jones. They talked about the U.S.A.",
            placeholders=[],
            protected_terms=[],
        )
    ]
    records = segment_spans(spans, language="en")
    assert [r.source for r in records] == [
        "Dr. Smith met Mr. Jones.",
        "They talked about the U.S.A.",
    ]


def test_segment_bcp47_language_code_uses_primary_subtag():
    spans = [
        ProseSpan(text="Dr. Smith arrived. Good.", placeholders=[], protected_terms=[])
    ]
    records = segment_spans(spans, language="en-US")
    assert [r.source for r in records] == ["Dr. Smith arrived.", "Good."]


def test_segment_unknown_language_falls_back_to_english():
    spans = [
        ProseSpan(text="Dr. Smith arrived. Good.", placeholders=[], protected_terms=[])
    ]
    records = segment_spans(spans, language="xx-TEST")
    assert [r.source for r in records] == ["Dr. Smith arrived.", "Good."]


def test_segment_preserves_placeholder_tokens():
    spans = [
        ProseSpan(
            text="__NAME_001__ met __NAME_002__. Then __TAG_001__ stayed.",
            placeholders=[],
            protected_terms=[],
        )
    ]
    records = segment_spans(spans, language="en")
    assert [r.source for r in records] == [
        "__NAME_001__ met __NAME_002__.",
        "Then __TAG_001__ stayed.",
    ]
