"""Phase 1B-3 source-analysis enrichment and context workflow tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from booktx.cli import app
from booktx.config import load_source_project
from booktx.context import load_context
from booktx.errors import BooktxError
from booktx.source_analysis import build_source_analysis

runner = CliRunner()


def _project(tmp_path: Path) -> Path:
    source = tmp_path / "novel.md"
    source.write_text(
        "# One\n\nTisamon met Tisamon. wasp-kinden wasp-kinden.\n",
        encoding="utf-8",
    )
    root = tmp_path / "novel"
    commands = (
        ["init", str(root), "--target", "de", "--source-file", str(source)],
        ["extract", str(root)],
        ["chapters", str(root)],
        ["context", "init", str(root), "--profile", "de_default"],
        ["source", "analyze", str(root), "--engine", "simple", "--write"],
    )
    for command in commands:
        result = runner.invoke(app, command)
        assert result.exit_code == 0, (command, result.output)
    return root


def _candidate(root: Path, kind: str) -> str:
    report = json.loads((root / ".booktx" / "source-analysis.json").read_text())
    return next(item["id"] for item in report["candidates"] if item["kind"] == kind)


def test_spacy_blank_pipeline_reports_reduced_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spacy = pytest.importorskip("spacy")
    monkeypatch.setattr(
        spacy,
        "load",
        lambda _name: (_ for _ in ()).throw(OSError("model unavailable")),
    )
    root = _project(tmp_path)
    report = build_source_analysis(
        load_source_project(root), engine_requested="spacy", min_count=1
    )
    assert report.settings.engine_resolved == "spacy"
    assert report.settings.spacy_model == "blank:en"
    assert report.capabilities.tokenizer
    assert report.capabilities.sentence_boundaries
    assert not report.capabilities.pos
    assert not report.capabilities.noun_chunks
    assert not report.capabilities.ner


def test_explicit_spacy_model_language_mismatch_is_controlled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spacy = pytest.importorskip("spacy")
    root = _project(tmp_path)
    fake = spacy.blank("de")
    monkeypatch.setattr(spacy, "load", lambda _name: fake)
    with pytest.raises(BooktxError, match="does not match"):
        build_source_analysis(
            load_source_project(root),
            engine_requested="spacy",
            spacy_model="wrong_language",
        )


def test_ignore_survives_reanalysis_and_prefill_is_dry_run_idempotent(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    candidate_id = _candidate(root, "hyphenated_term")
    ignored = runner.invoke(
        app,
        [
            "source",
            "ignore-candidate",
            str(root),
            candidate_id,
            "--reason",
            "ordinary prose",
            "--write",
        ],
    )
    assert ignored.exit_code == 0, ignored.output
    sidecar = root / ".booktx" / "source-analysis-decisions.json"
    before = json.loads(sidecar.read_text())

    rerun = runner.invoke(
        app, ["source", "analyze", str(root), "--engine", "simple", "--write"]
    )
    assert rerun.exit_code == 0, rerun.output
    assert json.loads(sidecar.read_text()) == before

    dry = runner.invoke(
        app,
        [
            "context",
            "prefill",
            str(root),
            "--profile",
            "de_default",
            "--from-source-analysis",
        ],
    )
    assert dry.exit_code == 0, dry.output
    context_path = root / "translations" / "de_default" / "context.json"
    dry_payload = json.loads(context_path.read_text())

    written = runner.invoke(
        app,
        [
            "context",
            "prefill",
            str(root),
            "--profile",
            "de_default",
            "--from-source-analysis",
            "--write",
        ],
    )
    assert written.exit_code == 0, written.output
    payload = json.loads(context_path.read_text())
    assert payload != dry_payload
    assert payload["ready"] is False
    assert all(
        entry.get("origin") != "source_analysis" for entry in payload["glossary"]
    )
    assert any(
        question.get("origin") == "source_analysis" for question in payload["questions"]
    )
    repeated = runner.invoke(
        app,
        [
            "context",
            "prefill",
            str(root),
            "--profile",
            "de_default",
            "--from-source-analysis",
            "--write",
        ],
    )
    assert repeated.exit_code == 0, repeated.output
    assert json.loads(context_path.read_text()) == payload


def test_context_prefill_advisory_requires_opt_in(tmp_path: Path) -> None:
    source = tmp_path / "advisory.md"
    source.write_text(
        "# One\n\nThe silk harbor opened. The silk harbor closed.\n",
        encoding="utf-8",
    )
    root = tmp_path / "advisory"
    for command in (
        ["init", str(root), "--target", "de", "--source-file", str(source)],
        ["extract", str(root)],
        ["chapters", str(root)],
        ["context", "init", str(root), "--profile", "de_default"],
        ["source", "analyze", str(root), "--engine", "simple", "--write"],
    ):
        result = runner.invoke(app, command)
        assert result.exit_code == 0, (command, result.output)
    context_path = root / "translations" / "de_default" / "context.json"

    default_prefill = runner.invoke(
        app,
        [
            "context",
            "prefill",
            str(root),
            "--profile",
            "de_default",
            "--from-source-analysis",
            "--write",
        ],
    )
    assert default_prefill.exit_code == 0, default_prefill.output
    default_payload = json.loads(context_path.read_text())
    assert all(
        entry.get("origin") != "source_analysis"
        for entry in default_payload["glossary"]
    )

    advisory_prefill = runner.invoke(
        app,
        [
            "context",
            "prefill",
            str(root),
            "--profile",
            "de_default",
            "--from-source-analysis",
            "--include-advisory",
            "--write",
        ],
    )
    assert advisory_prefill.exit_code == 0, advisory_prefill.output
    advisory_payload = json.loads(context_path.read_text())
    assert any(
        entry.get("origin") == "source_analysis"
        for entry in advisory_payload["glossary"]
    )


def test_promote_candidate_records_profile_reference_and_never_changes_names(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    candidate_id = _candidate(root, "hyphenated_term")
    names_path = root / ".booktx" / "names.json"
    names_before = names_path.read_bytes() if names_path.exists() else None
    promoted = runner.invoke(
        app,
        [
            "context",
            "promote-candidate",
            str(root),
            candidate_id,
            "--profile",
            "de_default",
            "--target",
            "Wespenart",
            "--require-target",
            "--enforce",
            "error",
            "--promoted-by",
            "tester",
            "--write",
        ],
    )
    assert promoted.exit_code == 0, promoted.output
    profile_project = load_source_project(root)
    from booktx.config import load_profile_project

    context = load_context(load_profile_project(profile_project.root, "de_default"))
    assert context is not None
    entry = next(
        item
        for item in context.glossary
        if item.source_analysis_candidate_id == candidate_id
    )
    assert entry.target == "Wespenart"
    assert entry.require_target
    assert entry.enforce == "error"
    decisions = json.loads(
        (root / ".booktx" / "source-analysis-decisions.json").read_text()
    )
    assert decisions["promotions"][0]["profile"] == "de_default"
    assert (names_path.read_bytes() if names_path.exists() else None) == names_before
