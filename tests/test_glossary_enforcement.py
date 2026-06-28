"""Glossary enforcement scenarios from booktx_tenday_dekade_analysis.md.

Covers the tenday/Dekade policy: active-only default validation, labeled
inactive findings, positive approved-target enforcement, variant matching, the
QA/validation matcher agreement, and safe correction-block generation. Scenario
numbers refer to the "Test plan" section of the analysis document.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from booktx.cli import app
from booktx.config import (
    BooktxError,
    init_project,
    load_project,
    load_translation_task,
    write_translation_store,
    write_translation_task,
    write_translation_version_ledger,
)
from booktx.context import (
    GlossaryEntry,
    default_context,
    load_context,
    write_context,
    write_context_markdown,
)
from booktx.models import (
    Chunk,
    Record,
    StoredTranslationRecordV2,
    TranslationCandidate,
    TranslationStoreV2,
    TranslationSubversionLedgerEntry,
    TranslationTrackLedgerEntry,
    TranslationVersionLedger,
)
from booktx.progress import source_record_sha256
from booktx.validate import Severity, validate_project

runner = CliRunner()


def _tenday_chunk() -> Chunk:
    return Chunk(
        chunk_id="0001",
        source_language="en",
        target_language="de",
        records=[
            Record(
                id="0001-000001",
                source="He resigned a tenday later.",
            )
        ],
    )


def _tenday_glossary(
    *,
    forbid: list[str] | None = None,
    enforce: str = "error",
    source_variants: list[str] | None = None,
    target_variants: list[str] | None = None,
    require_target: bool = False,
) -> GlossaryEntry:
    kwargs: dict[str, object] = {
        "source": "tenday",
        "target": "Dekade",
        "forbidden_targets": forbid if forbid is not None else ["Zehntag"],
        "enforce": enforce,
    }
    if source_variants is not None:
        kwargs["source_variants"] = source_variants
    if target_variants is not None:
        kwargs["target_variants"] = target_variants
    if require_target:
        kwargs["require_target"] = True
    return GlossaryEntry(**kwargs)  # type: ignore[arg-type]


def _write_project(
    tmp_path: Path,
    *,
    chunk: Chunk | None = None,
    glossary_entries: list[GlossaryEntry] | None = None,
) -> Path:
    proj = init_project(tmp_path / "book", target_language="de")
    proj.chunks_dir.mkdir(parents=True, exist_ok=True)
    used_chunk = chunk if chunk is not None else _tenday_chunk()
    (proj.chunks_dir / "0001.json").write_text(
        used_chunk.model_dump_json(), encoding="utf-8"
    )
    ctx = default_context(proj)
    if glossary_entries is not None:
        ctx.glossary.extend(glossary_entries)
    write_context(proj, ctx)
    write_context_markdown(proj, ctx)
    return proj.root


def _store_record(
    *,
    chunk: Chunk,
    record_id: str,
    active_version: str,
    versions: list[tuple[str, str]],
) -> StoredTranslationRecordV2:
    rec = next(r for r in chunk.records if r.id == record_id)
    return StoredTranslationRecordV2(
        chunk_id=1,
        part_id=int(record_id.split("-")[1]),
        source_sha256=source_record_sha256(rec.source),
        source=rec.source,
        active_version=active_version,
        versions=[
            TranslationCandidate(
                version=int(ref.split(".")[0]),
                subversion=int(ref.split(".")[1]),
                version_ref=ref,
                target=target,
                status="accepted",
                created_at="2026-06-22T12:00:00Z",
                updated_at="2026-06-22T12:00:00Z",
            )
            for ref, target in versions
        ],
    )


def _write_store_and_ledger(
    proj_path: Path,
    *,
    chunk: Chunk,
    record: StoredTranslationRecordV2,
    tracked_refs: set[str],
) -> None:
    proj = load_project(proj_path)
    record_id = next(r.id for r in chunk.records)
    write_translation_store(
        proj,
        TranslationStoreV2(records={record_id: record}),
    )
    tracks: dict[str, TranslationTrackLedgerEntry] = {}
    subversions_by_track: dict[str, list[TranslationSubversionLedgerEntry]] = {}
    for ref in sorted(tracked_refs):
        major, minor = ref.split(".")
        sub = TranslationSubversionLedgerEntry(
            version=int(major),
            subversion=int(minor),
            version_ref=ref,
            context_sha256="a" * 64,
            created_at="2026-06-22T12:00:00Z",
            updated_at="2026-06-22T12:00:00Z",
        )
        subversions_by_track.setdefault(major, []).append(sub)
    for major, subs in subversions_by_track.items():
        tracks[major] = TranslationTrackLedgerEntry(
            version=int(major),
            actor="user:test",
            harness="pi",
            model="human",
            created_at="2026-06-22T12:00:00Z",
            updated_at="2026-06-22T12:00:00Z",
            subversions={str(s.subversion): s for s in subs},
        )
    active = record.active_version or sorted(tracked_refs)[0]
    write_translation_version_ledger(
        proj,
        TranslationVersionLedger(active_version=active, tracks=tracks),
    )


def _write_store_and_ledger_multi(
    proj_path: Path,
    *,
    chunk: Chunk,
    versions_by_record: dict[str, tuple[str, str] | list[tuple[str, str]]],
    tracked_refs: set[str],
) -> None:
    """Write a store with one or more records and a shared version ledger.

    Each value is either a single ``(active_ref, target)`` tuple or a list of
    ``(ref, target)`` tuples (first is active).
    """
    proj = load_project(proj_path)
    records: dict[str, StoredTranslationRecordV2] = {}
    for record_id, spec in versions_by_record.items():
        version_list = spec if isinstance(spec, list) else [spec]
        records[record_id] = _store_record(
            chunk=chunk,
            record_id=record_id,
            active_version=version_list[0][0],
            versions=version_list,
        )
    write_translation_store(proj, TranslationStoreV2(records=records))
    tracks: dict[str, TranslationTrackLedgerEntry] = {}
    subversions_by_track: dict[str, list[TranslationSubversionLedgerEntry]] = {}
    for ref in sorted(tracked_refs):
        major, minor = ref.split(".")
        sub = TranslationSubversionLedgerEntry(
            version=int(major),
            subversion=int(minor),
            version_ref=ref,
            context_sha256="a" * 64,
            created_at="2026-06-22T12:00:00Z",
            updated_at="2026-06-22T12:00:00Z",
        )
        subversions_by_track.setdefault(major, []).append(sub)
    for major, subs in subversions_by_track.items():
        tracks[major] = TranslationTrackLedgerEntry(
            version=int(major),
            actor="user:test",
            harness="pi",
            model="human",
            created_at="2026-06-22T12:00:00Z",
            updated_at="2026-06-22T12:00:00Z",
            subversions={str(s.subversion): s for s in subs},
        )
    active = sorted(tracked_refs)[-1]
    write_translation_version_ledger(
        proj,
        TranslationVersionLedger(active_version=active, tracks=tracks),
    )


# --- Scenario 1: active-only validation ignores inactive history ----------------


def test_scenario1_active_only_ignores_inactive_forbidden(tmp_path: Path) -> None:
    chunk = _tenday_chunk()
    proj_path = _write_project(
        tmp_path,
        chunk=chunk,
        glossary_entries=[_tenday_glossary(forbid=["Zehntag"], enforce="error")],
    )
    _write_store_and_ledger(
        proj_path,
        chunk=chunk,
        record=_store_record(
            chunk=chunk,
            record_id="0001-000001",
            active_version="1.2",
            versions=[
                ("1.1", "Er trat einen Zehntag später zurück."),
                ("1.2", "Er trat eine Dekade später zurück."),
            ],
        ),
        tracked_refs={"1.1", "1.2"},
    )

    report = validate_project(load_project(proj_path))

    assert report.passed, [f.as_dict() for f in report.findings]
    assert not any(f.rule == "forbidden_term_used" for f in report.findings), [
        f.as_dict() for f in report.findings
    ]


# --- Scenario 4: structural integrity stays fatal in default mode --------------


def test_scenario4_structural_missing_ledger_remains_fatal(tmp_path: Path) -> None:
    chunk = _tenday_chunk()
    proj_path = _write_project(
        tmp_path,
        chunk=chunk,
        glossary_entries=[_tenday_glossary(forbid=["Zehntag"], enforce="error")],
    )
    # Effective 1.2 is clean and tracked; inactive 1.1 (forbidden text) is NOT
    # tracked in the ledger, which is structural corruption.
    _write_store_and_ledger(
        proj_path,
        chunk=chunk,
        record=_store_record(
            chunk=chunk,
            record_id="0001-000001",
            active_version="1.2",
            versions=[
                ("1.1", "Er trat einen Zehntag später zurück."),
                ("1.2", "Er trat eine Dekade später zurück."),
            ],
        ),
        tracked_refs={"1.2"},
    )

    report = validate_project(load_project(proj_path))

    assert not report.passed, [f.as_dict() for f in report.findings]
    assert any(
        f.rule == "missing_ledger_version" and f.severity == Severity.ERROR
        for f in report.findings
    )
    # No content findings for the inactive candidate in default mode.
    assert not any(f.rule == "forbidden_term_used" for f in report.findings), [
        f.as_dict() for f in report.findings
    ]


# --- Scenario 2: history validation labels inactive violations --------------


def test_scenario2_history_labels_inactive_forbidden(tmp_path: Path) -> None:
    chunk = _tenday_chunk()
    proj_path = _write_project(
        tmp_path,
        chunk=chunk,
        glossary_entries=[_tenday_glossary(forbid=["Zehntag"], enforce="error")],
    )
    _write_store_and_ledger(
        proj_path,
        chunk=chunk,
        record=_store_record(
            chunk=chunk,
            record_id="0001-000001",
            active_version="1.2",
            versions=[
                ("1.1", "Er trat einen Zehntag später zurück."),
                ("1.2", "Er trat eine Dekade später zurück."),
            ],
        ),
        tracked_refs={"1.1", "1.2"},
    )

    report = validate_project(load_project(proj_path), include_inactive_versions=True)

    inactive_forbidden = [
        f
        for f in report.findings
        if f.rule == "forbidden_term_used" and f.candidate_scope == "inactive"
    ]
    assert inactive_forbidden, [f.as_dict() for f in report.findings]
    f = inactive_forbidden[0]
    assert f.candidate_ref == "1.1"
    assert f.candidate_scope == "inactive"
    assert f.candidate_kind == "translation"
    assert "inactive version 1.1" in f.message
    # Effective output is clean, so report.passed is True.
    assert report.passed


# --- Scenario 3: --fail-on-warnings ignores inactive history -----------------


def _build_clean_active_dirty_history(tmp_path: Path) -> Path:
    chunk = _tenday_chunk()
    proj_path = _write_project(
        tmp_path,
        chunk=chunk,
        glossary_entries=[_tenday_glossary(forbid=["Zehntag"], enforce="error")],
    )
    _write_store_and_ledger(
        proj_path,
        chunk=chunk,
        record=_store_record(
            chunk=chunk,
            record_id="0001-000001",
            active_version="1.2",
            versions=[
                ("1.1", "Er trat einen Zehntag später zurück."),
                ("1.2", "Er trat eine Dekade später zurück."),
            ],
        ),
        tracked_refs={"1.1", "1.2"},
    )
    return proj_path


def test_scenario3a_default_fail_on_warnings_exits_zero(tmp_path: Path) -> None:
    proj_path = _build_clean_active_dirty_history(tmp_path)
    res = runner.invoke(app, ["validate", str(proj_path), "--fail-on-warnings"])
    assert res.exit_code == 0, res.output


def test_scenario3b_include_inactive_displays_labeled_history(tmp_path: Path) -> None:
    proj_path = _build_clean_active_dirty_history(tmp_path)
    res = runner.invoke(
        app, ["validate", str(proj_path), "--include-inactive", "--fail-on-warnings"]
    )
    # History is displayed but does not fail the build.
    assert res.exit_code == 0, res.output
    assert "forbidden_term_used" in res.output
    assert "inactive version 1.1" in res.output


def test_scenario3c_fail_on_history_warnings_exits_nonzero(tmp_path: Path) -> None:
    proj_path = _build_clean_active_dirty_history(tmp_path)
    res = runner.invoke(
        app,
        ["validate", str(proj_path), "--fail-on-history-warnings"],
    )
    assert res.exit_code == 1, res.output


# --- Scenario 5: positive enforcement catches missing approved target ------


def test_scenario5_positive_enforcement_catches_missing_target(tmp_path: Path) -> None:
    chunk = Chunk(
        chunk_id="0001",
        source_language="en",
        target_language="de",
        records=[Record(id="0001-000001", source="He resigned a tenday later.")],
    )
    entry = GlossaryEntry(
        source="tenday",
        source_variants=["tendays"],
        target="Dekade",
        target_variants=["Dekaden"],
        require_target=True,
        forbidden_targets=["Zehntag", "zehn Tage"],
        enforce="error",
    )
    proj_path = _write_project(tmp_path, chunk=chunk, glossary_entries=[entry])
    _write_store_and_ledger(
        proj_path,
        chunk=chunk,
        record=_store_record(
            chunk=chunk,
            record_id="0001-000001",
            active_version="1.1",
            versions=[("1.1", "Er trat zehn Tage später zurück.")],
        ),
        tracked_refs={"1.1"},
    )

    report = validate_project(load_project(proj_path))

    rules = {f.rule for f in report.findings}
    assert "forbidden_term_used" in rules, [f.as_dict() for f in report.findings]
    assert "glossary_target_missing" in rules, [f.as_dict() for f in report.findings]
    assert not report.passed


# --- Scenario 6: source/target variants cover singular and plural -----------


def test_scenario6_variants_cover_singular_and_plural(tmp_path: Path) -> None:
    from booktx.glossary_match import source_rule_applies

    entry = GlossaryEntry(
        source="tenday",
        source_variants=["tendays"],
        target="Dekade",
        target_variants=["Dekaden"],
        require_target=True,
        enforce="error",
    )
    # singular and plural source forms both trigger the rule...
    assert source_rule_applies("He resigned a tenday later.", entry)
    assert source_rule_applies("They were gone for several tendays.", entry)
    # ...but substring false positives do not.
    assert not source_rule_applies("a pretenday event", entry)
    assert not source_rule_applies("he was gone for ten days", entry)


# --- Scenario 7: positive enforcement accepts inflection ---------------------


@pytest.mark.parametrize(
    "target",
    [
        "Er trat eine Dekade später zurück.",
        "Dies dauerte mehrere Dekaden.",
    ],
)
def test_scenario7_positive_enforcement_accepts_inflection(
    tmp_path: Path, target: str
) -> None:
    chunk = Chunk(
        chunk_id="0001",
        source_language="en",
        target_language="de",
        records=[Record(id="0001-000001", source="He resigned a tenday later.")],
    )
    entry = GlossaryEntry(
        source="tenday",
        source_variants=["tendays"],
        target="Dekade",
        target_variants=["Dekaden"],
        require_target=True,
        enforce="error",
    )
    proj_path = _write_project(tmp_path, chunk=chunk, glossary_entries=[entry])
    _write_store_and_ledger(
        proj_path,
        chunk=chunk,
        record=_store_record(
            chunk=chunk,
            record_id="0001-000001",
            active_version="1.1",
            versions=[("1.1", target)],
        ),
        tracked_refs={"1.1"},
    )

    report = validate_project(load_project(proj_path))

    assert "glossary_target_missing" not in {f.rule for f in report.findings}
    assert report.passed, [f.as_dict() for f in report.findings]


# --- Scenario 9: QA and validation use identical matching -------------------


def test_scenario9_qa_matches_validation_and_is_effective_only(tmp_path: Path) -> None:
    from booktx.qa_scan import qa_scan
    from booktx.status import build_status_snapshot

    chunk = Chunk(
        chunk_id="0001",
        source_language="en",
        target_language="de",
        records=[
            Record(id="0001-000001", source="He resigned a tenday later."),
            # plural source variant record
            Record(id="0001-000002", source="They were gone for several tendays."),
            # unrelated record with a forbidden target but no source term
            Record(id="0001-000003", source="A plain sentence about the weather."),
        ],
    )
    entry = GlossaryEntry(
        source="tenday",
        source_variants=["tendays"],
        target="Dekade",
        target_variants=["Dekaden"],
        require_target=True,
        forbidden_targets=["Zehntag", "Zehntage"],
        enforce="error",
    )
    proj_path = _write_project(tmp_path, chunk=chunk, glossary_entries=[entry])
    # build_status_snapshot requires a source document in source/.
    _proj0 = load_project(proj_path)
    (_proj0.source_dir / "source.md").write_text("# source\n", encoding="utf-8")
    versions_by_record = {
        "0001-000001": ("1.2", "Er trat eine Dekade später zurück."),
        "0001-000002": ("1.2", "Es dauerte mehrere Dekaden."),
        "0001-000003": ("1.1", "Ein ganz normaler Satz über Zehntag Wetter."),
    }
    _write_store_and_ledger_multi(
        proj_path,
        chunk=chunk,
        versions_by_record=versions_by_record,
        tracked_refs={"1.1", "1.2"},
    )

    # Validation: clean effective output -> passes.
    report = validate_project(load_project(proj_path))
    assert report.passed, [f.as_dict() for f in report.findings]

    # QA scan is effective-only and uses the same matcher.
    proj = load_project(proj_path)
    bundle = build_status_snapshot(proj, context_exists=True, context_ready=True)
    result = qa_scan(proj, bundle, forbidden=True, glossary=True)
    record_ids = {f.record_id for f in result.findings}
    # The unrelated record (0003) is NOT flagged even though it contains a
    # forbidden target string, because its source has no tenday/tendays.
    assert "0001-000003" not in record_ids, [f.as_dict() for f in result.findings]
    # And the clean effective records are not flagged.
    assert record_ids == set(), [f.as_dict() for f in result.findings]


# --- Scenario 8: --enforce off guard + mandate-term ------------------------


def test_scenario8_enforce_off_guard_rejects_mandatory(tmp_path: Path) -> None:
    proj_path = _write_project(tmp_path)
    res = runner.invoke(
        app,
        [
            "context",
            "reset-term",
            str(proj_path),
            "tenday",
            "--create",
            "--target",
            "Dekade",
            "--forbid",
            "Zehntag",
            "--enforce",
            "off",
        ],
    )
    assert res.exit_code != 0, res.output
    assert "refusing to disable a mandatory glossary rule" in res.output


def test_scenario8_enforce_off_guard_allows_with_flag(tmp_path: Path) -> None:
    proj_path = _write_project(tmp_path)
    res = runner.invoke(
        app,
        [
            "context",
            "reset-term",
            str(proj_path),
            "tenday",
            "--create",
            "--target",
            "Dekade",
            "--forbid",
            "Zehntag",
            "--enforce",
            "off",
            "--allow-disable-enforcement",
        ],
    )
    assert res.exit_code == 0, res.output


def test_scenario8_advisory_target_only_enforce_off_is_warn_free(
    tmp_path: Path,
) -> None:
    # An approved advisory entry (target set, no require, no forbid) may use
    # enforce=off without failure and without a validation warning.
    chunk = _tenday_chunk()
    entry = GlossaryEntry(source="tenday", target="Dekade", enforce="off")
    proj_path = _write_project(tmp_path, chunk=chunk, glossary_entries=[entry])
    _write_store_and_ledger(
        proj_path,
        chunk=chunk,
        record=_store_record(
            chunk=chunk,
            record_id="0001-000001",
            active_version="1.1",
            versions=[("1.1", "Er trat eine Woche später zurück.")],
        ),
        tracked_refs={"1.1"},
    )
    report = validate_project(load_project(proj_path))
    assert report.passed, [f.as_dict() for f in report.findings]
    assert not any(f.rule == "glossary_target_missing" for f in report.findings)


def test_scenario8_mandate_term_records_binding_decision(tmp_path: Path) -> None:
    proj_path = _write_project(tmp_path)
    res = runner.invoke(
        app,
        [
            "context",
            "mandate-term",
            str(proj_path),
            "tenday",
            "--target",
            "Dekade",
            "--source-variant",
            "tendays",
            "--target-variant",
            "Dekaden",
            "--forbid",
            "Zehntag",
            "--forbid",
            "Zehntage",
        ],
    )
    assert res.exit_code == 0, res.output
    from booktx.context import load_context

    ctx = load_context(load_project(proj_path))
    entry = next(e for e in ctx.glossary if e.source == "tenday")
    assert entry.require_target is True
    assert entry.enforce == "error"
    assert entry.source_variants == ["tendays"]
    assert entry.target_variants == ["Dekaden"]
    assert entry.forbidden_targets == ["Zehntag", "Zehntage"]


def test_scenario8_mandate_term_rejects_enforce_off(tmp_path: Path) -> None:
    proj_path = _write_project(tmp_path)
    res = runner.invoke(
        app,
        [
            "context",
            "mandate-term",
            str(proj_path),
            "tenday",
            "--target",
            "Dekade",
            "--enforce",
            "off",
        ],
    )
    assert res.exit_code != 0, res.output
    assert "cannot disable enforcement" in res.output


# --- Scenario 10: generated correction blocks are safe ---------------------


def test_scenario10_audit_term_generates_safe_blocks(tmp_path: Path) -> None:
    from booktx.submissions import parse_block_submission

    chunk = Chunk(
        chunk_id="0001",
        source_language="en",
        target_language="de",
        records=[
            Record(id="0001-000001", source="He resigned a tenday later."),
            Record(id="0001-000002", source="A clean tenday record."),
        ],
    )
    entry = GlossaryEntry(
        source="tenday",
        source_variants=["tendays"],
        target="Dekade",
        target_variants=["Dekaden"],
        require_target=True,
        forbidden_targets=["Zehntag", "zehn Tage"],
        enforce="error",
    )
    proj_path = _write_project(tmp_path, chunk=chunk, glossary_entries=[entry])
    _proj0 = load_project(proj_path)
    (_proj0.source_dir / "source.md").write_text("# source\n", encoding="utf-8")
    _write_store_and_ledger_multi(
        proj_path,
        chunk=chunk,
        versions_by_record={
            # 001 violates: forbidden 'zehn Tage' and missing approved target.
            "0001-000001": ("1.1", "Er trat zehn Tage später zurück."),
            # 002 is clean (effective target present).
            "0001-000002": ("1.1", "Eine saubere Dekade Aufzeichnung."),
        },
        tracked_refs={"1.1"},
    )

    block_path = tmp_path / "ingest" / "glossary-tenday-fixes.block.txt"
    res = runner.invoke(
        app,
        [
            "context",
            "audit-term",
            str(proj_path),
            "tenday",
            "--write-block",
            str(block_path),
        ],
    )
    assert res.exit_code == 0, res.output
    assert "records with source term: 2" in res.output
    assert "forbidden target violations: 1" in res.output

    # Ingest block contains only the violating record header + target.
    ingest = block_path.read_text(encoding="utf-8")
    assert ">>> 0001-000001" in ingest
    assert ">>> 0001-000002" not in ingest  # clean record excluded
    assert "# source:" not in ingest
    assert "# current:" not in ingest
    # And it parses cleanly with the block parser.
    parsed = parse_block_submission(ingest)
    assert [r.id for r in parsed.records] == ["0001-000001"]

    # Companion source block carries source + current target, reference-only.
    companion = block_path.with_name("glossary-tenday-fixes.source.block.txt")
    assert companion.is_file()
    source_block = companion.read_text(encoding="utf-8")
    assert "# source:" in source_block
    assert "# current:" in source_block
    assert "Reference only" in source_block


# --- Scenario 11: stale task context enforcement --------------------------


def _mandated_project(tmp_path: Path) -> tuple[Path, str]:
    """Build a full project with a mandated tenday term.

    Returns ``(project_dir, first_record_id)``.
    """
    src = tmp_path / "tenday.md"
    src.write_text("# Tenday\n\nHe resigned a tenday later.\n", encoding="utf-8")
    project_dir = tmp_path / "tenday-book"
    res = runner.invoke(
        app,
        [
            "init",
            str(project_dir),
            "--target",
            "de",
            "--source-file",
            str(src),
            "--chunk-size",
            "5",
        ],
    )
    assert res.exit_code == 0, res.output
    assert runner.invoke(app, ["extract", str(project_dir)]).exit_code == 0
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    runner.invoke(
        app,
        [
            "context",
            "mandate-term",
            str(project_dir),
            "tenday",
            "--target",
            "Dekade",
            "--source-variant",
            "tendays",
            "--target-variant",
            "Dekaden",
            "--forbid",
            "Zehntag",
        ],
    )
    runner.invoke(
        app,
        [
            "context",
            "mark-ready",
            str(project_dir),
            "--force",
            "--reason",
            "test setup",
        ],
    )
    chunks = sorted((project_dir / ".booktx" / "chunks").glob("*.json"))
    first_id = json.loads(chunks[0].read_text("utf-8"))["records"][0]["id"]
    return project_dir, first_id


def test_scenario11_stale_task_blocks_submission(tmp_path: Path) -> None:
    import pytest as _pytest

    from booktx.acceptance import SubmittedRecord, accept_translation_records
    from booktx.status import build_status_snapshot

    project_dir, first_id = _mandated_project(tmp_path)
    next_res = runner.invoke(
        app,
        ["translate", "next", str(project_dir), "--unit", "paragraph", "--json"],
    )
    assert next_res.exit_code == 0, next_res.output
    task_payload = json.loads(next_res.output)
    proj = load_project(project_dir)
    task = load_translation_task(proj, task_payload["task_id"])
    assert task is not None
    assert task.mandatory_glossary_sha256 is not None

    # Change the binding glossary decision after task creation.
    ctx = load_context(proj)
    assert ctx is not None
    entry = next(e for e in ctx.glossary if e.source == "tenday")
    entry.forbidden_targets = ["Zehntag", "zehn Tage"]
    write_context(proj, ctx)

    bundle = build_status_snapshot(proj, context_exists=True, context_ready=True)
    with _pytest.raises(BooktxError) as exc_info:
        accept_translation_records(
            proj,
            [SubmittedRecord(id=first_id, target="Er trat eine Dekade später zurück.")],
            bundle=bundle,
            task=task,
            submission_translation_version=task.translation_version,
            enforce_task_version=True,
        )
    assert exc_info.value.code == "task_context_policy_stale"


def test_scenario11_chapter_note_change_does_not_stale(tmp_path: Path) -> None:
    from booktx.acceptance import SubmittedRecord, accept_translation_records
    from booktx.status import build_status_snapshot

    project_dir, first_id = _mandated_project(tmp_path)
    next_res = runner.invoke(
        app,
        ["translate", "next", str(project_dir), "--unit", "paragraph", "--json"],
    )
    assert next_res.exit_code == 0, next_res.output
    task_payload = json.loads(next_res.output)
    proj = load_project(project_dir)
    task = load_translation_task(proj, task_payload["task_id"])
    assert task is not None

    # A chapter-note-only change is non-binding and must not stale the task.
    ctx = load_context(proj)
    assert ctx is not None
    from booktx.context import ChapterContext

    ctx.chapter_contexts.append(ChapterContext(chapter_id="0001", source_summary="old"))
    write_context(proj, ctx)

    # Now mutate a chapter note (non-binding).
    ctx2 = load_context(proj)
    ctx2.chapter_contexts[0].source_summary = "Keep the calendar term consistent."
    write_context(proj, ctx2)

    bundle = build_status_snapshot(proj, context_exists=True, context_ready=True)
    # Must not raise task_context_policy_stale.
    accept_translation_records(
        proj,
        [SubmittedRecord(id=first_id, target="Er trat eine Dekade später zurück.")],
        bundle=bundle,
        task=task,
        submission_translation_version=task.translation_version,
        enforce_task_version=True,
    )


def test_scenario11_legacy_task_remains_loadable_and_warns(tmp_path: Path) -> None:
    import warnings

    from booktx.acceptance import SubmittedRecord, accept_translation_records
    from booktx.status import build_status_snapshot

    project_dir, first_id = _mandated_project(tmp_path)
    next_res = runner.invoke(
        app,
        ["translate", "next", str(project_dir), "--unit", "paragraph", "--json"],
    )
    assert next_res.exit_code == 0, next_res.output
    task_payload = json.loads(next_res.output)
    proj = load_project(project_dir)
    task = load_translation_task(proj, task_payload["task_id"])
    assert task is not None
    # Simulate a legacy task that predates the fingerprint field.
    task.mandatory_glossary_sha256 = None
    write_translation_task(proj, task)

    bundle = build_status_snapshot(proj, context_exists=True, context_ready=True)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        accept_translation_records(
            proj,
            [SubmittedRecord(id=first_id, target="Er trat eine Dekade später zurück.")],
            bundle=bundle,
            task=task,
            submission_translation_version=task.translation_version,
            enforce_task_version=True,
        )
    assert any("predates mandatory_glossary_sha256" in str(w.message) for w in caught)
