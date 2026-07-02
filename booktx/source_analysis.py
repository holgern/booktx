"""Deterministic source-level analysis for booktx.

This module inspects the *extracted* source representation and proposes likely
important words, names, invented terms, repeated phrases, and style signals
*before* translation starts. Its output is **evidence**, never policy:

* ``context.json`` remains canonical for profile-local translation decisions.
* ``.booktx/names.json`` is never mutated by analysis.
* Generated reports never contain approved translation decisions.

The dependency-free simple engine is always available. Optional spaCy
enrichment is loaded lazily and every linguistic detector is capability-gated.

The JSON report (``SourceAnalysisReport``) is authoritative; the Markdown view
(``render_report_markdown``) is a generated readable rendering of that JSON.
"""

# ruff: noqa: E501

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from functools import cache
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from booktx.epub_inline_xhtml import strip_inline_xhtml
from booktx.errors import BooktxError, _err

if TYPE_CHECKING:
    from booktx.chapters import ChapterMap
    from booktx.config import Project
    from booktx.models import Chunk, Record

__all__ = [
    "IDENTITY_RULESET_VERSION",
    "ANALYSIS_RULESET_VERSION",
    "COMMON_WORDS_VERSION",
    "ANALYSIS_SCHEMA",
    "SNAPSHOT_SCHEMA",
    "AnalysisCapabilities",
    "SourceAnalysisSettings",
    "SourceAnalysisOccurrence",
    "SourceCandidate",
    "SourceStyleMetrics",
    "SourceAnalysisReport",
    "SourceAnalysisSnapshot",
    "PreparedRecord",
    "SnapshotRead",
    "SnapshotValidationError",
    "prepare_record",
    "prepare_records",
    "candidate_id_from_identity",
    "extracted_input_sha256",
    "compute_analysis_sha256",
    "source_analysis_preflight",
    "build_source_analysis",
    "build_snapshot",
    "validate_snapshot_payload",
    "read_snapshot",
    "read_canonical_report",
    "render_report_markdown",
    "common_word_set",
    "common_words_metadata",
    "resolve_engine",
    "CaseBucket",
]


# --- Ruleset / schema versions ----------------------------------------------

#: Identity-ruleset version. Bumping this changes every candidate id and requires
#: an explicit migration. Candidate identity must never depend on score, rank,
#: detector kind, spaCy model, occurrence count, or analysis settings.
IDENTITY_RULESET_VERSION = "1"

#: Analysis-ruleset version. Covers scoring constants, detector behaviour,
#: normalization, phrase-boundary/overlap rules, and bundled common-word data.
ANALYSIS_RULESET_VERSION = "3"

#: Version stamp of the bundled common-word lists (feeds the analysis ruleset).
COMMON_WORDS_VERSION = "2026.07"

ANALYSIS_SCHEMA: Literal["booktx.source-analysis.v1"] = "booktx.source-analysis.v1"
SNAPSHOT_SCHEMA: Literal["booktx.source-analysis-snapshot.v1"] = (
    "booktx.source-analysis-snapshot.v1"
)


# --- Scoring constants (owned by ANALYSIS_RULESET_VERSION) -------------------

_PHRASE_BONUS = 1.5
_PHRASE_KINDS = frozenset({"phrase", "title_candidate", "hyphenated_term"})
_COMMON_PENALTY = 0.2
_SNIPPET_WIDTH = 120
_MAX_EXAMPLES_PER_CANDIDATE = 3
_DEFAULT_MAX_PER_BUCKET = 80
_DEFAULT_MIN_RISK_SCORE = 3.0
_DEFAULT_WORLD_MORPHEMES = ("kinden", "opter")
_DEFAULT_INCLUDE_PATTERNS = (r"(?i)^ten-?day$", r"(?i).*-kinden$")
_DEFAULT_EXCLUDE_PATTERNS: tuple[str, ...] = ()

SourceReviewBucket = Literal[
    "binding_glossary",
    "name_policy",
    "invented_or_rare",
    "domain_phrase",
    "style_signal",
    "maybe",
    "no_action",
]


# --- Data models ------------------------------------------------------------


class AnalysisCapabilities(BaseModel):
    """Resolved engine capabilities, recorded independently of the request."""

    model_config = ConfigDict(extra="forbid")

    tokenizer: bool
    sentence_boundaries: bool
    lemmatizer: bool
    pos: bool
    parser: bool
    noun_chunks: bool
    ner: bool


class SourceAnalysisSettings(BaseModel):
    """Effective analysis settings used to produce a report."""

    model_config = ConfigDict(extra="forbid")

    engine_requested: Literal["auto", "spacy", "simple"]
    engine_resolved: Literal["spacy", "simple"]
    spacy_model: str | None = None
    spacy_version: str | None = None
    model_version: str | None = None
    min_count: int
    ngram_max: int
    top: int
    include_common: bool


class SourceAnalysisOccurrence(BaseModel):
    """One bounded evidence occurrence of a candidate."""

    model_config = ConfigDict(extra="forbid")

    record_id: str
    chapter_id: str | None = None
    chapter_title: str | None = None
    visible_text: str
    snippet: str


class SourceCandidate(BaseModel):
    """One merged analysis candidate with a stable content-derived id."""

    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    normalized: str
    surface_forms: list[str] = Field(default_factory=list)
    lemma: str | None = None
    kind: Literal[
        "word",
        "phrase",
        "proper_name",
        "place_name",
        "hyphenated_term",
        "invented_term",
        "title_candidate",
    ]
    detectors: list[str] = Field(default_factory=list)
    category_hint: str | None = None
    count: int
    record_frequency: int
    chapter_frequency: int
    score: float
    uncommon_score: float
    first_record_id: str | None = None
    examples: list[SourceAnalysisOccurrence] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    reason: str = ""
    already_protected: bool = False
    suggested_context_action: Literal[
        "none",
        "ask_question",
        "add_advisory_glossary",
        "review_name_policy",
        "review_for_binding_glossary",
    ] = "none"
    review_bucket: SourceReviewBucket = "maybe"
    risk_score: float = 0.0
    genericity_score: float = 0.0
    rarity_score: float = 0.0
    morphology_flags: list[str] = Field(default_factory=list)
    suppression_reason: str | None = None
    canonical_surface: str | None = None
    source_variants: list[str] = Field(default_factory=list)
    token_count: int = 0
    external_frequency: float | None = None


class SourceStyleMetrics(BaseModel):
    """Structured style observations (not synthetic candidates)."""

    model_config = ConfigDict(extra="forbid")

    record_count_with_dialogue: int
    dialogue_record_ratio: float
    quote_counts: dict[str, int] = Field(default_factory=dict)
    em_dash_count: int
    emphasis_count: int
    sentence_count: int | None = None
    average_sentence_words: float | None = None
    capability_warnings: list[str] = Field(default_factory=list)


class SourceAnalysisReport(BaseModel):
    """Authoritative generated source-analysis evidence (JSON)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_name: Literal["booktx.source-analysis.v1"] = Field(
        default="booktx.source-analysis.v1", alias="schema"
    )
    identity_ruleset_version: str
    analysis_ruleset_version: str
    source_sha256: str
    extracted_input_sha256: str
    chapter_map_sha256: str
    analysis_sha256: str
    source_language: str
    generated_at: str
    settings: SourceAnalysisSettings
    capabilities: AnalysisCapabilities
    record_count: int
    chapter_count: int
    candidates: list[SourceCandidate]
    style_metrics: SourceStyleMetrics
    warnings: list[str] = Field(default_factory=list)
    suppressed_counts: dict[str, int] = Field(default_factory=dict)


class SourceAnalysisSnapshot(BaseModel):
    """Profile-local snapshot envelope embedding the canonical report."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_name: Literal["booktx.source-analysis-snapshot.v1"] = Field(
        default="booktx.source-analysis-snapshot.v1", alias="schema"
    )
    generated: Literal[True] = True
    canonical: Literal[False] = False
    profile: str
    snapshot_generated_at: str
    source_sha256: str
    extracted_input_sha256: str
    analysis_sha256: str
    report: SourceAnalysisReport


# --- Bundled common-word lists ----------------------------------------------
#
# Small curated lists of very frequent function words / common words for the
# initially supported language (English). These are intentionally tiny: they
# only need to suppress obvious noise. Unsupported languages still run using
# corpus-internal signals and emit a warning.

_COMMON_WORDS_EN = frozenset(
    """
    a an the and or but if then else of to in on at by for with from into onto
    upon over under above below between among through during before after as is
    are was were be been being am do does did doing have has had having i you he
    she it we they me him her us them my your his its our their this that these
    those there here not no nor so too very can could shall should will would may
    might must ought about above across against along although among any both each
    few more most other some such only own same than that them then thence there
    these they thine this those thou though three thy til tis unto was wept were
    what when where which while who whom why will with within without yet you your
    hers ourselves yourself yourselves themselves itself myself himself everything
    nothing someone anyone everyone none one two three four five six seven eight
    nine ten first second third new old good bad great little big long short high
    low own all another such
    """.split()
)


def common_word_set(language: str) -> frozenset[str]:
    """Return the bundled common-word set for ``language`` (empty if unknown)."""
    lang = (language or "").lower().split("-")[0]
    if lang == "en":
        return _COMMON_WORDS_EN
    return frozenset()


def common_words_metadata(language: str) -> dict[str, str]:
    """Return source/license/version metadata for the bundled common-word list."""
    lang = (language or "").lower().split("-")[0]
    if lang == "en":
        return {
            "source": "booktx-curated",
            "license": "CC0-1.0 (booktx-curated public domain)",
            "version": COMMON_WORDS_VERSION,
            "language": "en",
        }
    return {
        "source": "none",
        "license": "n/a",
        "version": COMMON_WORDS_VERSION,
        "language": lang,
    }


