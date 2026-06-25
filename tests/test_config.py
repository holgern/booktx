"""Tests for booktx.config: project layout, config read/write, source resolution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from booktx.config import (
    BooktxError,
    create_profile,
    detect_format,
    find_source_file,
    identity_path,
    init_project,
    init_source_project,
    load_identity,
    load_names,
    load_profile_config,
    load_project,
    load_source_project,
    load_translation_store,
    load_translation_task,
    load_translation_version_ledger,
    project_source_sha256,
    translation_ingest_block_path,
    translation_ingest_path,
    translation_review_dir,
    translation_review_ingest_block_path,
    translation_review_source_block_path,
    translation_review_task_path,
    translation_store_path,
    translation_task_path,
    translation_task_source_block_path,
    translation_version_ledger_path,
    write_identity,
    write_profile_config,
    write_translation_store,
    write_translation_task,
    write_translation_version_ledger,
)
from booktx.context import (
    ChapterContext,
    baseline_sha256,
    context_path,
    default_context,
    write_context,
)
from booktx.models import (
    StoredTranslationRecordV2,
    TranslationCandidate,
    TranslationIdentity,
    TranslationStoreV2,
    TranslationSubversionLedgerEntry,
    TranslationTask,
    TranslationTrackLedgerEntry,
    TranslationVersionLedger,
)
from booktx.versioning import canonical_json_sha256, resolve_current_version


def test_detect_format():
    assert detect_format("book.md") == "markdown"
    assert detect_format("book.epub") == "epub"
    with pytest.raises(BooktxError):
        detect_format("book.pdf")


def test_init_creates_full_layout(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    for d in (
        proj.source_dir,
        proj.booktx_dir,
        proj.chunks_dir,
        proj.translated_dir,
        proj.tasks_dir,
        proj.ingest_dir,
        proj.reports_dir,
        proj.output_dir,
    ):
        assert d.is_dir(), f"{d} missing"
    assert proj.config_path.is_file()
    assert proj.names_path.is_file()
    assert proj.config.target_language == "de"
    assert proj.config.source_language == "en"
    # empty names.json
    assert load_names(proj).protected_terms == []


def test_init_copies_supplied_source(tmp_path: Path, monkeypatch):
    src = tmp_path / "novel.md"
    src.write_text("# Title\n", encoding="utf-8")
    proj = init_project(
        tmp_path / "book",
        target_language="de",
        source_file=src,
    )
    assert (proj.source_dir / "novel.md").is_file()
    assert proj.config.source_file == "novel.md"
    assert proj.config.format == "markdown"


def test_init_rejects_unsupported_source(tmp_path: Path):
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"%PDF-1.4")
    with pytest.raises(BooktxError):
        init_project(tmp_path / "book", target_language="de", source_file=src)

    from booktx.config import tomllib

    proj = init_project(tmp_path / "book", target_language="fr", source_language="en")
    cfg_path = proj.config_path
    # rewrite chunk_size and reload via TOML round-trip
    with cfg_path.open("rb") as fh:
        data = tomllib.load(fh)
    data["chunk_size"] = 25
    import tomli_w

    cfg_path.write_bytes(tomli_w.dumps(data).encode("utf-8"))
    proj2 = load_project(tmp_path / "book")
    assert proj2.config.chunk_size == 25
    assert proj2.config.target_language == "fr"


def test_load_project_rejects_non_project(tmp_path: Path):
    with pytest.raises(BooktxError):
        load_project(tmp_path / "nope")


def test_find_source_file_requires_exactly_one(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    # No source file -> error
    with pytest.raises(BooktxError):
        find_source_file(proj)
    # Two sources -> ambiguous
    (proj.source_dir / "a.md").write_text("a", encoding="utf-8")
    (proj.source_dir / "b.md").write_text("b", encoding="utf-8")
    with pytest.raises(BooktxError):
        find_source_file(proj)


def test_find_source_file_syncs_config(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    (proj.source_dir / "story.md").write_text("# Hi\n", encoding="utf-8")
    found = find_source_file(proj)
    assert found.name == "story.md"
    # config should now reflect the discovered file
    proj2 = load_project(tmp_path / "book")
    assert proj2.config.source_file == "story.md"
    assert proj2.config.format == "markdown"


def test_translation_store_helpers_roundtrip(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    (proj.source_dir / "story.md").write_text("# Hi\n", encoding="utf-8")
    proj = load_project(proj.root)

    assert translation_store_path(proj).name == "translation-store.json"

    empty = load_translation_store(proj)
    assert empty.records == {}

    store = TranslationStoreV2(
        source_sha256=project_source_sha256(proj),
        records={
            "0001-000001": StoredTranslationRecordV2(
                chunk_id=1,
                part_id=1,
                source_sha256="abc123",
                source="Hi",
                active_version="1.1",
                versions=[
                    TranslationCandidate(
                        version=1,
                        subversion=1,
                        version_ref="1.1",
                        target="Hallo.",
                        created_at="2026-06-22T12:00:00Z",
                        updated_at="2026-06-22T12:00:00Z",
                    )
                ],
            )
        },
    )
    write_translation_store(proj, store)

    loaded = load_translation_store(proj)
    assert loaded == store


def test_translation_version_ledger_helpers_roundtrip(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")

    assert (
        translation_version_ledger_path(proj).name == "translation-version-ledger.json"
    )
    empty = load_translation_version_ledger(proj)
    assert empty.tracks == {}

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
                        created_at="2026-06-22T12:00:00Z",
                        updated_at="2026-06-22T12:00:00Z",
                    )
                },
            )
        },
    )
    write_translation_version_ledger(proj, ledger)

    assert load_translation_version_ledger(proj) == ledger


def test_identity_helpers_roundtrip(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")

    assert identity_path(proj).name == "identity.json"
    assert load_identity(proj) == TranslationIdentity(
        actor="user:unknown",
        harness="booktx",
        model="human",
    )

    identity = TranslationIdentity(
        actor="user:nahrstaedt",
        harness="pi",
        model="codex-openai/gpt-5.5@low",
    )
    write_identity(proj, identity)

    assert load_identity(proj) == identity


def test_canonical_context_sha_uses_canonical_json(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    ctx = default_context(proj)
    ctx.ready = True
    write_context(proj, ctx)
    payload = json.loads(context_path(proj).read_text("utf-8"))

    expected = canonical_json_sha256(payload)
    assert expected == canonical_json_sha256(ctx.model_dump(mode="json", by_alias=True))


def _make_project_with_context(tmp_path: Path) -> Path:
    src = tmp_path / "story.md"
    src.write_text("# Hi\n\nHello there.\n", encoding="utf-8")
    proj = init_project(tmp_path / "book", target_language="de", source_file=src)
    ctx = default_context(proj)
    ctx.ready = True
    write_context(proj, ctx)
    return proj.root


def test_version_resolution_reuses_same_identity_and_context(tmp_path: Path):
    proj = load_project(_make_project_with_context(tmp_path))
    write_identity(
        proj,
        TranslationIdentity(
            actor="user:nahrstaedt",
            harness="pi",
            model="codex-openai/gpt-5.5@low",
        ),
    )

    first = resolve_current_version(proj)
    second = resolve_current_version(proj)

    assert first.version_ref == "1.1"
    assert second.version_ref == "1.1"
    assert first.baseline_sha256 == second.baseline_sha256
    assert first.context_sha256 == first.baseline_sha256
    assert second.created_track is False
    assert second.created_subversion is False


def test_version_resolution_keeps_same_subversion_for_chapter_note_only_change(
    tmp_path: Path,
):
    proj = load_project(_make_project_with_context(tmp_path))
    write_identity(
        proj,
        TranslationIdentity(
            actor="user:nahrstaedt",
            harness="pi",
            model="codex-openai/gpt-5.5@low",
        ),
    )

    first = resolve_current_version(proj)
    ctx = default_context(proj)
    ctx.ready = True
    ctx.chapter_contexts.append(
        ChapterContext(
            chapter_id="0001",
            title="One",
            translation_summary="Completed chapter one.",
        )
    )
    write_context(proj, ctx)

    second = resolve_current_version(proj)

    assert first.version_ref == "1.1"
    assert second.version_ref == "1.1"
    assert first.baseline_sha256 == second.baseline_sha256
    assert second.created_subversion is False


def test_version_resolution_creates_new_subversion_on_baseline_change(tmp_path: Path):
    proj = load_project(_make_project_with_context(tmp_path))
    write_identity(
        proj,
        TranslationIdentity(
            actor="user:nahrstaedt",
            harness="pi",
            model="codex-openai/gpt-5.5@low",
        ),
    )

    first = resolve_current_version(proj)
    ctx = default_context(proj)
    ctx.ready = True
    ctx.global_rules.append("Prefer shorter German clauses.")
    write_context(proj, ctx)

    second = resolve_current_version(proj)

    assert first.version_ref == "1.1"
    assert second.version_ref == "1.2"
    assert second.baseline_sha256 == baseline_sha256(ctx)
    assert second.created_track is False
    assert second.created_subversion is True


def test_version_resolution_creates_new_major_track_on_model_change(tmp_path: Path):
    proj = load_project(_make_project_with_context(tmp_path))
    write_identity(
        proj,
        TranslationIdentity(
            actor="user:nahrstaedt",
            harness="pi",
            model="codex-openai/gpt-5.5@low",
        ),
    )
    first = resolve_current_version(proj)
    write_identity(
        proj,
        TranslationIdentity(
            actor="user:nahrstaedt",
            harness="pi",
            model="codex-openai/gpt-5.4-mini@low",
        ),
    )

    second = resolve_current_version(proj)

    assert first.version_ref == "1.1"
    assert second.version_ref == "2.1"


def test_translation_task_helpers_roundtrip(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    task = TranslationTask(
        task_id="bt-task-1",
        unit="batch",
        source_language="en",
        target_language="de",
    )

    assert translation_task_path(proj, "bt-task-1").name == "bt-task-1.json"
    assert translation_ingest_path(proj, "bt-task-1").name == "bt-task-1.json"
    assert (
        translation_ingest_block_path(proj, "bt-task-1").name == "bt-task-1.block.txt"
    )
    assert (
        translation_task_source_block_path(proj, "bt-task-1").name
        == "bt-task-1.source.block.txt"
    )
    assert (
        translation_task_source_block_path(proj, "bt-task-1").parent == proj.tasks_dir
    )
    with pytest.raises(BooktxError):
        translation_task_source_block_path(proj, "bt-task/1")
    assert load_translation_task(proj, "bt-task-1") is None

    write_translation_task(proj, task)

    loaded = load_translation_task(proj, "bt-task-1")
    assert loaded == task


def test_init_project_with_target_creates_default_profile(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")

    assert proj.layout_version == "profiles"
    assert proj.profile == "de_default"
    assert (
        translation_store_path(proj)
        == proj.root / "translations" / "de_default" / "translation-store.json"
    )
    assert proj.output_dir == proj.root / "translations" / "de_default" / "output"


def test_source_only_project_loads_without_profile(tmp_path: Path):
    proj = init_source_project(tmp_path / "book")
    loaded = load_source_project(proj.root)

    assert loaded.layout_version == "profiles"
    assert loaded.profile is None
    assert loaded.output_dir is None


def test_load_project_auto_resolves_single_profile(tmp_path: Path):
    proj = init_source_project(tmp_path / "book")
    create_profile(proj.root, "de_gpt5_5", target_language="de")

    loaded = load_project(proj.root)

    assert loaded.profile == "de_gpt5_5"
    assert loaded.config.target_language == "de"


def test_load_project_rejects_ambiguous_profiles_when_required(tmp_path: Path):
    proj = init_source_project(tmp_path / "book")
    create_profile(proj.root, "de_gpt5_5", target_language="de")
    create_profile(proj.root, "fr_gpt5_5", target_language="fr")

    with pytest.raises(BooktxError, match="multiple translation profiles exist"):
        load_project(proj.root, require_profile=True)


def test_profile_config_kind_roundtrips_through_toml(tmp_path: Path):
    proj = init_source_project(tmp_path / "book")
    create_profile(
        proj.root,
        "pt",
        target_language="en",
        kind="pass-through",
    )

    cfg = load_profile_config(proj.root, "pt")
    assert cfg.kind == "pass-through"
    # Normal profiles continue to serialize as translation.
    create_profile(proj.root, "de_default", target_language="de")
    assert load_profile_config(proj.root, "de_default").kind == "translation"


def test_profile_config_quality_review_omitted_when_unset(tmp_path: Path):
    """A profile created without quality_review must not gain a table on write."""
    proj = init_source_project(tmp_path / "book")
    create_profile(proj.root, "de_default", target_language="de")
    cfg = load_profile_config(proj.root, "de_default")
    assert cfg.quality_review is None
    # Re-writing must not introduce a [quality_review] table.
    write_profile_config(proj.root, cfg)
    from booktx.config import profile_config_path

    raw = profile_config_path(proj.root, "de_default").read_text("utf-8")
    assert "quality_review" not in raw
    # And it still loads back clean.
    assert load_profile_config(proj.root, "de_default").quality_review is None


def test_profile_config_quality_review_roundtrips_when_set(tmp_path: Path):
    from booktx.models import QualityReviewConfig, ReviewPassConfig

    proj = init_source_project(tmp_path / "book")
    create_profile(proj.root, "de_default", target_language="de")
    cfg = load_profile_config(proj.root, "de_default")
    cfg = cfg.model_copy(
        update={
            "quality_review": QualityReviewConfig(
                enabled=True,
                active_passes=[1, 2],
                passes=[
                    ReviewPassConfig(pass_number=1, name="Flow", enforce="warn"),
                    ReviewPassConfig(
                        pass_number=2,
                        name="Polish",
                        base="active_review",
                        required_base_pass=1,
                        enforce="error",
                    ),
                ],
            )
        }
    )
    write_profile_config(proj.root, cfg)
    loaded = load_profile_config(proj.root, "de_default")
    assert loaded.quality_review is not None
    assert loaded.quality_review.enabled is True
    assert loaded.quality_review.active_passes == [1, 2]
    assert [p.pass_number for p in loaded.quality_review.passes] == [1, 2]
    assert loaded.quality_review.passes[1].base == "active_review"


def test_review_path_helpers_resolve_profile_local(tmp_path: Path):
    from booktx.models import TranslationReviewTask, TranslationReviewTaskRecord

    proj = init_source_project(tmp_path / "book")
    create_profile(proj.root, "de_default", target_language="de")
    loaded = load_project(proj.root)
    # Select the profile so path helpers resolve.
    from booktx.config import select_profile

    select_profile(loaded.root, "de_default")
    proj2 = load_project(loaded.root)
    rid = "btr-20260625T120000Z-0002-r1-a1b2c3d4"
    assert translation_review_dir(proj2).name == "reviews"
    assert translation_review_task_path(proj2, rid).name == f"{rid}.json"
    assert (
        translation_review_source_block_path(proj2, rid).name
        == f"{rid}.source.block.txt"
    )
    assert translation_review_ingest_block_path(proj2, rid).name == f"{rid}.block.txt"
    # write + load round-trip
    task = TranslationReviewTask(
        review_task_id=rid,
        profile="de_default",
        chapter_id="0002",
        pass_number=1,
        source_language="en",
        target_language="de",
        before_records=2,
        after_records=2,
        source_words=10,
        record_count=1,
        created_at="t",
        records=[
            TranslationReviewTaskRecord(
                id="0002-000017",
                chunk_id="0002",
                source="src",
                base_kind="translation",
                base_ref="1.1",
                base_target="t",
                base_target_sha256="h",
                review_ref="R1.1",
                pass_number=1,
                review_window_sha256="w",
            )
        ],
    )
    from booktx.config import (
        load_translation_review_task,
        write_translation_review_task,
    )

    translation_review_dir(proj2).mkdir(parents=True, exist_ok=True)
    write_translation_review_task(proj2, task)
    loaded_task = load_translation_review_task(proj2, rid)
    assert loaded_task is not None
    assert loaded_task.records[0].review_ref == "R1.1"
