"""Sentence segmentation and chunk packing.

The extractor hands chunking a list of *protected prose spans* (each already
had names and inline tags replaced by placeholder tokens). chunking:

1. Segments each span into sentences with :mod:`phrasplit`.
2. Assigns each resulting sentence a stable record id.
3. Packs records into :class:`~booktx.models.Chunk` objects of at most
   ``chunk_size`` records, numbering chunks from 1.

Record ids are ``NNNN-NNNNNN``: the 4-digit chunk id, a dash, and a 1-based
6-digit index inside that chunk. Chunk ids are zero-padded 4-digit strings.

The goal stated in the spec is **one source sentence to one translated
sentence** — chunking never merges or splits beyond what phrasplit returns.
"""

from __future__ import annotations

from dataclasses import dataclass

from phrasplit import split_with_offsets

from booktx.models import Chunk, Placeholder, Record
from booktx.placeholders import collect_tokens

__all__ = [
    "ProseSpan",
    "segment_spans",
    "pack_chunks",
    "spans_to_chunks",
]

# phrasplit's simple backend expects spaCy-style model names for abbreviation
# lookup, even when spaCy is not used. Keep booktx deterministic by forcing the
# regex backend and mapping BCP-47-ish project language codes to phrasplit's
# abbreviation tables.
_LANGUAGE_MODEL_BY_CODE: dict[str, str] = {
    "ca": "ca_core_news_sm",
    "da": "da_core_news_sm",
    "de": "de_core_news_sm",
    "el": "el_core_news_sm",
    "en": "en_core_web_sm",
    "es": "es_core_news_sm",
    "fi": "fi_core_news_sm",
    "fr": "fr_core_news_sm",
    "hr": "hr_core_news_sm",
    "it": "it_core_news_sm",
    "lt": "lt_core_news_sm",
    "mk": "mk_core_news_sm",
    "nb": "nb_core_news_sm",
    "nl": "nl_core_news_sm",
    "pl": "pl_core_news_sm",
    "pt": "pt_core_news_sm",
    "ro": "ro_core_news_sm",
    "ru": "ru_core_news_sm",
    "sl": "sl_core_news_sm",
    "sv": "sv_core_news_sm",
    "uk": "uk_core_news_sm",
}


@dataclass(slots=True)
class ProseSpan:
    """A protected prose span produced by a format extractor.

    ``text`` is the prose with names/tags already replaced by placeholder
    tokens. ``placeholders`` lists every placeholder that appears anywhere in
    ``text``. Segmentation filters that list per record so each record carries
    only placeholders visible in its own source. ``protected_terms`` is the
    subset of names relevant to this span.
    """

    text: str
    placeholders: list[Placeholder]
    protected_terms: list[str]


def _language_model(language: str) -> str:
    # booktx config language codes are BCP-47-ish. phrasplit's abbreviation
    # tables are keyed by spaCy model names. Fall back to English for unknown
    # language codes so segmentation stays deterministic.
    code = language.split("-", 1)[0].lower() or "en"
    return _LANGUAGE_MODEL_BY_CODE.get(code, "en_core_web_sm")


def _sentences(text: str, *, language: str) -> list[str]:
    segments = split_with_offsets(
        text,
        mode="sentence",
        use_spacy=False,
        language_model=_language_model(language),
    )
    return [segment.text for segment in segments]


def segment_spans(spans: list[ProseSpan], *, language: str = "en") -> list[Record]:
    """Segment every span into one :class:`Record` per sentence.

    Empty/whitespace-only sentences are dropped so a span never yields a blank
    record. Each record carries only placeholders and protected terms visible
    in that record's source text.
    """
    records: list[Record] = []
    counter = 0
    for span in spans:
        if not span.text or not span.text.strip():
            continue
        sentences = _sentences(span.text, language=language)
        for sentence in sentences:
            cleaned = sentence.strip()
            if not cleaned:
                continue
            visible_tokens = set(collect_tokens(cleaned))
            record_placeholders = [
                p for p in span.placeholders if p.token in visible_tokens
            ]
            record_terms = [p.original for p in record_placeholders if p.kind == "name"]
            counter += 1
            records.append(
                Record(
                    id=f"{counter:06d}",  # provisional; repack reassigns
                    source=cleaned,
                    protected_terms=record_terms,
                    placeholders=record_placeholders,
                )
            )
    return records


def pack_chunks(
    records: list[Record],
    *,
    source_language: str,
    target_language: str,
    chunk_size: int = 50,
) -> list[Chunk]:
    """Pack records into chunks of at most ``chunk_size`` and assign final ids.

    Final record ids are ``NNNN-NNNNNN`` (chunk id + 1-based intra-chunk index).
    """
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    chunks: list[Chunk] = []
    for chunk_idx, start in enumerate(range(0, len(records), chunk_size), start=1):
        chunk_id = f"{chunk_idx:04d}"
        bucket = records[start : start + chunk_size]
        renumbered: list[Record] = []
        for intra, rec in enumerate(bucket, start=1):
            renumbered.append(rec.model_copy(update={"id": f"{chunk_id}-{intra:06d}"}))
        chunks.append(
            Chunk(
                chunk_id=chunk_id,
                source_language=source_language,
                target_language=target_language,
                records=renumbered,
            )
        )
    return chunks


def spans_to_chunks(
    spans: list[ProseSpan],
    *,
    source_language: str,
    target_language: str,
    chunk_size: int = 50,
) -> list[Chunk]:
    """Convenience: segment spans then pack into chunks."""
    records = segment_spans(spans, language=source_language)
    return pack_chunks(
        records,
        source_language=source_language,
        target_language=target_language,
        chunk_size=chunk_size,
    )