@cache
def _bundled_generic_lemmas(language: str) -> frozenset[str]:
    """Return bundled generic lemmas for ``language``.

    The file is package data because it is part of the ruleset, not user state.
    Missing bundled data is treated as a code/package error for supported
    languages so the failure is explicit instead of silently disabling the
    suppression signal.
    """

    lang = (language or "").lower().split("-")[0]
    if lang != "en":
        return frozenset()
    path = Path(__file__).with_name("data") / f"common_lemmas_{lang}.txt"
    try:
        raw = path.read_text("utf-8")
    except OSError as exc:  # pragma: no cover - packaging/runtime guard
        raise _err(
            "source_analysis_common_lemmas_missing",
            f"bundled generic-lemma data is unavailable: {path.name}",
        ) from exc
    values = [line.strip().casefold() for line in raw.splitlines()]
    return frozenset(value for value in values if value and not value.startswith("#"))


@dataclass(frozen=True)
class _SourceAnalysisRuntimeConfig:
    include_singletons: bool
    max_per_bucket: int
    min_risk_score: float
    world_morphemes: tuple[str, ...]
    include_patterns: tuple[re.Pattern[str], ...]
    exclude_patterns: tuple[re.Pattern[str], ...]
    generic_lemmas: frozenset[str]


def _compile_patterns(values: list[str], *, label: str) -> tuple[re.Pattern[str], ...]:
    compiled: list[re.Pattern[str]] = []
    for value in values:
        try:
            compiled.append(re.compile(value))
        except re.error as exc:
            raise _err(
                "source_analysis_bad_config",
                f"invalid source-analysis {label} regex {value!r}: {exc}",
            ) from exc
    return tuple(compiled)


def _runtime_source_analysis_config(
    project: Project, source_language: str
) -> _SourceAnalysisRuntimeConfig:
    """Resolve project-local source-analysis config with deterministic defaults."""

    configured = project.source_config.source_analysis
    patterns = configured.patterns if configured is not None else None
    lemmas = configured.generic_lemmas if configured is not None else None
    world_morphemes = tuple(
        item.casefold()
        for item in (
            patterns.world_morphemes
            if patterns is not None
            else _DEFAULT_WORLD_MORPHEMES
        )
        if item.strip()
    )
    include_regex = list(
        patterns.include_regex if patterns is not None else _DEFAULT_INCLUDE_PATTERNS
    )
    exclude_regex = list(
        patterns.exclude_regex if patterns is not None else _DEFAULT_EXCLUDE_PATTERNS
    )
    extra_lemmas = frozenset(
        item.casefold() for item in (lemmas.extra if lemmas is not None else []) if item
    )
    return _SourceAnalysisRuntimeConfig(
        include_singletons=(
            configured.include_singletons if configured is not None else True
        ),
        max_per_bucket=(
            configured.max_per_bucket
            if configured is not None
            else _DEFAULT_MAX_PER_BUCKET
        ),
        min_risk_score=(
            configured.min_risk_score
            if configured is not None
            else _DEFAULT_MIN_RISK_SCORE
        ),
        world_morphemes=world_morphemes,
        include_patterns=_compile_patterns(include_regex, label="include"),
        exclude_patterns=_compile_patterns(exclude_regex, label="exclude"),
        generic_lemmas=_bundled_generic_lemmas(source_language) | extra_lemmas,
    )


# --- Canonical hashing helpers ----------------------------------------------


def _canonical_json(payload: object) -> str:
    """Serialize to deterministic compact JSON with sorted keys."""
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _sha256_text(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


# --- Source-text preparation ------------------------------------------------

# Alphabetic word runs (Unicode). Digits and punctuation are separators for
# candidate purposes; hyphenated compounds are detected separately.
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
# Hyphenated compound: two or more alphabetic runs joined by ASCII hyphens.
_HYPHENATED_RE = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)+", re.UNICODE)
_HARD_PHRASE_GAP_RE = re.compile(r"(?:\.\s*){2,}|[,;:!?…]|[—–]|[\"“”‘’]")
_PHRASE_TOKEN_RE = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)+|[^\W\d_]+", re.UNICODE)
# Matches a placeholder token OR, for EPUB records, a residual inline XHTML tag.
_TOKEN_OR_XHTML_RE = re.compile(r"__(?:NAME|TAG)_(\d+)__|<[^>]*>")
_TOKEN_ONLY_RE = re.compile(r"__(?:NAME|TAG)_(\d+)__")

#: Case-semantic buckets used for stable candidate identity.
CaseBucket = Literal["title", "upper", "lower", "mixed"]


@dataclass(slots=True)
class _Span:
    start: int
    end: int


@dataclass(slots=True)
class PreparedRecord:
    """One record reduced to clean visible analysis text plus span metadata.

    ``visible_text`` has name placeholders restored to visible names, tag
    placeholders restored to their visible text (but flagged opaque), and EPUB
    inline XHTML tags stripped. ``opaque_spans`` mark code-like spans that must
    not generate candidates; ``protected_spans`` mark text restored from a
    protected name (still analyzable, flagged ``already_protected``).
    """

    record_id: str
    chunk_id: str
    source_markup: str
    chapter_id: str | None
    chapter_title: str | None
    visible_text: str
    opaque_spans: list[_Span] = field(default_factory=list)
    protected_spans: list[_Span] = field(default_factory=list)


def _strip_tag_visible(original: str) -> str:
    """Return the human-readable text of a tag placeholder original."""
    return strip_inline_xhtml(original)


def prepare_record(
    record: Record,
    *,
    chunk_id: str,
    chapter_id: str | None = None,
    chapter_title: str | None = None,
) -> PreparedRecord:
    """Reduce one extracted record to clean visible analysis text.

    Steps (Phase 0 contract):

    1. NAME placeholders are restored to their original visible names and the
       resulting span is marked protected (still analyzable, flagged later).
    2. TAG placeholders are restored to their visible text but the span is marked
       opaque so code-like content never becomes a candidate.
    3. For ``epub-inline-xhtml:v1`` records, residual inline XHTML tags are
       stripped (markup removed, inner text kept as prose).
    4. Placeholder tokens and markup therefore never appear in tokenization.
    """
    source = record.source
    is_epub = record.source_markup == "epub-inline-xhtml:v1"
    name_by_token = {
        ph.token: ph.original for ph in record.placeholders if ph.kind == "name"
    }
    tag_by_token = {
        ph.token: ph.original for ph in record.placeholders if ph.kind == "tag"
    }

    pattern = _TOKEN_OR_XHTML_RE if is_epub else _TOKEN_ONLY_RE
    out: list[str] = []
    opaque: list[_Span] = []
    protected: list[_Span] = []
    pos = 0
    for match in pattern.finditer(source):
        # Append the literal text before this match verbatim.
        if match.start() > pos:
            out.append(source[pos : match.start()])
        token = match.group(0)
        if token.startswith("__NAME_"):
            original = name_by_token.get(token, token)
            start = sum(len(part) for part in out)
            out.append(original)
            end = start + len(original)
            if original:
                protected.append(_Span(start, end))
        elif token.startswith("__TAG_"):
            original = tag_by_token.get(token, token)
            visible = _strip_tag_visible(original)
            start = sum(len(part) for part in out)
            out.append(visible)
            end = start + len(visible)
            if visible:
                opaque.append(_Span(start, end))
        else:
            # Residual inline XHTML tag: markup removed, no text contributed.
            pass
        pos = match.end()
    if pos < len(source):
        out.append(source[pos:])

    visible_text = "".join(out)
    return PreparedRecord(
        record_id=record.id,
        chunk_id=chunk_id,
        source_markup=record.source_markup,
        chapter_id=chapter_id,
        chapter_title=chapter_title,
        visible_text=visible_text,
        opaque_spans=opaque,
        protected_spans=protected,
    )


def prepare_records(
    chunks: list[Chunk],
    chapter_by_record: dict[str, dict[str, str | None]],
) -> list[PreparedRecord]:
    """Prepare every record in chunk order, attaching chapter metadata."""
    out: list[PreparedRecord] = []
    for chunk in chunks:
        for record in chunk.records:
            meta = chapter_by_record.get(record.id, {})
            out.append(
                prepare_record(
                    record,
                    chunk_id=chunk.chunk_id,
                    chapter_id=meta.get("chapter_id"),
                    chapter_title=meta.get("chapter_title"),
                )
            )
    return out


# --- Normalization + case bucket --------------------------------------------


def normalize_token(text: str) -> str:
    """Normalize a surface token for identity/frequency grouping."""
    return " ".join(text.casefold().split())


def case_bucket(surface: str) -> CaseBucket:
    """Classify the dominant case pattern of a surface form.

    Two forms that differ only by this bucket (e.g. ``Empire`` vs ``empire``)
    get separate candidate ids when their uses are meaningfully distinct.
    """
    letters = [ch for ch in surface if ch.isalpha()]
    if not letters:
        return "lower"
    upper = sum(1 for ch in letters if ch.isupper())
    if upper == len(letters):
        return "upper"
    if upper == 1 and letters[0].isupper():
        return "title"
    if upper == 0:
        return "lower"
    return "mixed"


def _phrase_bucket(surfaces: list[str]) -> CaseBucket:
    """Case bucket for a multi-token phrase (title if every token is title/upper)."""
    if not surfaces:
        return "lower"
    buckets = {case_bucket(s) for s in surfaces}
    if buckets <= {"title", "upper"}:
        return "title"
    if buckets == {"lower"}:
        return "lower"
    return "mixed"


# --- Stable candidate identity ----------------------------------------------


def candidate_id_from_identity(
    *,
    source_language: str,
    normalized: str,
    tokens: list[str],
    case_bucket_value: CaseBucket,
) -> str:
    """Return a stable content-derived candidate id.

    The identity payload is canonical JSON over (identity ruleset version,
    source language, normalized text, token boundaries, case-semantic bucket)
    and deliberately EXCLUDES score, rank, detector kind, spaCy model,
    occurrence count, and analysis settings.
    """
    payload = {
        "identity_ruleset_version": IDENTITY_RULESET_VERSION,
        "source_language": source_language,
        "normalized": normalized,
        "token_count": len(tokens),
        "tokens": tokens,
        "case_bucket": case_bucket_value,
    }
    digest = sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return "CAND-" + digest[:16].upper()


# --- Extracted-input fingerprint --------------------------------------------


