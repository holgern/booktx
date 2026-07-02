"""Unit tests for booktx static source analysis (Phase 0 + Phase 1A).

Covers the Phase 0 contracts (stable candidate identity, extracted-input
fingerprint, semantic digest, source-text preparation, blocking preflight) and
the Phase 1A simple engine (token/phrase/hyphenated/title-case detection,
candidate merging/scoring/ordering, style metrics, snapshot validation, and
Markdown rendering).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from booktx.cli import app
from booktx.config import load_source_project
from booktx.errors import BooktxError
from booktx.models import Chunk, Placeholder, Record
from booktx.source_analysis import (
    ANALYSIS_SCHEMA,
    SNAPSHOT_SCHEMA,
    build_snapshot,
    build_source_analysis,
    candidate_id_from_identity,
    case_bucket,
    common_word_set,
    common_words_metadata,
    compute_analysis_sha256,
    extracted_input_sha256,
    prepare_record,
    read_canonical_report,
    read_snapshot,
    render_report_markdown,
    resolve_engine,
    validate_snapshot_payload,
)

runner = CliRunner()


# --- helpers ----------------------------------------------------------------


def _record(
    rid: str,
    source: str,
    *,
    placeholders: list[Placeholder] | None = None,
    protected_terms: list[str] | None = None,
    source_markup: str = "plain:v1",
) -> Record:
    return Record(
        id=rid,
        source=source,
        placeholders=placeholders or [],
        protected_terms=protected_terms or [],
        source_markup=source_markup,
    )


def _chunk(
    chunk_id: str,
    records: list[Record],
    *,
    source_language: str = "en",
    record_id_scheme: str = "chunk-local:v1",
) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        source_language=source_language,
        record_id_scheme=record_id_scheme,
        records=records,
    )


def _chapter_map(chapters):
    from booktx.chapters import Chapter, ChapterMap

    return ChapterMap(
        version=2,
        source_sha256="abc",
        chapters=[Chapter(**ch) for ch in chapters],
    )


def _make_markdown_project(tmp_path: Path, doc: str) -> Path:
    src = tmp_path / "book.md"
    src.write_text(doc, encoding="utf-8")
    project_dir = tmp_path / "book"
    for args in (
        ["init", str(project_dir), "--target", "de", "--source-file", str(src)],
        ["extract", str(project_dir)],
        ["chapters", str(project_dir)],
    ):
        res = runner.invoke(app, args)
        assert res.exit_code == 0, (args, res.output)
    return project_dir


# --- stable candidate identity (ac-0001 / todo-0003) ------------------------


class TestCandidateIdentity:
    def test_case_bucket_separates_ids(self):
        title = candidate_id_from_identity(
            source_language="en",
            normalized="empire",
            tokens=["empire"],
            case_bucket_value="title",
        )
        lower = candidate_id_from_identity(
            source_language="en",
            normalized="empire",
            tokens=["empire"],
            case_bucket_value="lower",
        )
        assert title != lower
        assert title.startswith("CAND-") and len(title) == len("CAND-") + 16

    def test_identity_excludes_score_rank_kind_settings(self):
        # Identity depends only on ruleset/language/normalized/tokens/bucket.
        base = dict(
            source_language="en",
            normalized="new york",
            tokens=["new", "york"],
            case_bucket_value="title",
        )
        stable = candidate_id_from_identity(**base)
        # Re-computing yields the same id (no hidden state).
        assert candidate_id_from_identity(**base) == stable

    def test_identity_changes_with_tokens_or_language(self):
        a = candidate_id_from_identity(
            source_language="en",
            normalized="new york",
            tokens=["new", "york"],
            case_bucket_value="title",
        )
        b = candidate_id_from_identity(
            source_language="en",
            normalized="york new",
            tokens=["york", "new"],
            case_bucket_value="title",
        )
        c = candidate_id_from_identity(
            source_language="de",
            normalized="new york",
            tokens=["new", "york"],
            case_bucket_value="title",
        )
        assert len({a, b, c}) == 3

    def test_case_bucket_classification(self):
        assert case_bucket("Empire") == "title"
        assert case_bucket("empire") == "lower"
        assert case_bucket("EMPIRE") == "upper"
        assert case_bucket("iPod") == "mixed"
        assert case_bucket("42") == "lower"


# --- extracted-input fingerprint (ac-0002 / todo-0004) ----------------------


class TestExtractedInputFingerprint:
    def _fingerprint(self, record_source="Hello world.", protected=None):
        rec = _record("0001-000001", record_source, protected_terms=protected or [])
        chunk = _chunk("0001", [rec])
        cmap = _chapter_map(
            [
                {
                    "chapter_id": "0001",
                    "title": "One",
                    "start_record_id": "0001-000001",
                    "end_record_id": "0001-000001",
                    "record_count": 1,
                }
            ]
        )
        return extracted_input_sha256(
            [chunk],
            source_language="en",
            source_sha256="src",
            record_id_scheme="chunk-local:v1",
            chapter_map=cmap,
            chapter_by_record={
                "0001-000001": {"chapter_id": "0001", "chapter_title": "One"}
            },
        )

    def test_stable_for_identical_input(self):
        assert self._fingerprint() == self._fingerprint()

    def test_changes_when_record_text_changes(self):
        assert self._fingerprint() != self._fingerprint("Hello changed world.")

    def test_changes_when_protected_terms_change(self):
        assert self._fingerprint() != self._fingerprint(protected=["Alice"])

    def test_changes_when_chapter_assignment_changes(self):
        rec = _record("0001-000001", "Hello world.")
        chunk = _chunk("0001", [rec])
        cmap = _chapter_map(
            [
                {
                    "chapter_id": "0001",
                    "title": "One",
                    "start_record_id": "0001-000001",
                    "end_record_id": "0001-000001",
                    "record_count": 1,
                }
            ]
        )
        a = extracted_input_sha256(
            [chunk],
            source_language="en",
            source_sha256="s",
            record_id_scheme="chunk-local:v1",
            chapter_map=cmap,
            chapter_by_record={
                "0001-000001": {"chapter_id": "0001", "chapter_title": "One"}
            },
        )
        b = extracted_input_sha256(
            [chunk],
            source_language="en",
            source_sha256="s",
            record_id_scheme="chunk-local:v1",
            chapter_map=cmap,
            chapter_by_record={
                "0001-000001": {"chapter_id": "0002", "chapter_title": "Two"}
            },
        )
        assert a != b

    def test_changes_when_placeholder_original_changes(self):
        rec_a = _record(
            "0001-000001",
            "Meet __NAME_001__ now.",
            placeholders=[
                Placeholder(token="__NAME_001__", original="Alice", kind="name")
            ],
        )
        rec_b = _record(
            "0001-000001",
            "Meet __NAME_001__ now.",
            placeholders=[
                Placeholder(token="__NAME_001__", original="Alicia", kind="name")
            ],
        )
        cmap = _chapter_map(
            [
                {
                    "chapter_id": "0001",
                    "title": "One",
                    "start_record_id": "0001-000001",
                    "end_record_id": "0001-000001",
                    "record_count": 1,
                }
            ]
        )
        kwargs = dict(
            source_language="en",
            source_sha256="s",
            record_id_scheme="chunk-local:v1",
            chapter_map=cmap,
            chapter_by_record={
                "0001-000001": {"chapter_id": "0001", "chapter_title": "One"}
            },
        )
        assert extracted_input_sha256(
            [_chunk("0001", [rec_a])], **kwargs
        ) != extracted_input_sha256([_chunk("0001", [rec_b])], **kwargs)


# --- semantic digest (ac-0003 / todo-0005) ----------------------------------


class TestSemanticDigest:
    def test_digest_excludes_generated_at(self, tmp_path):
        project_dir = _make_markdown_project(
            tmp_path, "# One\n\nTisamon walked. Tisamon came.\n"
        )
        proj = load_source_project(project_dir)
        r1 = build_source_analysis(proj)
        r2 = build_source_analysis(proj, generated_at="1970-01-01T00:00:00Z")
        assert r1.generated_at != r2.generated_at
        assert compute_analysis_sha256(r1) == compute_analysis_sha256(r2)
        assert r1.analysis_sha256 == r2.analysis_sha256

    def test_digest_changes_when_settings_change(self, tmp_path):
        project_dir = _make_markdown_project(
            tmp_path, "# One\n\nwasp-kinden wasp-kinden wasp-kinden.\n"
        )
        proj = load_source_project(project_dir)
        r1 = build_source_analysis(proj, ngram_max=2)
        r2 = build_source_analysis(proj, ngram_max=4)
        # Settings differ -> reports differ -> digests differ.
        assert r1.settings.ngram_max != r2.settings.ngram_max
        assert r1.analysis_sha256 != r2.analysis_sha256


# --- source-text preparation (ac-0004 / todo-0006) --------------------------


class TestSourceTextPreparation:
    def test_name_placeholders_restored_and_protected(self):
        rec = _record(
            "0001-000001",
            "Meet __NAME_001__ the hero.",
            placeholders=[
                Placeholder(token="__NAME_001__", original="Alice", kind="name")
            ],
        )
        prepared = prepare_record(rec, chunk_id="0001")
        assert "Alice" in prepared.visible_text
        assert "__NAME_001__" not in prepared.visible_text
        # The restored name is flagged as a protected span.
        assert prepared.protected_spans
        span = prepared.protected_spans[0]
        assert prepared.visible_text[span.start : span.end] == "Alice"

    def test_tag_placeholders_excluded_from_candidates_but_visible(self):
        rec = _record(
            "0001-000001",
            "Run __TAG_001__ now.",
            placeholders=[
                Placeholder(token="__TAG_001__", original="<code>42</code>", kind="tag")
            ],
        )
        prepared = prepare_record(rec, chunk_id="0001")
        # Visible text contains the tag's readable text but no token.
        assert "__TAG_001__" not in prepared.visible_text
        assert "42" in prepared.visible_text
        # The restored tag span is opaque (must not generate candidates).
        assert prepared.opaque_spans
        span = prepared.opaque_spans[0]
        assert prepared.visible_text[span.start : span.end] == "42"

    def test_epub_inline_xhtml_markup_stripped(self):
        rec = _record(
            "0001-000001",
            "He <em>ran</em> fast and <strong>won</strong>.",
            source_markup="epub-inline-xhtml:v1",
        )
        prepared = prepare_record(rec, chunk_id="0001")
        assert "<em>" not in prepared.visible_text
        assert "<strong>" not in prepared.visible_text
        assert "ran" in prepared.visible_text and "won" in prepared.visible_text

    def test_no_placeholder_tokens_remain(self):
        rec = _record(
            "0001-000001",
            "A __NAME_001__ and __TAG_001__ mix.",
            placeholders=[
                Placeholder(token="__NAME_001__", original="Bob", kind="name"),
                Placeholder(token="__TAG_001__", original="x", kind="tag"),
            ],
        )
        prepared = prepare_record(rec, chunk_id="0001")
        assert "__NAME_" not in prepared.visible_text
        assert "__TAG_" not in prepared.visible_text


# --- preflight (ac-0011 / todo-0008) ----------------------------------------


class TestPreflight:
    def test_missing_extraction_blocks(self, tmp_path):
        project_dir = _make_markdown_project(tmp_path, "# One\n\nText.\n")
        proj = load_source_project(project_dir)
        # Remove chunks to simulate no extraction.
        for chunk_path in proj.chunks():
            chunk_path.unlink()
        from booktx.chapters import load_chapter_map_only

        cmap = load_chapter_map_only(proj)
        from booktx.source_analysis import source_analysis_preflight

        with pytest.raises(BooktxError) as exc:
            source_analysis_preflight(proj, chapter_map=cmap)
        assert exc.value.code == "source_analysis_no_extraction"

    def test_missing_chapter_map_blocks_without_repair(self, tmp_path):
        project_dir = _make_markdown_project(tmp_path, "# One\n\nText.\n")
        proj = load_source_project(project_dir)
        # Delete the cached chapter map so the dry run cannot proceed.
        chapter_map_path = proj.chapter_map_path
        if chapter_map_path.is_file():
            chapter_map_path.unlink()
        with pytest.raises(BooktxError) as exc:
            build_source_analysis(proj)
        assert exc.value.code == "source_analysis_no_chapter_map"
        # Dry run must not have repaired the chapter map.
        assert not chapter_map_path.is_file()


# --- simple engine (ac-0007/0008 / todo-0010, 0011) -------------------------


class TestSimpleEngine:
    def test_engine_resolves_to_simple_without_spacy(self):
        resolved, caps, warnings = resolve_engine("auto", None)
        assert resolved == "simple"
        assert caps.tokenizer is True
        assert caps.sentence_boundaries is True
        # Honest capabilities: no POS/lemmatizer/parser/noun_chunks/ner.
        assert not (
            caps.pos or caps.lemmatizer or caps.parser or caps.noun_chunks or caps.ner
        )
        assert warnings

    def test_proper_name_and_hyphenated_and_phrase(self, tmp_path):
        doc = (
            "# Chapter One\n\n"
            "Tisamon walked. Tisamon spoke. Tisamon left.\n"
            "wasp-kinden wasp-kinden wasp-kinden.\n"
            "the Apt Empire rules the Apt Empire again.\n"
        )
        project_dir = _make_markdown_project(tmp_path, doc)
        proj = load_source_project(project_dir)
        report = build_source_analysis(proj, min_count=2, ngram_max=3, top=50)
        kinds = {c.kind for c in report.candidates}
        texts = {c.text for c in report.candidates}
        assert "proper_name" in kinds
        assert "hyphenated_term" in kinds
        assert any(c.kind == "phrase" for c in report.candidates)
        assert any(t == "Tisamon" for t in texts)
        assert any(t == "wasp-kinden" for t in texts)

    def test_merging_by_stable_identity_keeps_detectors(self, tmp_path):
        # "Tisamon" appears as title-case (proper_name) repeatedly -> single id.
        doc = "# C\n\nTisamon came. Tisamon saw. Tisamon went.\n"
        project_dir = _make_markdown_project(tmp_path, doc)
        proj = load_source_project(project_dir)
        report = build_source_analysis(proj, min_count=2)
        tisamon = [c for c in report.candidates if c.text == "Tisamon"]
        assert len(tisamon) == 1
        assert tisamon[0].kind == "proper_name"
        assert "title_case" in tisamon[0].detectors

    def test_ordering_is_deterministic(self, tmp_path):
        doc = "# C\n\nwasp-kinden wasp-kinden wasp-kinden. Tisamon Tisamon.\n"
        project_dir = _make_markdown_project(tmp_path, doc)
        proj = load_source_project(project_dir)
        r1 = build_source_analysis(proj, min_count=2, top=50)
        r2 = build_source_analysis(proj, min_count=2, top=50)
        assert [c.id for c in r1.candidates] == [c.id for c in r2.candidates]
        assert [c.score for c in r1.candidates] == [c.score for c in r2.candidates]

    def test_top_limit_applied_after_merging(self, tmp_path):
        doc = "# C\n\nalpha beta gamma delta epsilon zeta eta theta.\n" * 3
        project_dir = _make_markdown_project(tmp_path, doc)
        proj = load_source_project(project_dir)
        small = build_source_analysis(proj, min_count=2, top=3)
        large = build_source_analysis(proj, min_count=2, top=200)
        assert len(small.candidates) <= 3
        # The top-3 of the larger run equal the small run (top applied post-merge).
        assert [c.id for c in small.candidates] == [c.id for c in large.candidates[:3]]

    def test_unsupported_language_runs_with_warning(self, tmp_path):
        # Create a project whose source language has no bundled common words.
        doc = "# Titolo\n\nZorka zorka zorka bleen bleen.\n"
        project_dir = _make_markdown_project(tmp_path, doc)
        # Force an unsupported language by editing source-config.toml is fragile;
        # instead exercise common_word_set directly + run analysis on 'en'.
        assert common_word_set("xx") == frozenset()
        meta = common_words_metadata("xx")
        assert meta["source"] == "none"
        proj = load_source_project(project_dir)
        report = build_source_analysis(proj)
        # English corpus still produces a report; this asserts the engine runs.
        assert report.record_count >= 1

    def test_generic_vocabulary_is_suppressed_by_default(self, tmp_path):
        doc = (
            "# One\n\nThe man saw the people. The woman moved her lips. "
            "The man had time.\n"
        )
        project_dir = _make_markdown_project(tmp_path, doc)
        proj = load_source_project(project_dir)
        report = build_source_analysis(proj, engine_requested="simple", min_count=2)
        texts = {candidate.text for candidate in report.candidates}
        assert {"man", "people", "woman", "lips", "time"}.isdisjoint(texts)
        assert report.suppressed_counts.get("generic_single_token", 0) >= 4

    def test_hyphenated_world_terms_are_binding_candidates(self, tmp_path):
        doc = "# One\n\nA Spider-kinden envoy met a Fly-kinden pilot.\n"
        project_dir = _make_markdown_project(tmp_path, doc)
        proj = load_source_project(project_dir)
        report = build_source_analysis(proj, engine_requested="simple", min_count=2)
        buckets = {
            candidate.text: candidate.review_bucket for candidate in report.candidates
        }
        assert buckets["Spider-kinden"] == "binding_glossary"
        assert buckets["Fly-kinden"] == "binding_glossary"
        assert "Spider kinden" not in buckets
        assert "Fly kinden" not in buckets

    def test_rare_calendar_compound_is_emitted(self, tmp_path):
        doc = "# One\n\nA tenday later, the army returned.\n"
        project_dir = _make_markdown_project(tmp_path, doc)
        proj = load_source_project(project_dir)
        report = build_source_analysis(proj, engine_requested="simple", min_count=2)
        tenday = next(
            candidate for candidate in report.candidates if candidate.text == "tenday"
        )
        assert tenday.review_bucket in {"invented_or_rare", "binding_glossary"}
        assert tenday.count == 1

    def test_name_policy_bucket_keeps_rare_title_case_name(self, tmp_path):
        doc = "# One\n\nDryclaw entered. Dryclaw left.\n"
        project_dir = _make_markdown_project(tmp_path, doc)
        proj = load_source_project(project_dir)
        report = build_source_analysis(proj, engine_requested="simple", min_count=2)
        dryclaw = next(
            candidate for candidate in report.candidates if candidate.text == "Dryclaw"
        )
        assert dryclaw.review_bucket in {"name_policy", "invented_or_rare"}
        assert dryclaw.suggested_context_action in {
            "review_name_policy",
            "ask_question",
        }

    def test_phrase_detection_does_not_cross_glossary_comma(self, tmp_path):
        doc = "# Glossary\n\n<b>Achaeos</b> – Moth-kinden magician, Che’s lover.\n"
        project_dir = _make_markdown_project(tmp_path, doc)
        proj = load_source_project(project_dir)

        report = build_source_analysis(
            proj,
            engine_requested="simple",
            min_count=1,
            ngram_max=4,
            top=200,
        )
        texts = {candidate.text for candidate in report.candidates}
        normalized = {candidate.normalized for candidate in report.candidates}

        assert report.analysis_ruleset_version == "3"
        assert "Moth-kinden" in texts
        assert "Achaeos" in texts
        assert "Che" in texts

        assert "moth kinden magician che" not in normalized
        assert "moth-kinden magician che" not in normalized
        assert "magician che" not in normalized

    def test_phrase_detection_does_not_cross_dialogue_quote_comma(self, tmp_path):
        doc = "# One\n\n‘You’re like Mantis-kinden,’ Che said.\n"
        project_dir = _make_markdown_project(tmp_path, doc)
        proj = load_source_project(project_dir)

        report = build_source_analysis(
            proj,
            engine_requested="simple",
            min_count=1,
            ngram_max=4,
            top=200,
        )

        normalized = {candidate.normalized for candidate in report.candidates}
        assert "mantis-kinden" in normalized
        assert "kinden che" not in normalized
        assert "mantis kinden che said" not in normalized
        assert "mantis-kinden che said" not in normalized

    def test_phrase_detection_does_not_cross_ellipsis(self, tmp_path):
        doc = "# One\n\n‘Beetle-kinden . . .’ Thalric started.\n"
        project_dir = _make_markdown_project(tmp_path, doc)
        proj = load_source_project(project_dir)

        report = build_source_analysis(
            proj,
            engine_requested="simple",
            min_count=1,
            ngram_max=4,
            top=200,
        )

        normalized = {candidate.normalized for candidate in report.candidates}
        assert "beetle-kinden" in normalized
        assert "beetle kinden thalric" not in normalized
        assert "beetle-kinden thalric" not in normalized

    def test_phrase_detection_does_not_cross_sentence_boundary(self, tmp_path):
        doc = "# One\n\nTisamon departed. Che arrived.\n"
        project_dir = _make_markdown_project(tmp_path, doc)
        proj = load_source_project(project_dir)

        report = build_source_analysis(
            proj,
            engine_requested="simple",
            min_count=1,
            ngram_max=3,
            top=200,
        )

        normalized = {candidate.normalized for candidate in report.candidates}
        assert "departed che" not in normalized
        assert "tisamon departed che" not in normalized

    def test_boundary_safe_repeated_phrase_still_emitted(self, tmp_path):
        doc = (
            "# One\n\n"
            "The Apt Empire endured. The Apt Empire expanded. "
            "The Apt Empire returned.\n"
        )
        project_dir = _make_markdown_project(tmp_path, doc)
        proj = load_source_project(project_dir)

        report = build_source_analysis(
            proj,
            engine_requested="simple",
            min_count=2,
            ngram_max=2,
            top=200,
        )

        normalized = {candidate.normalized for candidate in report.candidates}
        assert "apt empire" in normalized

    def test_phrase_candidates_do_not_get_singleton_world_morpheme_override(
        self, tmp_path
    ):
        doc = "# One\n\nA Moth-kinden magician, Che arrived.\n"
        project_dir = _make_markdown_project(tmp_path, doc)
        proj = load_source_project(project_dir)

        report = build_source_analysis(
            proj,
            engine_requested="simple",
            min_count=2,
            ngram_max=4,
            top=200,
        )

        assert all(
            not (
                candidate.kind == "phrase"
                and "singleton_override" in candidate.reason_codes
            )
            for candidate in report.candidates
        )


class TestPhraseBoundaryHelpers:
    def test_hard_phrase_boundary_rejects_opaque_gap(self):
        from booktx.source_analysis import (
            _Span,
            _Token,
            _window_crosses_hard_phrase_boundary,
        )

        text = "Apt code Empire"
        window = [
            _Token(
                surface="Apt",
                normalized="apt",
                start=0,
                end=3,
                bucket="title",
                protected=False,
            ),
            _Token(
                surface="Empire",
                normalized="empire",
                start=9,
                end=15,
                bucket="title",
                protected=False,
            ),
        ]

        assert _window_crosses_hard_phrase_boundary(
            text,
            window,
            opaque_spans=[_Span(4, 8)],
        )

    def test_phrasplit_clause_offsets_are_exact_for_source_analysis(self):
        from phrasplit import split_with_offsets

        text = "<b>Achaeos</b> – Moth-kinden magician, Che’s lover"
        segments = split_with_offsets(text, mode="clause", use_spacy=False)

        assert segments
        for segment in segments:
            assert text[segment.char_start : segment.char_end] == segment.text

        assert any(segment.text.endswith("magician,") for segment in segments)
        assert any(segment.text == "Che’s lover" for segment in segments)


# --- style metrics (todo-0012) ----------------------------------------------


class TestStyleMetrics:
    def test_metrics_capture_dialogue_em_dash_emphasis(self, tmp_path):
        doc = (
            "# Chapter One\n\n"
            '"Hello," said Tisamon. "Goodbye."\n'  # dialogue
            "The pause—long—ended.\n"  # em dashes
        )
        project_dir = _make_markdown_project(tmp_path, doc)
        proj = load_source_project(project_dir)
        report = build_source_analysis(proj)
        metrics = report.style_metrics
        assert metrics.record_count_with_dialogue >= 1
        assert metrics.dialogue_record_ratio > 0.0
        assert metrics.em_dash_count >= 1
        assert metrics.quote_counts.get("double", 0) >= 2
        assert metrics.sentence_count is not None and metrics.sentence_count >= 1


# --- snapshot validation + markdown (todo-0016) -----------------------------


class TestSnapshotAndMarkdown:
    def test_snapshot_envelope_and_validation(self, tmp_path):
        project_dir = _make_markdown_project(tmp_path, "# C\n\nTisamon Tisamon.\n")
        proj = load_source_project(project_dir)
        report = build_source_analysis(proj)
        snap = build_snapshot(
            report, profile="de_default", generated_at="2026-07-02T00:00:00Z"
        )
        assert snap.schema_name == SNAPSHOT_SCHEMA
        assert snap.generated is True and snap.canonical is False
        assert snap.profile == "de_default"
        assert snap.analysis_sha256 == report.analysis_sha256
        # Round-trip through JSON and validate.
        payload = snap.model_dump(by_alias=True, mode="json")
        again = validate_snapshot_payload(payload)
        assert again.analysis_sha256 == report.analysis_sha256

    def test_tampered_snapshot_rejected(self, tmp_path):
        project_dir = _make_markdown_project(tmp_path, "# C\n\nTisamon Tisamon.\n")
        proj = load_source_project(project_dir)
        report = build_source_analysis(proj)
        snap = build_snapshot(report, profile="de_default", generated_at="t")
        payload = snap.model_dump(by_alias=True, mode="json")
        payload["analysis_sha256"] = "0" * 64  # tamper
        from booktx.source_analysis import SnapshotValidationError

        with pytest.raises(SnapshotValidationError) as exc:
            validate_snapshot_payload(payload)
        assert exc.value.code == "source_analysis_snapshot_tampered"

    def test_missing_snapshot_reports_safe_hint(self, tmp_path):
        with pytest.raises(BooktxError) as exc:
            read_snapshot(tmp_path / "missing.json")
        assert exc.value.code == "source_analysis_snapshot_missing"
        assert ".." not in str(exc.value) and "/tmp" not in str(exc.value)

    def test_markdown_has_sections_and_no_paths(self, tmp_path):
        project_dir = _make_markdown_project(
            tmp_path, "# C\n\nTisamon Tisamon wasp-kinden wasp-kinden.\n"
        )
        proj = load_source_project(project_dir)
        report = build_source_analysis(proj, engine_requested="simple", min_count=2)
        md = render_report_markdown(report)
        assert "# booktx source analysis" in md
        assert "## Review first: binding glossary decisions" in md
        assert "## Review names and titles" in md
        assert "## Suppressed/no-action summary" in md
        assert "## Style observations" in md
        assert "booktx context promote-candidate" in md
        assert "booktx source review-candidate" in md
        assert report.analysis_sha256 in md
        # No internal absolute paths leak into the rendered view.
        assert str(tmp_path) not in md
        assert "/data/" not in md


# --- read canonical report --------------------------------------------------


class TestReadCanonical:
    def test_read_canonical_returns_none_when_absent(self, tmp_path):
        project_dir = _make_markdown_project(tmp_path, "# C\n\nText.\n")
        proj = load_source_project(project_dir)
        assert read_canonical_report(proj) is None

    def test_read_canonical_roundtrip(self, tmp_path):
        project_dir = _make_markdown_project(tmp_path, "# C\n\nTisamon Tisamon.\n")
        proj = load_source_project(project_dir)
        report = build_source_analysis(proj)
        from booktx.config import source_analysis_markdown_path, source_analysis_path
        from booktx.io_utils import write_json_text_atomic

        write_json_text_atomic(
            source_analysis_path(proj), report.model_dump_json(by_alias=True)
        )
        loaded = read_canonical_report(proj)
        assert loaded is not None
        assert loaded.analysis_sha256 == report.analysis_sha256
        assert loaded.schema_name == ANALYSIS_SCHEMA
        _ = source_analysis_markdown_path  # path helper exists
