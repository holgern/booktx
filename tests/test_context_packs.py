"""Unit tests for booktx.context_packs (series-wide context packs)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from booktx.cli import app
from booktx.config import load_project
from booktx.context import (
    ContextQuestion,
    GlossaryEntry,
    StyleProfile,
    TranslationContext,
    context_markdown_path,
    context_path,
    unapproved_required_questions,
)
from booktx.context_packs import (
    CORE_QUESTION_STYLE_FIELDS,
    ContextPackError,
    ContextPackImportResult,
    SeriesContextPack,
    collapse_whitespace,
    export_context_pack,
    glossary_identity,
    normalize_glossary_entry,
    parse_context_pack,
    plan_context_pack_import,
    question_identity,
    read_context_pack,
    validate_pack_glossary,
    validate_pack_questions,
    write_context_pack,
)

runner = CliRunner(env={"COLUMNS": "120"})

MARKDOWN_DOC = """\
# One

The Wasp Empire has commenced its great war against the Lowlands.
"""


def _make_project(tmp_path: Path, *, ready: bool = False) -> Path:
    project_dir = tmp_path / "book"
    src = tmp_path / "novel.md"
    src.write_text(MARKDOWN_DOC, encoding="utf-8")
    res = runner.invoke(
        app,
        ["init", str(project_dir), "--target", "de", "--source-file", str(src)],
    )
    assert res.exit_code == 0, res.output
    res = runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    assert res.exit_code == 0, res.output
    if ready:
        _force_ready(project_dir)
    return project_dir


def _force_ready(project_dir: Path) -> None:
    """Answer required core questions via the CLI and mark the context ready.

    Uses `context answer` so core answers are applied to their mapped style
    fields, keeping the context internally consistent for pack export/import.
    """
    answers = [
        ("Q001", "de-DE"),
        ("Q002", "balanced"),
        ("Q003", "neutral"),
        ("Q004", "natural dialogue"),
        ("Q005", "keep Apt names"),
        ("Q006", "translate world terms"),
        ("Q012", "error"),
    ]
    for qid, text in answers:
        res = runner.invoke(
            app,
            ["context", "answer", str(project_dir), qid, "--text", text],
        )
        assert res.exit_code == 0, res.output
    res = runner.invoke(app, ["context", "mark-ready", str(project_dir)])
    assert res.exit_code == 0, res.output


def _pack_dict(**overrides: object) -> dict:
    base: dict[str, object] = {
        "format": "booktx.series-context-pack",
        "version": 1,
        "series_id": "shadows-of-apt",
        "source_language": "en",
        "target_language": "de",
        "created_at": "2024-01-01T00:00:00Z",
    }
    base.update(overrides)
    return base


def _empire_entry() -> GlossaryEntry:
    return GlossaryEntry(
        source="empire",
        target="Imperium",
        forbidden_targets=["Reich"],
        category="concept",
        status="approved",
        notes="Series-wide political entity term. Do not use Reich.",
        case_sensitive=False,
        enforce="error",
    )


# --- 1. strict schema and series_id validation --------------------------------


@pytest.mark.parametrize("bad_id", ["", " bad", "bad id", "1 bad", "bad!", ".bad"])
def test_series_id_rejects_invalid_values(bad_id: str):
    with pytest.raises(ValidationError):
        SeriesContextPack.model_validate(_pack_dict(series_id=bad_id))


def test_format_and_version_are_fixed_literals():
    with pytest.raises(ValidationError):
        SeriesContextPack.model_validate({**_pack_dict(), "format": "something.else"})
    with pytest.raises(ValidationError):
        SeriesContextPack.model_validate({**_pack_dict(), "version": 2})


def test_extra_fields_are_forbidden():
    with pytest.raises(ValidationError):
        SeriesContextPack.model_validate({**_pack_dict(), "unexpected": True})


def test_languages_must_be_nonempty():
    with pytest.raises(ValidationError):
        SeriesContextPack.model_validate(_pack_dict(source_language=""))
    with pytest.raises(ValidationError):
        SeriesContextPack.model_validate(_pack_dict(target_language="   "))


# --- 2/3. export selection and exclusions -------------------------------------


def test_export_includes_only_selected_reusable_fields(tmp_path: Path):
    project_dir = _make_project(tmp_path, ready=True)
    proj = load_project(project_dir)
    # Add a glossary entry and a global rule to verify they are exported.
    res = runner.invoke(
        app,
        [
            "context",
            "reset-term",
            str(project_dir),
            "empire",
            "--target",
            "Imperium",
            "--forbid",
            "Reich",
            "--category",
            "concept",
            "--enforce",
            "error",
            "--create",
        ],
    )
    assert res.exit_code == 0, res.output
    pack = export_context_pack(proj, series_id="shadows-of-apt")
    assert pack.source_language == "en"
    assert pack.target_language == "de"
    assert pack.style is not None
    assert any(g.source == "empire" for g in pack.glossary)


def test_export_excludes_source_book_readiness_and_chapter_fields(tmp_path: Path):
    project_dir = _make_project(tmp_path, ready=True)
    proj = load_project(project_dir)
    pack = export_context_pack(proj, series_id="s")
    dumped = pack.model_dump(mode="json", by_alias=True)
    for forbidden in (
        "ready",
        "ready_forced",
        "ready_reason",
        "ready_by",
        "ready_at",
        "source_title",
        "source_author",
        "source_sha256",
        "chapter_contexts",
    ):
        assert forbidden not in dumped, f"pack leaked field {forbidden!r}"
    # The pack's own version is the format schema version (1), not the
    # TranslationContext book-local version field.
    assert dumped["version"] == 1


def test_export_requires_readiness_unless_overridden(tmp_path: Path):
    project_dir = _make_project(tmp_path, ready=False)
    proj = load_project(project_dir)
    with pytest.raises(ContextPackError) as exc:
        export_context_pack(proj, series_id="s")
    assert exc.value.code == "context_not_ready"
    # Override permitted.
    pack = export_context_pack(proj, series_id="s", allow_not_ready=True)
    assert pack.series_id == "s"


def test_export_refuses_forced_ready_unless_overridden(tmp_path: Path):
    project_dir = tmp_path / "book"
    src = tmp_path / "novel.md"
    src.write_text(MARKDOWN_DOC, encoding="utf-8")
    runner.invoke(
        app, ["init", str(project_dir), "--target", "de", "--source-file", str(src)]
    )
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
    proj = load_project(project_dir)
    path = context_path(proj)
    data = json.loads(path.read_text("utf-8"))
    data["ready"] = True
    data["ready_forced"] = True
    path.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
    with pytest.raises(ContextPackError) as exc:
        export_context_pack(proj, series_id="s")
    assert exc.value.code == "context_ready_forced"


def test_export_refuses_unsafe_markdown_drift(tmp_path: Path):
    project_dir = _make_project(tmp_path, ready=True)
    proj = load_project(project_dir)
    md_path = context_markdown_path(proj)
    md = md_path.read_text("utf-8")
    md = md.replace(
        "## Rules for agents",
        "## Chapter notes\n\n### 0001\n- Decision: never use Reich\n\n"
        "## Rules for agents",
        1,
    )
    md_path.write_text(md, encoding="utf-8")
    with pytest.raises(ValueError):
        export_context_pack(proj, series_id="s")


# --- 6. export excludes agent and forced answers ------------------------------


def test_export_excludes_agent_and_forced_answers(tmp_path: Path):
    project_dir = _make_project(tmp_path, ready=True)
    proj = load_project(project_dir)
    path = context_path(proj)
    data = json.loads(path.read_text("utf-8"))
    # Reassign answer_source across the five reusable sources, keeping the
    # answer texts unchanged so core/style consistency is preserved.
    sources = ["user", "imported", "legacy", "forced", "agent"]
    answered = [q for q in data["questions"] if q.get("status") == "answered"]
    assert len(answered) >= 5
    for q, src in zip(answered, sources, strict=False):
        q["answer_source"] = src
    path.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
    pack = export_context_pack(proj, series_id="s")
    exported_sources = {q.answer_source for q in pack.questions}
    assert exported_sources == {"user", "imported", "legacy"}
    assert "agent" not in exported_sources
    assert "forced" not in exported_sources


# --- 7. global-rule normalization and ordering --------------------------------


def test_global_rules_are_normalized_and_exported(tmp_path: Path):
    project_dir = _make_project(tmp_path, ready=True)
    proj = load_project(project_dir)
    path = context_path(proj)
    data = json.loads(path.read_text("utf-8"))
    data["global_rules"] = ["Keep   names   intact", "  No Reich  ", "", "   "]
    path.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
    pack = export_context_pack(proj, series_id="s")
    assert pack.global_rules == ["Keep names intact", "No Reich"]
    # Empty rules dropped after normalization.
    assert all(r.strip() for r in pack.global_rules)


def test_global_rule_validator_rejects_empty_after_normalization():
    with pytest.raises(ValidationError):
        SeriesContextPack.model_validate({**_pack_dict(), "global_rules": ["   "]})


# --- 8/9/10. glossary identity, equality, validation --------------------------


def test_glossary_identity_is_normalized_source_only():
    assert glossary_identity(GlossaryEntry(source=" Empire ")) == "empire"
    other = GlossaryEntry(source="EMPIRE", category="place", case_sensitive=True)
    assert glossary_identity(other) == glossary_identity(_empire_entry())


def test_normalize_glossary_entry_trims_and_dedupes():
    entry = GlossaryEntry(
        source=" empire ",
        source_variants=[" Empires ", "Empires", ""],
        target=" Imperium ",
        forbidden_targets=[" Reich ", "Reich"],
        notes="  note  ",
        category=" concept ",
    )
    norm = normalize_glossary_entry(entry)
    assert norm.source == "empire"
    assert norm.source_variants == ["Empires"]
    assert norm.target == "Imperium"
    assert norm.forbidden_targets == ["Reich"]
    assert norm.notes == "note"
    assert norm.category == "concept"


def test_validate_pack_glossary_rejects_empty_source():
    with pytest.raises(ContextPackError):
        validate_pack_glossary([GlossaryEntry(source="  ")])


def test_validate_pack_glossary_rejects_duplicate_identity():
    with pytest.raises(ContextPackError) as exc:
        validate_pack_glossary(
            [GlossaryEntry(source="empire"), GlossaryEntry(source="EMPIRE")]
        )
    assert exc.value.code == "pack_glossary_duplicate_identity"


def test_validate_pack_glossary_rejects_approved_also_forbidden():
    with pytest.raises(ContextPackError) as exc:
        validate_pack_glossary(
            [
                GlossaryEntry(
                    source="x",
                    target="A",
                    forbidden_targets=["A"],
                    enforce="error",
                )
            ]
        )
    assert exc.value.code == "pack_glossary_approved_forbidden"


def test_validate_pack_glossary_rejects_require_without_target():
    with pytest.raises(ContextPackError) as exc:
        validate_pack_glossary(
            [GlossaryEntry(source="x", require_target=True, enforce="error")]
        )
    assert exc.value.code == "pack_glossary_require_without_target"


def test_validate_pack_glossary_rejects_mandatory_rule_disabled():
    with pytest.raises(ContextPackError) as exc:
        validate_pack_glossary(
            [GlossaryEntry(source="x", forbidden_targets=["y"], enforce="off")]
        )
    assert exc.value.code == "pack_glossary_mandatory_disabled"


def test_validate_pack_glossary_rejects_empty_or_duplicate_members():
    with pytest.raises(ContextPackError):
        validate_pack_glossary([GlossaryEntry(source="x", forbidden_targets=["", "y"])])
    with pytest.raises(ContextPackError):
        validate_pack_glossary([GlossaryEntry(source="x", source_variants=["a", "a"])])


# --- 11. Empire/Reich round-trip preserves enforcement ------------------------


def test_empire_reich_roundtrip_preserves_enforcement(tmp_path):
    pack = SeriesContextPack(
        series_id="shadows-of-apt",
        source_language="en",
        target_language="de",
        created_at="t",
        glossary=[_empire_entry()],
    )
    js = pack.model_dump_json(by_alias=True)
    pack2 = parse_context_pack(js)
    entry = pack2.glossary[0]
    assert entry.source == "empire"
    assert entry.target == "Imperium"
    assert entry.forbidden_targets == ["Reich"]
    assert entry.enforce == "error"
    assert entry.category == "concept"
    assert entry.require_target is False


def test_read_and_write_context_pack_roundtrip(tmp_path: Path):
    pack = SeriesContextPack(
        series_id="s",
        source_language="en",
        target_language="de",
        created_at="t",
        glossary=[_empire_entry()],
    )
    path = tmp_path / "pack.json"
    write_context_pack(path, pack)
    assert path.is_file()
    reloaded = read_context_pack(path)
    assert reloaded.glossary[0].enforce == "error"


# --- 12. style merge: pristine vs approved ------------------------------------


def test_style_merge_replaces_pristine_default_without_conflict(tmp_path: Path):
    project_dir = _make_project(tmp_path, ready=False)
    proj = load_project(project_dir)
    # Fresh context: style fields equal defaults -> pristine replaceable.
    pack = SeriesContextPack(
        series_id="s",
        source_language="en",
        target_language="de",
        created_at="t",
        style=StyleProfile(prose_style="fluent literary"),
    )
    planned, result = plan_context_pack_import(proj, pack)
    assert result.conflicts == 0
    assert planned.style.prose_style == "fluent literary"


def test_style_merge_conflicts_on_approved_local_value(tmp_path: Path):
    project_dir = _make_project(tmp_path, ready=False)
    proj = load_project(project_dir)
    from booktx.context import load_context

    # Make context ready with an approved (non-default) prose_style.
    path = context_path(proj)
    data = json.loads(path.read_text("utf-8"))
    data["ready"] = True
    data["style"]["prose_style"] = "literal"
    path.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
    # Pack style copies the live local style and changes only prose_style, so
    # exactly one field conflicts.
    local = load_context(proj)
    assert local is not None
    pack_style = local.style.model_copy(update={"prose_style": "fluent literary"})
    pack = SeriesContextPack(
        series_id="s",
        source_language="en",
        target_language="de",
        created_at="t",
        style=pack_style,
    )
    planned, result = plan_context_pack_import(proj, pack)
    assert result.conflicts == 1
    # fail mode keeps local value.
    assert planned.style.prose_style == "literal"


# --- 13. generic question-id collision does not merge -------------------------


def test_generic_question_id_collision_does_not_merge():
    # Two unrelated questions that happen to share id "Q099" but differ in
    # semantic identity must not merge. Construct two TranslationContexts and
    # import one's question into the other via plan on a constructed merge path.
    from booktx.context_packs import _merge_questions

    target = TranslationContext(source_language="en", target_language="de")
    target.questions.append(
        ContextQuestion(
            id="Q099", topic="names", question="Which names stay?", origin="seed"
        )
    )
    pack_like = SeriesContextPack(
        series_id="s",
        source_language="en",
        target_language="de",
        created_at="t",
        questions=[
            ContextQuestion(
                id="Q099",
                topic="units",
                question="Convert units?",
                answer="yes",
                status="answered",
                answer_source="user",
                origin="user",
            )
        ],
    )
    findings: list = []
    _merge_questions(target, pack_like, conflict="fail", findings=findings)
    # A new question was added (S001), the local Q099 untouched.
    ids = [q.id for q in target.questions]
    assert "Q099" in ids
    assert any(i.startswith("S") for i in ids)
    added = [q for q in target.questions if q.id.startswith("S")]
    assert added[0].topic == "units"


# --- 14. semantic question match preserves local id ---------------------------


def test_semantic_question_match_preserves_local_id():
    from booktx.context_packs import _merge_questions

    target = TranslationContext(source_language="en", target_language="de")
    target.questions.append(
        ContextQuestion(
            id="S007", topic="names", question="Which names stay?", origin="user"
        )
    )
    pack_like = SeriesContextPack(
        series_id="s",
        source_language="en",
        target_language="de",
        created_at="t",
        questions=[
            ContextQuestion(
                id="Q099",
                topic="names",
                question="Which names stay?",
                answer="keep Apt",
                status="answered",
                answer_source="user",
                origin="user",
            )
        ],
    )
    findings: list = []
    _merge_questions(target, pack_like, conflict="fail", findings=findings)
    matched = [q for q in target.questions if q.id == "S007"]
    assert matched and matched[0].answer == "keep Apt"
    assert matched[0].answer_source == "imported"


# --- 15. new imported questions receive SNNN ids ------------------------------


def test_new_imported_questions_get_snnn_ids(tmp_path: Path):
    project_dir = _make_project(tmp_path, ready=False)
    proj = load_project(project_dir)
    pack = SeriesContextPack(
        series_id="s",
        source_language="en",
        target_language="de",
        created_at="t",
        questions=[
            ContextQuestion(
                id="Q099",
                topic="voice",
                question="How should the narrator sound?",
                answer="grim",
                status="answered",
                answer_source="user",
                origin="user",
            )
        ],
    )
    planned, result = plan_context_pack_import(proj, pack)
    ids = [q.id for q in planned.questions]
    assert any(i.startswith("S") for i in ids)
    # Generic Q099 must not appear as a new id.
    assert "Q099" not in ids


# --- 16. core answer/style disagreement rejected ------------------------------


def test_core_answer_style_disagreement_rejected():
    # Pack style.prose_style says "literal" but Q002 answer says "fluent".
    with pytest.raises(ContextPackError) as exc:
        SeriesContextPack.model_validate(
            _pack_dict(
                style={"prose_style": "literal"},
                questions=[
                    {
                        "id": "Q002",
                        "topic": "overall_style",
                        "question": "Overall style?",
                        "answer": "fluent",
                        "status": "answered",
                        "answer_source": "user",
                        "origin": "core",
                    }
                ],
            )
        )
    assert exc.value.code == "pack_core_style_conflict"


def test_core_answer_style_agreement_accepted():
    pack = SeriesContextPack.model_validate(
        _pack_dict(
            style={"prose_style": "fluent"},
            questions=[
                {
                    "id": "Q002",
                    "topic": "overall_style",
                    "question": "Overall style?",
                    "answer": "fluent",
                    "status": "answered",
                    "answer_source": "user",
                    "origin": "core",
                }
            ],
        )
    )
    assert pack.style.prose_style == "fluent"


# --- 17. imported provenance accepted by readiness validation -----------------


def test_imported_answer_provenance_satisfies_readiness(tmp_path: Path):
    project_dir = _make_project(tmp_path, ready=False)
    proj = load_project(project_dir)
    # Build a pack that answers a required core question (Q005 names).
    pack = SeriesContextPack(
        series_id="s",
        source_language="en",
        target_language="de",
        created_at="t",
        questions=[
            ContextQuestion(
                id="Q005",
                topic="names",
                question="Which names/titles/place names must remain unchanged?",
                answer="keep Apt names",
                status="answered",
                answer_source="user",
                origin="core",
            )
        ],
    )
    planned, _ = plan_context_pack_import(proj, pack)
    q = next(x for x in planned.questions if x.id == "Q005")
    assert q.answer_source == "imported"
    # Imported provenance is in the approved set, so it does not count as
    # unapproved. (It may still be open/required elsewhere, but this one is OK.)
    assert q not in unapproved_required_questions(planned)


# --- 18. changed import clears readiness --------------------------------------


def test_changed_import_clears_readiness(tmp_path: Path):
    project_dir = _make_project(tmp_path, ready=True)
    proj = load_project(project_dir)
    # Mutate context.json to be ready with a known prose style.
    path = context_path(proj)
    data = json.loads(path.read_text("utf-8"))
    assert data["ready"] is True
    pack = SeriesContextPack(
        series_id="s",
        source_language="en",
        target_language="de",
        created_at="t",
        global_rules=["Keep names intact"],
    )
    planned, result = plan_context_pack_import(proj, pack)
    assert result.changed is True
    assert planned.ready is False
    assert planned.ready_forced is False
    assert planned.ready_reason == ""


# --- 19. no-op import preserves readiness -------------------------------------


def test_noop_import_preserves_readiness(tmp_path: Path):
    project_dir = _make_project(tmp_path, ready=True)
    proj = load_project(project_dir)
    # Export then plan-import the exact same pack: everything should skip.
    pack = export_context_pack(proj, series_id="s")
    planned, result = plan_context_pack_import(proj, pack)
    assert result.changed is False
    assert result.conflicts == 0
    # Readiness preserved exactly.
    assert planned.ready is True


# --- 20. pure planning does not mutate inputs ---------------------------------


def test_pure_planning_does_not_mutate_inputs(tmp_path: Path):
    project_dir = _make_project(tmp_path, ready=False)
    proj = load_project(project_dir)
    pack = SeriesContextPack(
        series_id="s",
        source_language="en",
        target_language="de",
        created_at="t",
        glossary=[_empire_entry()],
        global_rules=["Keep names intact"],
    )
    pack_snapshot = pack.model_dump_json(by_alias=True)
    ctx_path = context_path(proj)
    ctx_snapshot = ctx_path.read_text("utf-8")
    plan_context_pack_import(proj, pack)
    # Pack object unchanged.
    assert pack.model_dump_json(by_alias=True) == pack_snapshot
    # No files written.
    assert ctx_path.read_text("utf-8") == ctx_snapshot


def test_question_identity_collapses_whitespace_and_is_case_sensitive():
    q1 = ContextQuestion(id="A", topic="Names", question="Which names?")
    q2 = ContextQuestion(id="B", topic="  Names ", question="Which   names?")
    q3 = ContextQuestion(id="C", topic="names", question="Which names?")
    assert question_identity(q1)[0] == question_identity(q2)[0]  # whitespace
    # case-sensitive: "Names" != "names"
    assert question_identity(q1) != question_identity(q3)


def test_duplicate_question_identity_rejected():
    questions = [
        ContextQuestion(id="Q1", topic="names", question="Which?"),
        ContextQuestion(id="Q2", topic="names", question="Which?"),
    ]
    with pytest.raises(ContextPackError):
        validate_pack_questions(questions, None)


def test_context_pack_import_result_counts_findings():
    from booktx.context_packs import ContextPackImportFinding

    findings = [
        ContextPackImportFinding(
            section="glossary", key="a", action="add", message="m"
        ),
        ContextPackImportFinding(
            section="glossary", key="b", action="skip", message="m"
        ),
        ContextPackImportFinding(
            section="glossary", key="c", action="conflict", message="m"
        ),
    ]
    result = ContextPackImportResult.from_findings(findings, changed=True)
    assert result.added == 1
    assert result.skipped == 1
    assert result.conflicts == 1
    assert result.changed is True


def test_core_question_style_fields_map_known_ids():
    assert CORE_QUESTION_STYLE_FIELDS["Q001"] == "target_locale"
    assert CORE_QUESTION_STYLE_FIELDS["Q002"] == "prose_style"
    assert CORE_QUESTION_STYLE_FIELDS["Q003"] == "register_level"


def test_collapse_whitespace_helper():
    assert collapse_whitespace("  a   b  ") == "a b"
    assert collapse_whitespace("\tx\n\ty") == "x y"