def extracted_input_sha256(
    chunks: list[Chunk],
    *,
    source_language: str,
    source_sha256: str,
    record_id_scheme: str,
    chapter_map: ChapterMap,
    chapter_by_record: dict[str, dict[str, str | None]],
) -> str:
    """Fingerprint the actual extracted representation, not just source bytes.

    Changes when records, placeholders, protected terms, chapter assignments,
    record-id scheme, chapter-map version, or segmentation metadata change,
    even when the raw source bytes are unchanged.
    """
    records_payload: list[dict[str, object]] = []
    for chunk in chunks:
        for record in chunk.records:
            meta = chapter_by_record.get(record.id, {})
            records_payload.append(
                {
                    "id": record.id,
                    "chunk_id": chunk.chunk_id,
                    "source": record.source,
                    "source_markup": record.source_markup,
                    "span_index": record.span_index,
                    "span_record_index": record.span_record_index,
                    "protected_terms": list(record.protected_terms),
                    "placeholders": [
                        {"token": ph.token, "original": ph.original, "kind": ph.kind}
                        for ph in record.placeholders
                    ],
                    "chapter_id": meta.get("chapter_id"),
                    "chapter_title": meta.get("chapter_title"),
                }
            )
    payload = {
        "source_language": source_language,
        "source_sha256": source_sha256,
        "record_id_scheme": record_id_scheme,
        "chapter_map_version": chapter_map.version,
        "chapter_map_source_sha256": chapter_map.source_sha256,
        "records": records_payload,
    }
    return _sha256_text(_canonical_json(payload))


def _chapter_map_sha256(chapter_map: ChapterMap) -> str:
    """Stable digest of the chapter-map structure (ids/titles/ranges)."""
    payload = {
        "version": chapter_map.version,
        "source_sha256": chapter_map.source_sha256,
        "chapters": [
            {
                "chapter_id": ch.chapter_id,
                "title": ch.title,
                "start_record_id": ch.start_record_id,
                "end_record_id": ch.end_record_id,
                "record_count": ch.record_count,
            }
            for ch in chapter_map.chapters
        ],
    }
    return _sha256_text(_canonical_json(payload))


# --- Semantic digest --------------------------------------------------------


_EXCLUDED_FROM_DIGEST = {"analysis_sha256", "generated_at"}


def compute_analysis_sha256(report: SourceAnalysisReport) -> str:
    """Semantic digest over canonical report content.

    Excludes ``analysis_sha256`` itself, ``generated_at``, snapshot envelope
    metadata, and Markdown. Deterministic for unchanged input, rulesets,
    capabilities, model version, and settings.
    """
    payload = report.model_dump(by_alias=True, mode="json")
    for key in _EXCLUDED_FROM_DIGEST:
        payload.pop(key, None)
    return _sha256_text(_canonical_json(payload))


# --- Engine resolution ------------------------------------------------------


@dataclass(frozen=True)
class _EngineRuntime:
    resolved: Literal["spacy", "simple"]
    capabilities: AnalysisCapabilities
    warnings: list[str]
    nlp: Any | None = None
    model_name: str | None = None
    spacy_version: str | None = None
    model_version: str | None = None


_DEFAULT_SPACY_MODELS = {
    "de": "de_core_news_sm",
    "en": "en_core_web_sm",
    "es": "es_core_news_sm",
    "fr": "fr_core_news_sm",
}


def _pipeline_capabilities(nlp: Any) -> AnalysisCapabilities:
    pipes = set(getattr(nlp, "pipe_names", ()))
    parser = "parser" in pipes
    return AnalysisCapabilities(
        tokenizer=getattr(nlp, "tokenizer", None) is not None,
        sentence_boundaries=bool(
            parser or pipes.intersection({"senter", "sentencizer"})
        ),
        lemmatizer="lemmatizer" in pipes,
        pos=bool(pipes.intersection({"tagger", "morphologizer"})),
        parser=parser,
        noun_chunks=parser,
        ner="ner" in pipes,
    )


def _resolve_engine_runtime(
    engine_requested: str,
    spacy_model: str | None,
    source_language: str,
) -> _EngineRuntime:
    simple_caps = AnalysisCapabilities(
        tokenizer=True,
        sentence_boundaries=True,
        lemmatizer=False,
        pos=False,
        parser=False,
        noun_chunks=False,
        ner=False,
    )
    simple_warning = "sentence_boundaries: heuristic splitter (no linguistic model)"
    if engine_requested == "simple":
        return _EngineRuntime("simple", simple_caps, [simple_warning])
    try:
        import spacy
    except ImportError as exc:
        if engine_requested == "spacy" or spacy_model:
            raise _err(
                "source_analysis_spacy_unavailable",
                "spaCy analysis was requested but spaCy is not installed; "
                "install booktx[analysis] or use --engine simple",
            ) from exc
        return _EngineRuntime(
            "simple",
            simple_caps,
            ["spaCy is not installed; using the simple engine.", simple_warning],
        )

    language = (source_language or "").lower().split("-")[0]
    model_name = spacy_model or _DEFAULT_SPACY_MODELS.get(language)
    nlp: Any | None = None
    warnings: list[str] = []
    if model_name:
        try:
            nlp = spacy.load(model_name)
        except (OSError, ImportError) as exc:
            if spacy_model:
                raise _err(
                    "source_analysis_spacy_model_unavailable",
                    f"explicit spaCy model {model_name!r} could not be loaded: {exc}",
                ) from exc
            warnings.append(
                f"configured spaCy model {model_name!r} is unavailable; "
                "using a blank language pipeline"
            )
    if nlp is not None:
        model_language = str(getattr(nlp, "lang", "")).lower().split("-")[0]
        if model_language and language and model_language != language:
            message = (
                f"spaCy model language {model_language!r} does not match "
                f"source language {language!r}"
            )
            if spacy_model:
                raise _err("source_analysis_spacy_model_mismatch", message)
            warnings.append(message + "; using a blank language pipeline")
            nlp = None
    if nlp is None:
        try:
            nlp = spacy.blank(language)
        except (ImportError, ValueError) as exc:
            if engine_requested == "spacy":
                raise _err(
                    "source_analysis_spacy_language_unsupported",
                    f"spaCy has no pipeline for source language {source_language!r}",
                ) from exc
            return _EngineRuntime(
                "simple",
                simple_caps,
                warnings
                + [
                    f"spaCy does not support source language {source_language!r}; "
                    "using the simple engine.",
                    simple_warning,
                ],
            )
        if "sentencizer" not in set(nlp.pipe_names):
            nlp.add_pipe("sentencizer")
        model_name = f"blank:{language}"
    caps = _pipeline_capabilities(nlp)
    meta = getattr(nlp, "meta", {}) or {}
    return _EngineRuntime(
        "spacy",
        caps,
        warnings,
        nlp=nlp,
        model_name=model_name,
        spacy_version=str(getattr(spacy, "__version__", "")) or None,
        model_version=str(meta.get("version") or "") or None,
    )


def resolve_engine(
    engine_requested: str,
    spacy_model: str | None,
) -> tuple[Literal["spacy", "simple"], AnalysisCapabilities, list[str]]:
    """Compatibility resolver.

    The historical two-argument ``auto`` probe remains dependency-independent;
    report construction uses the language-aware runtime resolver.
    """
    if engine_requested == "auto" and spacy_model is None:
        caps = AnalysisCapabilities(
            tokenizer=True,
            sentence_boundaries=True,
            lemmatizer=False,
            pos=False,
            parser=False,
            noun_chunks=False,
            ner=False,
        )
        return (
            "simple",
            caps,
            ["sentence_boundaries: heuristic splitter (no linguistic model)"],
        )
    runtime = _resolve_engine_runtime(engine_requested, spacy_model, "en")
    return runtime.resolved, runtime.capabilities, runtime.warnings


# --- Tokenization of prepared text ------------------------------------------


@dataclass(slots=True)
class _Token:
    surface: str
    normalized: str
    start: int
    end: int
    bucket: CaseBucket
    protected: bool


def _in_any_span(pos: int, spans: list[_Span]) -> bool:
    return any(span.start <= pos < span.end for span in spans)


def _tokenize_prepared(prepared: PreparedRecord) -> list[_Token]:
    """Yield word tokens with positions, skipping opaque spans."""
    text = prepared.visible_text
    opaque = prepared.opaque_spans
    protected = prepared.protected_spans
    tokens: list[_Token] = []
    for match in _WORD_RE.finditer(text):
        start, end = match.start(), match.end()
        # Skip tokens that fall inside an opaque (code-like) span.
        if _in_any_span(start, opaque) or _in_any_span((start + end) // 2, opaque):
            continue
        surface = match.group(0)
        is_protected = _in_any_span(start, protected) or _in_any_span(
            end - 1, protected
        )
        tokens.append(
            _Token(
                surface=surface,
                normalized=normalize_token(surface),
                start=start,
                end=end,
                bucket=case_bucket(surface),
                protected=is_protected,
            )
        )
    return tokens


def _phrasplit_model_for_source(source_language: str) -> str:
    # Reuse the deterministic language-model mapping already used by chunking.
    from booktx.chunking import _language_model

    return _language_model(source_language)


def _phrase_units(prepared: PreparedRecord, source_language: str) -> list[_Span]:
    text = prepared.visible_text
    if not text.strip():
        return []

    from phrasplit import split_with_offsets

    segments = split_with_offsets(
        text,
        mode="clause",
        use_spacy=False,
        language_model=_phrasplit_model_for_source(source_language),
    )
    spans = [
        _Span(int(segment.char_start), int(segment.char_end)) for segment in segments
    ]
    return [span for span in spans if span.start < span.end]


def _tokenize_phrase_unit(prepared: PreparedRecord, span: _Span) -> list[_Token]:
    text = prepared.visible_text
    tokens: list[_Token] = []
    for match in _PHRASE_TOKEN_RE.finditer(text, span.start, span.end):
        start, end = match.start(), match.end()
        if _in_any_span(start, prepared.opaque_spans) or _in_any_span(
            (start + end) // 2, prepared.opaque_spans
        ):
            continue
        surface = match.group(0)
        protected = _in_any_span(start, prepared.protected_spans) or _in_any_span(
            end - 1, prepared.protected_spans
        )
        tokens.append(
            _Token(
                surface=surface,
                normalized=normalize_token(surface),
                start=start,
                end=end,
                bucket=case_bucket(surface),
                protected=protected,
            )
        )
    return tokens


def _gap_contains_opaque_span(gap_start: int, gap_end: int, spans: list[_Span]) -> bool:
    return any(span.start < gap_end and gap_start < span.end for span in spans)


def _window_crosses_hard_phrase_boundary(
    text: str,
    window: list[_Token],
    opaque_spans: list[_Span],
) -> bool:
    for left, right in zip(window, window[1:], strict=False):
        gap = text[left.end : right.start]
        if _HARD_PHRASE_GAP_RE.search(gap):
            return True
        if _gap_contains_opaque_span(left.end, right.start, opaque_spans):
            return True
    return False


def _window_contains_hyphenated_token(window: list[_Token]) -> bool:
    return any("-" in token.surface for token in window)


def _hyphenated_spans(prepared: PreparedRecord) -> list[tuple[int, int, str]]:
    """Hyphenated compounds with positions, skipping opaque spans."""
    text = prepared.visible_text
    opaque = prepared.opaque_spans
    out: list[tuple[int, int, str]] = []
    for match in _HYPHENATED_RE.finditer(text):
        start, end = match.start(), match.end()
        if _in_any_span(start, opaque) or _in_any_span((start + end) // 2, opaque):
            continue
        out.append((start, end, match.group(0)))
    return out


def _snippet(text: str, start: int, end: int) -> str:
    """Bounded evidence window around a span, without internal file paths."""
    width = _SNIPPET_WIDTH
    lo = max(0, start - width // 2)
    hi = min(len(text), end + width // 2)
    window = text[lo:hi].strip()
    if len(window) > width:
        window = window[:width]
    return " ".join(window.split())


# --- Candidate accumulator --------------------------------------------------


@dataclass
class _Accum:
    identity: str
    text: str
    normalized: str
    tokens: list[str]
    bucket: CaseBucket
    kind: str
    detector: str
    reason_codes: list[str]
    detectors: set[str] = field(default_factory=set)
    lemma: str | None = None
    count: int = 0
    records: set[str] = field(default_factory=set)
    chapters: set[str] = field(default_factory=set)
    surfaces: dict[str, int] = field(default_factory=dict)
    variant_surfaces: dict[str, int] = field(default_factory=dict)
    first_record_id: str | None = None
    examples: list[SourceAnalysisOccurrence] = field(default_factory=list)
    already_protected: bool = False


@dataclass(frozen=True)
class CandidateFeatures:
    token_count: int
    has_hyphen: bool
    has_title_case: bool
    has_mixed_case: bool
    lemma: str | None
    in_common_lemma_list: bool
    contains_known_world_morpheme: bool
    matches_include_pattern: bool
    matches_exclude_pattern: bool


@dataclass(frozen=True)
class _Classification:
    review_bucket: SourceReviewBucket
    suggested_action: Literal[
        "none",
        "ask_question",
        "add_advisory_glossary",
        "review_name_policy",
        "review_for_binding_glossary",
    ]
    risk_score: float
    genericity_score: float
    rarity_score: float
    morphology_flags: tuple[str, ...]
    suppression_reason: str | None
    canonical_surface: str
    source_variants: tuple[str, ...]
    include: bool


_BUCKET_ORDER: tuple[SourceReviewBucket, ...] = (
    "binding_glossary",
    "name_policy",
    "invented_or_rare",
    "domain_phrase",
    "style_signal",
    "maybe",
    "no_action",
)


_KIND_PRECEDENCE = {
    "proper_name": 0,
    "place_name": 1,
    "invented_term": 2,
    "hyphenated_term": 3,
    "title_candidate": 4,
    "phrase": 5,
    "word": 6,
}


def _merge_kind(existing: str, candidate: str) -> str:
    if _KIND_PRECEDENCE[candidate] < _KIND_PRECEDENCE[existing]:
        return candidate
    return existing


def _add_occurrence(
    accum: _Accum,
    *,
    prepared: PreparedRecord,
    surface: str,
    start: int,
    end: int,
) -> None:
    accum.count += 1
    accum.records.add(prepared.record_id)
    if prepared.chapter_id:
        accum.chapters.add(prepared.chapter_id)
    accum.surfaces[surface] = accum.surfaces.get(surface, 0) + 1
    if accum.first_record_id is None:
        accum.first_record_id = prepared.record_id
    if len(accum.examples) < _MAX_EXAMPLES_PER_CANDIDATE:
        accum.examples.append(
            SourceAnalysisOccurrence(
                record_id=prepared.record_id,
                chapter_id=prepared.chapter_id,
                chapter_title=prepared.chapter_title,
                visible_text=prepared.visible_text[start:end],
                snippet=_snippet(prepared.visible_text, start, end),
            )
        )


# --- Detectors --------------------------------------------------------------


def _detect_tokens(
    prepared_records: list[PreparedRecord],
    source_language: str,
    min_count: int,
    accum_by_id: dict[str, _Accum],
) -> None:
    """Word + proper-name + protected-name detection from word tokens."""
    for prepared in prepared_records:
        tokens = _tokenize_prepared(prepared)
        for tok in tokens:
            bucket = tok.bucket
            kind = "proper_name" if bucket == "title" else "word"
            detector = "title_case" if kind == "proper_name" else "frequency"
            identity = candidate_id_from_identity(
                source_language=source_language,
                normalized=tok.normalized,
                tokens=[tok.normalized],
                case_bucket_value=bucket,
            )
            accum = accum_by_id.get(identity)
            if accum is None:
                accum = _Accum(
                    identity=identity,
                    text=tok.surface,
                    normalized=tok.normalized,
                    tokens=[tok.normalized],
                    bucket=bucket,
                    kind=kind,
                    detector=detector,
                    reason_codes=[],
                )
                accum_by_id[identity] = accum
            else:
                accum.kind = _merge_kind(accum.kind, kind)
            if tok.protected:
                accum.already_protected = True
            _add_occurrence(
                accum,
                prepared=prepared,
                surface=tok.surface,
                start=tok.start,
                end=tok.end,
            )


def _detect_hyphenated(
    prepared_records: list[PreparedRecord],
    source_language: str,
    accum_by_id: dict[str, _Accum],
) -> None:
    """Hyphenated-term detection."""
    for prepared in prepared_records:
        for start, end, surface in _hyphenated_spans(prepared):
            normalized = normalize_token(surface)
            tokens = [normalize_token(part) for part in surface.split("-")]
            bucket = case_bucket(surface)
            identity = candidate_id_from_identity(
                source_language=source_language,
                normalized=normalized,
                tokens=tokens,
                case_bucket_value=bucket,
            )
            accum = accum_by_id.get(identity)
            if accum is None:
                accum = _Accum(
                    identity=identity,
                    text=surface,
                    normalized=normalized,
                    tokens=tokens,
                    bucket=bucket,
                    kind="hyphenated_term",
                    detector="hyphenated",
                    reason_codes=[],
                )
                accum_by_id[identity] = accum
            else:
                accum.kind = _merge_kind(accum.kind, "hyphenated_term")
            _add_occurrence(
                accum, prepared=prepared, surface=surface, start=start, end=end
            )


# Stop words at phrase boundaries: common words + pure punctuation tokens.
def _is_phrase_boundary_token(norm: str, common: frozenset[str]) -> bool:
    return norm in common or len(norm) <= 1


def _detect_phrases(
    prepared_records: list[PreparedRecord],
    source_language: str,
    common: frozenset[str],
    min_count: int,
    ngram_max: int,
    accum_by_id: dict[str, _Accum],
) -> None:
    """Statistical 2-N token phrase detection after boundary trimming.

    Phrases never cross record or chapter boundaries. Leading/trailing common
    words are trimmed. Only phrases with length 2..ngram_max are retained.
    """
    if ngram_max < 2:
        return
    for prepared in prepared_records:
        for unit in _phrase_units(prepared, source_language):
            tokens = _tokenize_phrase_unit(prepared, unit)
            n = len(tokens)
            for size in range(2, ngram_max + 1):
                for i in range(0, n - size + 1):
                    window = tokens[i : i + size]
                    # Trim leading/trailing boundary (common/short) tokens.
                    lo, hi = 0, len(window)
                    while lo < hi - 1 and _is_phrase_boundary_token(
                        window[lo].normalized, common
                    ):
                        lo += 1
                    while hi > lo + 1 and _is_phrase_boundary_token(
                        window[hi - 1].normalized, common
                    ):
                        hi -= 1
                    trimmed = window[lo:hi]
                    if len(trimmed) < 2:
                        continue
                    # Require every surviving token to be non-boundary.
                    if any(
                        _is_phrase_boundary_token(t.normalized, common) for t in trimmed
                    ):
                        continue
                    if _window_crosses_hard_phrase_boundary(
                        prepared.visible_text,
                        trimmed,
                        prepared.opaque_spans,
                    ):
                        continue
                    if _window_contains_hyphenated_token(trimmed):
                        continue
                    surfaces = [t.surface for t in trimmed]
                    norms = [t.normalized for t in trimmed]
                    joined = " ".join(norms)
                    bucket = _phrase_bucket(surfaces)
                    kind = "title_candidate" if bucket == "title" else "phrase"
                    detector = "title_span" if kind == "title_candidate" else "ngram"
                    identity = candidate_id_from_identity(
                        source_language=source_language,
                        normalized=joined,
                        tokens=norms,
                        case_bucket_value=bucket,
                    )
                    start = trimmed[0].start
                    end = trimmed[-1].end
                    accum = accum_by_id.get(identity)
                    if accum is None:
                        accum = _Accum(
                            identity=identity,
                            text=" ".join(surfaces),
                            normalized=joined,
                            tokens=norms,
                            bucket=bucket,
                            kind=kind,
                            detector=detector,
                            reason_codes=[],
                        )
                        accum_by_id[identity] = accum
                    else:
                        accum.kind = _merge_kind(accum.kind, kind)
                    _add_occurrence(
                        accum,
                        prepared=prepared,
                        surface=" ".join(surfaces),
                        start=start,
                        end=end,
                    )


_ENTITY_KINDS = {
    "GPE": "place_name",
    "LOC": "place_name",
    "PERSON": "proper_name",
    "ORG": "proper_name",
    "NORP": "proper_name",
    "WORK_OF_ART": "title_candidate",
}


def _mark_linguistic_candidate(
    accum_by_id: dict[str, _Accum],
    *,
    prepared: PreparedRecord,
    source_language: str,
    surface: str,
    start: int,
    end: int,
    kind: str,
    detector: str,
    lemma: str | None = None,
) -> None:
    normalized = normalize_token(surface)
    tokens = [normalize_token(part) for part in _WORD_RE.findall(surface)]
    if not tokens:
        return
    bucket = _phrase_bucket(_WORD_RE.findall(surface))
    if detector == "spacy_noun_chunk" and len(tokens) == 1:
        identity = candidate_id_from_identity(
            source_language=source_language,
            normalized=normalized,
            tokens=tokens,
            case_bucket_value=bucket,
        )
        accum = accum_by_id.get(identity)
        if accum is None:
            return
        accum.detectors.add(detector)
        accum.reason_codes.append(detector)
        if lemma and not accum.lemma:
            accum.lemma = lemma
        return
    identity = candidate_id_from_identity(
        source_language=source_language,
        normalized=normalized,
        tokens=tokens,
        case_bucket_value=bucket,
    )
    accum = accum_by_id.get(identity)
    if accum is None:
        accum = _Accum(
            identity=identity,
            text=surface,
            normalized=normalized,
            tokens=tokens,
            bucket=bucket,
            kind=kind,
            detector=detector,
            reason_codes=[detector],
        )
        accum_by_id[identity] = accum
        _add_occurrence(
            accum,
            prepared=prepared,
            surface=surface,
            start=start,
            end=end,
        )
    else:
        accum.kind = _merge_kind(accum.kind, kind)
        accum.reason_codes.append(detector)
    accum.detectors.add(detector)
    if lemma and not accum.lemma:
        accum.lemma = lemma


def _enrich_with_spacy(
    prepared_records: list[PreparedRecord],
    *,
    source_language: str,
    nlp: Any,
    capabilities: AnalysisCapabilities,
    accum_by_id: dict[str, _Accum],
) -> None:
    """Add model-backed evidence without making identity model-dependent."""
    for prepared in prepared_records:
        doc = nlp(prepared.visible_text)
        if capabilities.lemmatizer or capabilities.pos:
            for token in doc:
                if getattr(token, "is_space", False) or getattr(
                    token, "is_punct", False
                ):
                    continue
                surface = str(token.text)
                normalized = normalize_token(surface)
                identity = candidate_id_from_identity(
                    source_language=source_language,
                    normalized=normalized,
                    tokens=[normalized],
                    case_bucket_value=case_bucket(surface),
                )
                accum = accum_by_id.get(identity)
                if accum is None:
                    continue
                if capabilities.lemmatizer:
                    lemma = str(getattr(token, "lemma_", "")).strip()
                    if lemma and lemma != "-PRON-":
                        accum.lemma = normalize_token(lemma)
                        accum.detectors.add("spacy_lemma")
                if capabilities.pos and str(getattr(token, "pos_", "")).strip():
                    accum.detectors.add("spacy_pos")
                    accum.reason_codes.append(
                        "spacy_pos_" + str(token.pos_).strip().lower()
                    )
        if capabilities.noun_chunks:
            for chunk in doc.noun_chunks:
                _mark_linguistic_candidate(
                    accum_by_id,
                    prepared=prepared,
                    source_language=source_language,
                    surface=str(chunk.text),
                    start=int(chunk.start_char),
                    end=int(chunk.end_char),
                    kind="phrase",
                    detector="spacy_noun_chunk",
                )
        if capabilities.ner:
            for entity in doc.ents:
                kind = _ENTITY_KINDS.get(str(entity.label_))
                if kind is None:
                    continue
                _mark_linguistic_candidate(
                    accum_by_id,
                    prepared=prepared,
                    source_language=source_language,
                    surface=str(entity.text),
                    start=int(entity.start_char),
                    end=int(entity.end_char),
                    kind=kind,
                    detector="spacy_ner_" + str(entity.label_).lower(),
                )


# --- Style metrics ----------------------------------------------------------

_QUOTE_STYLES = [
    ("double", re.compile(r'[\u201c\u201d"]')),
    ("single", re.compile(r"[\u2018\u2019']")),
    ("guillemet", re.compile(r"[«»\u2039\u203a]")),
    ("german", re.compile(r"[„“]")),
]
_DIALOGUE_RE = re.compile(r'[\u201c\u201d"\u201e„«»]')
_EM_DASH_RE = re.compile(r"\u2014")
_EPUB_EMPHASIS_RE = re.compile(r"<(?:em|strong|i|b)\b", re.IGNORECASE)
_MD_EMPHASIS_RE = re.compile(r"(?<!\*)\*[^*\s][^*]*\*(?!\*)|(?<!_)_[^_\s][^_]*_(?!_)")
_SENTENCE_SPLIT_RE = re.compile(r"[.!?。！？]+[\s]+|$")


def _count_sentences(text: str) -> int:
    if not text.strip():
        return 0
    # Heuristic: count sentence-ending punctuation followed by whitespace.
    return max(1, len(re.findall(r"[.!?。！？]+(?:\s+|$)", text)))


def _build_style_metrics(
    prepared_records: list[PreparedRecord],
    raw_sources: list[str],
    capability_warnings: list[str],
) -> SourceStyleMetrics:
    total = len(prepared_records)
    quote_counts: dict[str, int] = {name: 0 for name, _ in _QUOTE_STYLES}
    em_dash_count = 0
    emphasis_counts: list[int] = []
    dialogue_records = 0
    sentence_total = 0
    word_total = 0
    for prepared, source in zip(prepared_records, raw_sources, strict=True):
        text = prepared.visible_text
        if _DIALOGUE_RE.search(text):
            dialogue_records += 1
        for name, pattern in _QUOTE_STYLES:
            quote_counts[name] += len(pattern.findall(text))
        em_dash_count += len(_EM_DASH_RE.findall(text))
        if prepared.source_markup == "epub-inline-xhtml:v1":
            emphasis_counts.append(len(_EPUB_EMPHASIS_RE.findall(source)))
        else:
            emphasis_counts.append(len(_MD_EMPHASIS_RE.findall(source)))
        sentence_total += _count_sentences(text)
        word_total += len(_WORD_RE.findall(text))
    emphasis_count = sum(emphasis_counts)
    sentence_count = sentence_total or None
    average = (word_total / sentence_total) if sentence_total else None
    return SourceStyleMetrics(
        record_count_with_dialogue=dialogue_records,
        dialogue_record_ratio=(dialogue_records / total) if total else 0.0,
        quote_counts=quote_counts,
        em_dash_count=em_dash_count,
        emphasis_count=emphasis_count,
        sentence_count=sentence_count,
        average_sentence_words=round(average, 3) if average is not None else None,
        capability_warnings=list(capability_warnings),
    )


# --- Scoring + assembly -----------------------------------------------------


def _score_accum(
    accum: _Accum, common: frozenset[str], include_common: bool
) -> float | None:
    # Compatibility score retained for legacy consumers and rough signal only.
    if accum.count <= 0:
        return None
    is_common = accum.normalized in common or all(tok in common for tok in accum.tokens)
    uncommon_score = _COMMON_PENALTY if is_common else 1.0
    phrase_bonus = _PHRASE_BONUS if accum.kind in _PHRASE_KINDS else 1.0
    return (
        math.log(1 + accum.count)
        * (1 + math.log(1 + len(accum.chapters)))
        * uncommon_score
        * phrase_bonus
    )


def _all_surfaces(accum: _Accum) -> list[tuple[str, int]]:
    counts = dict(accum.surfaces)
    for surface, count in accum.variant_surfaces.items():
        counts[surface] = counts.get(surface, 0) + count
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def _candidate_features(
    accum: _Accum,
    *,
    common: frozenset[str],
    runtime: _SourceAnalysisRuntimeConfig,
) -> CandidateFeatures:
    surfaces = [surface for surface, _ in _all_surfaces(accum)]
    haystacks = [accum.text, accum.normalized, *surfaces]
    lemma = accum.lemma.casefold() if accum.lemma else None
    normalized_no_separators = accum.normalized.replace("-", "").replace(" ", "")
    contains_known_world_morpheme = any(
        morpheme in normalized_no_separators
        or any(morpheme in token for token in accum.tokens)
        for morpheme in runtime.world_morphemes
    )
    matches_include_pattern = any(
        pattern.search(value)
        for pattern in runtime.include_patterns
        for value in haystacks
    )
    matches_exclude_pattern = any(
        pattern.search(value)
        for pattern in runtime.exclude_patterns
        for value in haystacks
    )
    in_common_lemma_list = (
        accum.normalized in common
        or accum.normalized in runtime.generic_lemmas
        or (lemma in runtime.generic_lemmas if lemma else False)
        or any(token in runtime.generic_lemmas for token in accum.tokens)
    )
    return CandidateFeatures(
        token_count=len(accum.tokens),
        has_hyphen=any("-" in surface for surface in haystacks),
        has_title_case=accum.bucket == "title",
        has_mixed_case=accum.bucket == "mixed",
        lemma=lemma,
        in_common_lemma_list=in_common_lemma_list,
        contains_known_world_morpheme=contains_known_world_morpheme,
        matches_include_pattern=matches_include_pattern,
        matches_exclude_pattern=matches_exclude_pattern,
    )


def _genericity_score(
    accum: _Accum,
    *,
    features: CandidateFeatures,
    common: frozenset[str],
    runtime: _SourceAnalysisRuntimeConfig,
) -> float:
    score = 0.0
    if accum.normalized in common or all(token in common for token in accum.tokens):
        score += 1.0
    if features.in_common_lemma_list:
        score += 1.2 if features.token_count == 1 else 0.5
    generic_token_hits = sum(
        1
        for token in accum.tokens
        if token in common or token in runtime.generic_lemmas
    )
    if generic_token_hits:
        score += (
            0.8 if features.token_count == 1 else min(1.4, 0.6 * generic_token_hits)
        )
    if features.token_count > 1 and accum.tokens[0] in common:
        score += 0.6
    if features.token_count > 1 and accum.tokens[-1] in runtime.generic_lemmas:
        score += 0.8
    if features.token_count == 1 and accum.kind == "word":
        score += 0.4
    if features.token_count == 1 and "spacy_pos_verb" in accum.reason_codes:
        score += 0.3
    return score


def _rarity_score(accum: _Accum) -> float:
    if accum.count <= 1:
        return 1.6
    if accum.count == 2:
        return 1.4
    if accum.count <= 4:
        return 1.0
    if accum.count <= 8:
        return 0.7
    return 0.3


def _detector_priority(accum: _Accum, features: CandidateFeatures) -> float:
    if accum.kind == "hyphenated_term":
        return 1.6
    if features.matches_include_pattern or features.contains_known_world_morpheme:
        return 1.4
    if accum.kind in {"proper_name", "place_name", "title_candidate"}:
        return 1.2
    if accum.kind == "invented_term":
        return 1.1
    if accum.kind == "phrase":
        return 0.6
    return 0.2


def _build_morphology_flags(accum: _Accum, features: CandidateFeatures) -> list[str]:
    flags: list[str] = []
    if features.token_count == 1:
        flags.append("single_token")
    if features.has_hyphen:
        flags.append("hyphenated")
    if features.has_title_case:
        flags.append("title_case")
    if features.has_mixed_case:
        flags.append("mixed_case")
    if features.contains_known_world_morpheme:
        flags.append("world_morpheme")
    if features.matches_include_pattern:
        flags.append("include_pattern")
    if accum.already_protected:
        flags.append("already_protected")
    return flags


def _compute_high_risk_singleton(
    accum: _Accum,
    features: CandidateFeatures,
    runtime: _SourceAnalysisRuntimeConfig,
    genericity: float,
) -> bool:
    if accum.kind == "phrase":
        return False
    return (
        runtime.include_singletons
        and accum.count == 1
        and (
            accum.kind == "hyphenated_term"
            or (
                accum.kind in {"proper_name", "place_name"}
                and not features.in_common_lemma_list
            )
            or (
                accum.kind == "title_candidate"
                and genericity < 1.0
                and features.token_count <= 2
            )
            or features.contains_known_world_morpheme
            or features.matches_include_pattern
            or (features.has_mixed_case and features.token_count == 1)
        )
    )


def _compute_candidate_scores(
    accum: _Accum,
    features: CandidateFeatures,
    genericity: float,
    rarity: float,
) -> tuple[float, float, float, float, float, float]:
    morphology = 0.0
    if features.has_hyphen:
        morphology += 1.4
    if features.contains_known_world_morpheme:
        morphology += 1.2
    if features.matches_include_pattern:
        morphology += 1.0
    if features.has_mixed_case:
        morphology += 0.8
    if features.has_title_case:
        morphology += 0.6
    name_score = 0.0
    if accum.kind in {"proper_name", "place_name", "title_candidate"}:
        name_score += 1.6
    if features.has_title_case and not accum.already_protected:
        name_score += 0.8
    termhood = 0.4
    if features.token_count > 1:
        termhood += 0.6
    if accum.kind in {"hyphenated_term", "invented_term", "title_candidate"}:
        termhood += 0.5
    if features.token_count == 1 and "spacy_noun_chunk" in accum.detectors:
        termhood -= 0.5
    dispersion = min(len(accum.chapters), 3) / 3.0
    grammar_noise = (
        1.0 if features.token_count == 1 and len(accum.normalized) <= 1 else 0.0
    )
    risk_score = (
        4.0 * _detector_priority(accum, features)
        + 2.5 * rarity
        + 2.0 * morphology
        + 2.0 * name_score
        + 1.5 * termhood
        + 1.0 * min(math.log(1 + accum.count), 2.0)
        + 0.5 * dispersion
        - 3.0 * genericity
        - 2.0 * grammar_noise
    )
    return morphology, name_score, termhood, dispersion, grammar_noise, risk_score


def _classify_review_bucket(
    accum: _Accum,
    features: CandidateFeatures,
    runtime: _SourceAnalysisRuntimeConfig,
    genericity: float,
    is_fragment: bool,
    high_risk_singleton: bool,
    min_count: int,
    risk_score: float,
) -> tuple[str, str, str | None]:
    review_bucket: SourceReviewBucket = "maybe"
    suggested_action: Literal[
        "none",
        "ask_question",
        "add_advisory_glossary",
        "review_name_policy",
        "review_for_binding_glossary",
    ] = "none"
    suppression_reason: str | None = None

    if features.matches_exclude_pattern:
        review_bucket = "no_action"
        suppression_reason = "excluded_by_pattern"
    elif is_fragment:
        review_bucket = "no_action"
        suppression_reason = "fragment"
    elif (
        features.token_count == 1
        and features.in_common_lemma_list
        and not (
            features.contains_known_world_morpheme or features.matches_include_pattern
        )
    ):
        review_bucket = "no_action"
        suppression_reason = "generic_single_token"
    elif accum.kind == "hyphenated_term":
        review_bucket = "binding_glossary"
        suggested_action = "review_for_binding_glossary"
    elif accum.kind in {"proper_name", "place_name", "title_candidate"} and not (
        features.token_count == 1 and features.in_common_lemma_list
    ):
        review_bucket = "name_policy"
        suggested_action = "review_name_policy"
    elif (
        accum.kind == "phrase"
        and "spacy_noun_chunk" in accum.detectors
        and features.has_hyphen
    ):
        review_bucket = "no_action"
        suppression_reason = "contextual_noun_chunk"
    elif features.matches_include_pattern or high_risk_singleton:
        review_bucket = "invented_or_rare"
        suggested_action = "ask_question"
    elif accum.kind == "phrase" and features.token_count > 1 and genericity < 1.0:
        review_bucket = "domain_phrase"
        suggested_action = "add_advisory_glossary"
    elif (
        accum.kind == "phrase"
        and features.token_count <= 2
        and any(token in runtime.generic_lemmas for token in accum.tokens)
        and not (
            features.contains_known_world_morpheme or features.matches_include_pattern
        )
    ):
        review_bucket = "no_action"
        suppression_reason = "generic_phrase"
    elif accum.kind == "phrase" and features.token_count <= 3 and genericity >= 1.2:
        review_bucket = "no_action"
        suppression_reason = "generic_phrase"
    elif genericity >= 1.0 and features.token_count == 1:
        review_bucket = "no_action"
        suppression_reason = "generic_single_token"

    if (
        review_bucket != "no_action"
        and accum.count < min_count
        and not high_risk_singleton
    ):
        review_bucket = "no_action"
        suppression_reason = "below_min_count"
    if (
        review_bucket
        not in {"no_action", "binding_glossary", "name_policy", "invented_or_rare"}
        and risk_score < runtime.min_risk_score
    ):
        review_bucket = "no_action"
        suppression_reason = suppression_reason or "below_risk_threshold"
        suggested_action = "none"
    if review_bucket == "no_action":
        suggested_action = "none"

    return review_bucket, suggested_action, suppression_reason


def _classify_candidate(
    accum: _Accum,
    *,
    common: frozenset[str],
    include_common: bool,
    min_count: int,
    runtime: _SourceAnalysisRuntimeConfig,
) -> _Classification:
    features = _candidate_features(accum, common=common, runtime=runtime)
    morphology_flags = _build_morphology_flags(accum, features)
    genericity = _genericity_score(
        accum, features=features, common=common, runtime=runtime
    )
    rarity = _rarity_score(accum)
    (morphology, name_score, termhood, dispersion, grammar_noise, risk_score) = (
        _compute_candidate_scores(accum, features, genericity, rarity)
    )

    is_fragment = features.token_count == 1 and len(accum.normalized) <= 1
    high_risk_singleton = _compute_high_risk_singleton(
        accum, features, runtime, genericity
    )
    if high_risk_singleton:
        morphology_flags.append("singleton_override")

    review_bucket, suggested_action, suppression_reason = _classify_review_bucket(
        accum,
        features,
        runtime,
        genericity,
        is_fragment,
        high_risk_singleton,
        min_count,
        risk_score,
    )

    include = review_bucket != "no_action" or include_common
    if review_bucket == "no_action" and include_common:
        morphology_flags.append("included_common")

    canonical_surface = accum.text
    source_variants = tuple(
        surface for surface, _ in _all_surfaces(accum) if surface != canonical_surface
    )
    return _Classification(
        review_bucket=review_bucket,
        suggested_action=suggested_action,
        risk_score=risk_score,
        genericity_score=genericity,
        rarity_score=rarity,
        morphology_flags=tuple(dict.fromkeys(morphology_flags)),
        suppression_reason=suppression_reason,
        canonical_surface=canonical_surface,
        source_variants=source_variants,
        include=include,
    )


def _merge_hyphenated_variants(accum_by_id: dict[str, _Accum]) -> None:
    hyphenated_by_phrase: dict[tuple[tuple[str, ...], CaseBucket], _Accum] = {}
    hyphenated_by_normalized: dict[tuple[str, tuple[str, ...]], _Accum] = {}
    for accum in accum_by_id.values():
        if accum.kind == "hyphenated_term":
            hyphenated_by_phrase[(tuple(accum.tokens), accum.bucket)] = accum
            hyphenated_by_normalized[(accum.normalized, tuple(accum.tokens))] = accum
    remove_ids: set[str] = set()
    for identity, accum in accum_by_id.items():
        if accum.kind == "hyphenated_term":
            continue
        normalized_target = hyphenated_by_normalized.get(
            (accum.normalized, tuple(accum.tokens))
        )
        if normalized_target is not None:
            remove_ids.add(identity)
            continue
        target = hyphenated_by_phrase.get((tuple(accum.tokens), accum.bucket))
        if target is None:
            continue
        if accum.normalized != target.normalized.replace("-", " "):
            continue
        for surface, count in accum.surfaces.items():
            if surface not in target.surfaces:
                target.variant_surfaces[surface] = (
                    target.variant_surfaces.get(surface, 0) + count
                )
        remove_ids.add(identity)
    for identity in remove_ids:
        accum_by_id.pop(identity, None)


def _finalize_candidate(
    accum: _Accum,
    score: float,
    common: frozenset[str],
    classification: _Classification,
) -> SourceCandidate:
    is_common = accum.normalized in common or all(tok in common for tok in accum.tokens)
    uncommon_score = _COMMON_PENALTY if is_common else 1.0
    surface_forms = [surface for surface, _ in _all_surfaces(accum)]
    detectors = sorted(
        accum.detectors
        | {accum.detector, *(["protected_name"] if accum.already_protected else [])}
    )
    reason_codes = sorted(
        set(accum.reason_codes)
        | {accum.detector}
        | {f"bucket_{classification.review_bucket}"}
        | (
            {f"suppressed_{classification.suppression_reason}"}
            if classification.suppression_reason
            else set()
        )
        | set(classification.morphology_flags)
    )
    if accum.already_protected and "already_protected" not in reason_codes:
        reason_codes.append("already_protected")
    return SourceCandidate(
        id=accum.identity,
        text=classification.canonical_surface,
        normalized=accum.normalized,
        surface_forms=surface_forms,
        lemma=accum.lemma,
        kind=accum.kind,  # type: ignore[arg-type]
        detectors=detectors,
        count=accum.count,
        record_frequency=len(accum.records),
        chapter_frequency=len(accum.chapters),
        score=round(score, 6),
        uncommon_score=uncommon_score,
        first_record_id=accum.first_record_id,
        examples=accum.examples,
        reason_codes=reason_codes,
        reason=_reason_text(accum, classification),
        already_protected=accum.already_protected,
        suggested_context_action=classification.suggested_action,
        review_bucket=classification.review_bucket,
        risk_score=round(classification.risk_score, 6),
        genericity_score=round(classification.genericity_score, 6),
        rarity_score=round(classification.rarity_score, 6),
        morphology_flags=list(classification.morphology_flags),
        suppression_reason=classification.suppression_reason,
        canonical_surface=classification.canonical_surface,
        source_variants=list(classification.source_variants),
        token_count=len(accum.tokens),
        external_frequency=None,
    )


def _reason_text(accum: _Accum, classification: _Classification) -> str:
    parts: list[str] = []
    if classification.review_bucket == "binding_glossary":
        parts.append("binding glossary review candidate")
    elif classification.review_bucket == "name_policy":
        parts.append("name or title policy review candidate")
    elif classification.review_bucket == "invented_or_rare":
        parts.append("rare or invented term review candidate")
    elif classification.review_bucket == "domain_phrase":
        parts.append("domain phrase worth later review")
    elif (
        classification.review_bucket == "no_action"
        and classification.suppression_reason
    ):
        parts.append(
            f"suppressed: {classification.suppression_reason.replace('_', ' ')}"
        )
    elif accum.kind == "phrase":
        parts.append("repeated phrase")
    else:
        parts.append("frequent term")
    if classification.morphology_flags:
        parts.append(
            ", ".join(
                flag.replace("_", " ") for flag in classification.morphology_flags
            )
        )
    if accum.already_protected:
        parts.append("already protected at extraction time")
    return "; ".join(parts)


def _sort_key(candidate: SourceCandidate) -> tuple[int, float, int, str, str]:
    return (
        _BUCKET_ORDER.index(candidate.review_bucket),
        -candidate.risk_score,
        -candidate.count,
        candidate.normalized,
        candidate.id,
    )


# --- Preflight --------------------------------------------------------------


def source_analysis_preflight(
    project: Project,
    *,
    chapter_map: ChapterMap | None,
) -> None:
    """Block analysis when extraction is missing or the chapter audit fails.

    A missing/stale chapter map is a controlled error in a dry run, never a
    silent repair. Blocking EPUB chapter-map/TOC audit findings also block so an
    incomplete chapter map cannot produce a deceptively complete report.
    """
    if not project.chunks():
        raise _err(
            "source_analysis_no_extraction",
            "no extracted source records found; run `booktx extract` before analysis",
        )
    if chapter_map is None:
        raise _err(
            "source_analysis_no_chapter_map",
            "no chapter map found; run `booktx extract` (EPUB) or "
            "`booktx chapters PROJECT_DIR` (markdown) before analysis. "
            "Analysis does not repair a missing chapter map.",
        )
    # Blocking EPUB TOC audit (no-op for markdown projects).
    try:
        from booktx.epub_toc_audit import audit_epub_chapter_map

        audit = audit_epub_chapter_map(project, chapter_map=chapter_map)
    except Exception:  # noqa: BLE001 - audit is advisory for blocking decisions
        return
    blocking = audit.error_findings
    if blocking:
        first = blocking[0]
        raise _err(
            "source_analysis_blocking_chapter_audit",
            "blocking EPUB chapter-map audit finding prevents analysis: "
            f"{first.code}: {first.message}",
        )


# --- Report assembly --------------------------------------------------------


def build_source_analysis(
    project: Project,
    *,
    engine_requested: str = "auto",
    spacy_model: str | None = None,
    min_count: int = 2,
    ngram_max: int = 4,
    top: int = 200,
    include_common: bool = False,
    generated_at: str | None = None,
) -> SourceAnalysisReport:
    """Build the authoritative source-analysis report (no file writes).

    Runs preflight, loads source chunks and a read-only chapter map, prepares
    records, runs the simple engine, merges/scores/orders candidates, computes
    style metrics, and assembles the report with its semantic digest.
    """
    from booktx.chapters import load_chapter_map_only
    from booktx.config import (
        project_source_sha256,
    )
    from booktx.editor_indexes import build_chapter_record_map
    from booktx.io_utils import utc_timestamp
    from booktx.progress import load_source_chunks

    if ngram_max < 1 or ngram_max > 4:
        raise _err(
            "source_analysis_bad_ngram_max",
            "--ngram-max must be between 1 and 4",
        )
    if min_count < 1:
        raise _err(
            "source_analysis_bad_min_count",
            "--min-count must be at least 1",
        )
    if top < 1:
        raise _err(
            "source_analysis_bad_top",
            "--top must be at least 1",
        )

    chapter_map = load_chapter_map_only(project)
    source_analysis_preflight(project, chapter_map=chapter_map)
    assert chapter_map is not None  # preflight guarantees it

    chunks = load_source_chunks(project)
    chapter_by_record = build_chapter_record_map(chunks, chapter_map)
    source_language = (
        chunks[0].source_language if chunks else project.config.source_language
    )
    record_id_scheme = chunks[0].record_id_scheme if chunks else "chunk-local:v1"
    source_sha = project_source_sha256(project)
    extracted_input = extracted_input_sha256(
        chunks,
        source_language=source_language,
        source_sha256=source_sha,
        record_id_scheme=record_id_scheme,
        chapter_map=chapter_map,
        chapter_by_record=chapter_by_record,
    )
    chapter_map_sha = _chapter_map_sha256(chapter_map)

    runtime = _resolve_engine_runtime(engine_requested, spacy_model, source_language)
    resolved = runtime.resolved
    capabilities = runtime.capabilities
    capability_warnings = runtime.warnings

    prepared = prepare_records(chunks, chapter_by_record)
    raw_sources = [record.source for chunk in chunks for record in chunk.records]
    common = common_word_set(source_language)
    runtime_config = _runtime_source_analysis_config(project, source_language)

    warnings: list[str] = []
    warnings.extend(runtime.warnings)
    if not common:
        warnings.append(
            f"no bundled common-word list for source language '{source_language}'; "
            "running with corpus-internal signals only"
        )
    if (
        source_language.lower().split("-")[0] != "en"
        and not runtime_config.generic_lemmas
    ):
        warnings.append(
            f"no bundled generic-lemma list for source language '{source_language}'; "
            "generic single-token suppression will rely on common words and morphology"
        )

    accum_by_id: dict[str, _Accum] = {}
    _detect_tokens(prepared, source_language, min_count, accum_by_id)
    _detect_hyphenated(prepared, source_language, accum_by_id)
    _detect_phrases(
        prepared, source_language, common, min_count, ngram_max, accum_by_id
    )
    if runtime.nlp is not None:
        _enrich_with_spacy(
            prepared,
            source_language=source_language,
            nlp=runtime.nlp,
            capabilities=capabilities,
            accum_by_id=accum_by_id,
        )
    _merge_hyphenated_variants(accum_by_id)

    # Classify after evidence collection, then form bucketed review output.
    suppressed_counts: dict[str, int] = {}
    emitted_by_bucket: dict[SourceReviewBucket, list[SourceCandidate]] = {
        bucket: [] for bucket in _BUCKET_ORDER
    }
    for accum in accum_by_id.values():
        score = _score_accum(accum, common, include_common)
        if score is None:
            continue
        classification = _classify_candidate(
            accum,
            common=common,
            include_common=include_common,
            min_count=min_count,
            runtime=runtime_config,
        )
        if not classification.include:
            key = classification.suppression_reason or "suppressed"
            suppressed_counts[key] = suppressed_counts.get(key, 0) + 1
            continue
        candidate = _finalize_candidate(accum, score, common, classification)
        emitted_by_bucket[candidate.review_bucket].append(candidate)
        if candidate.review_bucket == "no_action":
            key = candidate.suppression_reason or "suppressed"
            suppressed_counts[key] = suppressed_counts.get(key, 0) + 1
    candidates: list[SourceCandidate] = []
    for bucket in _BUCKET_ORDER:
        bucket_candidates = emitted_by_bucket[bucket]
        bucket_candidates.sort(key=_sort_key)
        candidates.extend(bucket_candidates[: runtime_config.max_per_bucket])
    candidates = candidates[:top]
    candidates.sort(key=_sort_key)

    style_metrics = _build_style_metrics(prepared, raw_sources, capability_warnings)
    settings = SourceAnalysisSettings(
        engine_requested=engine_requested,  # type: ignore[arg-type]
        engine_resolved=resolved,
        spacy_model=runtime.model_name,
        spacy_version=runtime.spacy_version,
        model_version=runtime.model_version,
        min_count=min_count,
        ngram_max=ngram_max,
        top=top,
        include_common=include_common,
    )
    chapter_count = len(chapter_map.chapters)
    report = SourceAnalysisReport(
        identity_ruleset_version=IDENTITY_RULESET_VERSION,
        analysis_ruleset_version=ANALYSIS_RULESET_VERSION,
        source_sha256=source_sha,
        extracted_input_sha256=extracted_input,
        chapter_map_sha256=chapter_map_sha,
        analysis_sha256="",  # filled after digest
        source_language=source_language,
        generated_at=generated_at or utc_timestamp(),
        settings=settings,
        capabilities=capabilities,
        record_count=len(prepared),
        chapter_count=chapter_count,
        candidates=candidates,
        style_metrics=style_metrics,
        warnings=warnings,
        suppressed_counts=suppressed_counts,
    )
    report.analysis_sha256 = compute_analysis_sha256(report)
    return report


# --- Snapshot envelope + validation -----------------------------------------


def build_snapshot(
    report: SourceAnalysisReport, *, profile: str, generated_at: str
) -> SourceAnalysisSnapshot:
    """Wrap a canonical report in a profile-scoped snapshot envelope."""
    return SourceAnalysisSnapshot(
        schema=SNAPSHOT_SCHEMA,
        generated=True,
        canonical=False,
        profile=profile,
        snapshot_generated_at=generated_at,
        source_sha256=report.source_sha256,
        extracted_input_sha256=report.extracted_input_sha256,
        analysis_sha256=report.analysis_sha256,
        report=report,
    )


class SnapshotValidationError(BooktxError):
    """Raised when a snapshot envelope is missing, stale, or tampered."""


@dataclass(slots=True)
class SnapshotRead:
    """A validated snapshot read result for profile-root rendering."""

    snapshot: SourceAnalysisSnapshot
    stale: bool
    hint: str = ""


def validate_snapshot_payload(payload: dict[str, object]) -> SourceAnalysisSnapshot:
    """Validate a parsed snapshot payload and verify its embedded digest."""
    schema = payload.get("schema") or payload.get("schema_name")
    if schema != SNAPSHOT_SCHEMA:
        raise SnapshotValidationError(
            "source_analysis_bad_snapshot_schema",
            f"source-analysis snapshot has unexpected schema: {schema!r}",
        )
    if payload.get("generated") is not True or payload.get("canonical") is not False:
        raise SnapshotValidationError(
            "source_analysis_bad_snapshot_envelope",
            "source-analysis snapshot envelope flags are invalid",
        )
    snapshot = SourceAnalysisSnapshot.model_validate(payload)
    recomputed = compute_analysis_sha256(snapshot.report)
    if recomputed != snapshot.analysis_sha256:
        raise SnapshotValidationError(
            "source_analysis_snapshot_tampered",
            "source-analysis snapshot analysis_sha256 does not match its embedded report",
        )
    return snapshot


def read_snapshot(
    path: object, *, expected_analysis_sha256: str | None = None
) -> SnapshotRead:
    """Read and validate a profile snapshot, reporting staleness safely.

    ``path`` is accepted as ``object`` so callers can pass a profile-root
    relative marker without exposing parent paths here. The hint never contains
    absolute or parent paths.
    """
    from pathlib import Path

    p = Path(path)  # type: ignore[arg-type]
    if not p.is_file():
        raise SnapshotValidationError(
            "source_analysis_snapshot_missing",
            "no source-analysis snapshot exists for this profile; "
            "run `booktx source analyze . --write --sync-profiles` from the project root",
        )
    payload = json.loads(p.read_text("utf-8"))
    snapshot = validate_snapshot_payload(payload)
    stale = False
    hint = ""
    if (
        expected_analysis_sha256
        and snapshot.analysis_sha256 != expected_analysis_sha256
    ):
        stale = True
        hint = (
            "source-analysis snapshot is stale relative to the canonical report; "
            "refresh with `booktx source analyze . --write --sync-profiles`"
        )
    return SnapshotRead(snapshot=snapshot, stale=stale, hint=hint)


def read_canonical_report(project: Project) -> SourceAnalysisReport | None:
    """Read the canonical project-root report, or ``None`` when absent."""
    from booktx.config import source_analysis_path

    path = source_analysis_path(project)
    if not path.is_file():
        return None
    payload = json.loads(path.read_text("utf-8"))
    if (payload.get("schema") or payload.get("schema_name")) != ANALYSIS_SCHEMA:
        raise SnapshotValidationError(
            "source_analysis_bad_report_schema",
            "canonical source-analysis report has unexpected schema",
        )
    report = SourceAnalysisReport.model_validate(payload)
    recomputed = compute_analysis_sha256(report)
    if recomputed != report.analysis_sha256:
        raise SnapshotValidationError(
            "source_analysis_report_tampered",
            "canonical source-analysis report analysis_sha256 does not match its content",
        )
    return report


# --- Markdown rendering -----------------------------------------------------


def _capabilities_label(cap: AnalysisCapabilities) -> str:
    names = [
        name
        for name, on in (
            ("tokenizer", cap.tokenizer),
            ("sentence_boundaries", cap.sentence_boundaries),
            ("lemmatizer", cap.lemmatizer),
            ("pos", cap.pos),
            ("parser", cap.parser),
            ("noun_chunks", cap.noun_chunks),
            ("ner", cap.ner),
        )
        if on
    ]
    return ", ".join(names) if names else "(none)"


def _markdown_bucket_title(bucket: SourceReviewBucket) -> str:
    return {
        "binding_glossary": "## Review first: binding glossary decisions",
        "name_policy": "## Review names and titles",
        "invented_or_rare": "## Possible invented / rare terms",
        "domain_phrase": "## Maybe review later",
        "maybe": "## Maybe review later",
        "style_signal": "## Style signals",
        "no_action": "## Suppressed / no action candidates",
    }[bucket]


def _markdown_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _candidate_example(candidate: SourceCandidate) -> str:
    if not candidate.examples:
        return ""
    return _markdown_cell(candidate.examples[0].snippet)


def _candidate_command(candidate: SourceCandidate) -> str:
    if candidate.review_bucket == "binding_glossary":
        return (
            f"`booktx context promote-candidate . {candidate.id} --profile PROFILE "
            '--target "TARGET" --require-target --enforce error --write`'
        )
    if candidate.review_bucket in {"name_policy", "invented_or_rare"}:
        return (
            f"`booktx context promote-candidate . {candidate.id} "
            "--profile PROFILE --as-question --write`"
        )
    if candidate.review_bucket == "no_action":
        return (
            f"`booktx source ignore-candidate . {candidate.id} "
            '--reason "ordinary vocabulary" --write`'
        )
    return (
        f"`booktx source review-candidate . {candidate.id} "
        '--reason "checked; no glossary decision needed" --write`'
    )


def render_report_markdown(report: SourceAnalysisReport) -> str:
    """Render a deterministic Markdown view of the report (JSON authoritative)."""
    lines: list[str] = []
    lines.append("# booktx source analysis")
    lines.append("")
    lines.append(f"Source SHA256: {report.source_sha256}")
    lines.append(f"Extracted input SHA256: {report.extracted_input_sha256}")
    lines.append(f"Chapter map SHA256: {report.chapter_map_sha256}")
    lines.append(f"Analysis SHA256: {report.analysis_sha256}")
    lines.append(f"Identity ruleset: {report.identity_ruleset_version}")
    lines.append(f"Analysis ruleset: {report.analysis_ruleset_version}")
    lines.append(f"Source language: {report.source_language}")
    lines.append(f"Engine: {report.settings.engine_resolved}")
    lines.append(f"Capabilities: {_capabilities_label(report.capabilities)}")
    lines.append(f"Records: {report.record_count}")
    lines.append(f"Chapters: {report.chapter_count}")
    lines.append(f"Candidates: {len(report.candidates)}")
    lines.append("")

    if report.warnings:
        lines.append("## Warnings")
        lines.append("")
        for warning in report.warnings:
            lines.append(f"- {warning}")
        lines.append("")

    by_bucket: dict[SourceReviewBucket, list[SourceCandidate]] = {
        bucket: [] for bucket in _BUCKET_ORDER
    }
    for candidate in report.candidates:
        by_bucket[candidate.review_bucket].append(candidate)
    rendered_any = False
    for bucket in _BUCKET_ORDER:
        if bucket == "no_action":
            continue
        bucket_candidates = by_bucket[bucket]
        if not bucket_candidates:
            continue
        rendered_any = True
        lines.append(_markdown_bucket_title(bucket))
        lines.append("")
        lines.append(
            "| ID | Candidate | Type | Count | Chapters | Why | Example | Suggested command |"
        )
        lines.append("|---|---|---|---:|---:|---|---|---|")
        for cand in bucket_candidates:
            lines.append(
                f"| {cand.id} | {_markdown_cell(cand.text)} | {cand.kind} | "
                f"{cand.count} | {cand.chapter_frequency} | {_markdown_cell(cand.reason or cand.kind)} | "
                f"{_candidate_example(cand)} | {_candidate_command(cand)} |"
            )
        lines.append("")
    if not rendered_any:
        lines.append("_No review candidates above the current thresholds._")
        lines.append("")

    lines.append("## Suppressed/no-action summary")
    lines.append("")
    if report.suppressed_counts:
        for reason, count in sorted(
            report.suppressed_counts.items(), key=lambda item: (-item[1], item[0])
        ):
            lines.append(f"- {count} suppressed as `{reason}`")
    else:
        lines.append("- no suppressed candidates recorded")
    if by_bucket["no_action"]:
        lines.append(
            f"- {len(by_bucket['no_action'])} no-action candidate(s) kept in JSON because `--include-common` was enabled"
        )
    lines.append("")

    metrics = report.style_metrics
    lines.append("## Style observations")
    lines.append("")
    lines.append(
        f"- records with dialogue: {metrics.record_count_with_dialogue} "
        f"({metrics.dialogue_record_ratio:.2%})"
    )
    if metrics.quote_counts:
        quote_summary = ", ".join(
            f"{k}={v}" for k, v in metrics.quote_counts.items() if v
        )
        lines.append(f"- quote styles: {quote_summary or 'none'}")
    lines.append(f"- em dashes: {metrics.em_dash_count}")
    lines.append(f"- emphasis spans: {metrics.emphasis_count}")
    if metrics.sentence_count is not None:
        avg = (
            metrics.average_sentence_words
            if metrics.average_sentence_words is not None
            else 0
        )
        lines.append(f"- sentences: {metrics.sentence_count} (avg {avg:.1f} words)")
    if metrics.capability_warnings:
        for warning in metrics.capability_warnings:
            lines.append(f"- capability: {warning}")
    lines.append("")

    return "\n".join(lines)
