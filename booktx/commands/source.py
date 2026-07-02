"""Typer commands for source-record inspection (Phase 3 slice 2).

Thin command layer for ``source status / record / chapter``. Each command loads
the runtime/project via the shared CLI helper, delegates data work to
:mod:`booktx.workflows.source`, renders the result, and maps
:class:`booktx.errors.BooktxError` to a non-zero exit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from booktx.source_analysis import SourceAnalysisReport

import typer

from booktx.cli_support import (
    _die,
    _handle_booktx_error,
    _load_runtime_or_exit,
    _project_status_snapshot,
    console,
)
from booktx.errors import BooktxError
from booktx.source_analysis import read_canonical_report
from booktx.source_analysis_context import set_disposition
from booktx.workflows.source import (
    analyze_source,
    build_source_status_payload,
    collect_chapter_records,
    find_source_record,
    read_source_analysis,
)

source_app = typer.Typer(help="Inspect brokered source records without path leaks.")


def _validate_source_format(output_format: str) -> None:
    if output_format not in {"block", "text", "json"}:
        _die("--format must be block, text, or json")


@source_app.command(name="status")
def source_status_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory or profile root."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Show a safe summary of extracted source state."""
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=False)
    proj = runtime.project
    payload = build_source_status_payload(proj)
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    console.print(f"source: {payload['source']}")
    console.print(f"format: {payload['format']}")
    console.print(f"source language: {payload['source_language']}")
    console.print(f"records: {payload['records']}")
    console.print(f"chunks: {payload['chunks']}")
    console.print(f"chapters: {payload['chapters']}")


@source_app.command(name="record")
def source_record_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory or profile root."),
    record_ref: str = typer.Argument(..., help="Record id or record ref."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    output_format: str = typer.Option(
        "block",
        "--format",
        help="Output format: block, text, or json.",
    ),
) -> None:
    """Print one source record without exposing chunk paths."""
    _validate_source_format(output_format)
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=False)
    proj = runtime.project
    try:
        record = find_source_record(proj, record_ref)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    payload = {"id": record.record_id, "source": record.source}
    if output_format == "json":
        console.print_json(json.dumps(payload, ensure_ascii=False))
    elif output_format == "text":
        console.print(f"{record.record_id}\t{record.source}")
    else:
        console.print(f">>> {record.record_id}")
        console.print(record.source)


@source_app.command(name="chapter")
def source_chapter_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory or profile root."),
    chapter_id: str = typer.Argument(..., help="Chapter id, e.g. 0001."),
    profile: str | None = typer.Option(
        None, "--profile", help="Translation profile name."
    ),
    output_format: str = typer.Option(
        "block",
        "--format",
        help="Output format: block, text, or json.",
    ),
) -> None:
    """Print all source records for one chapter without exposing chunk paths."""
    _validate_source_format(output_format)
    runtime = _load_runtime_or_exit(project_dir, profile=profile, require_profile=False)
    proj = runtime.project
    bundle = _project_status_snapshot(proj)
    try:
        result = collect_chapter_records(bundle, chapter_id)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    records = result.records
    if output_format == "json":
        console.print_json(
            json.dumps(
                {
                    "chapter_id": result.chapter_id,
                    "title": result.title,
                    "records": records,
                },
                ensure_ascii=False,
            )
        )
        return
    for item in records:
        if output_format == "text":
            console.print(f"{item['id']}\t{item['source']}")
        else:
            console.print(f">>> {item['id']}")
            console.print(item["source"])
            if item != records[-1]:
                console.print()


def _validate_analysis_format(output_format: str) -> None:
    if output_format not in {"human", "json"}:
        _die("--format must be human or json")


def _print_analysis_human(
    report: SourceAnalysisReport,
    *,
    stale: bool = False,
    hint: str = "",
) -> None:
    caps = report.capabilities
    cap_names = [
        name
        for name, on in (
            ("tokenizer", caps.tokenizer),
            ("sentence_boundaries", caps.sentence_boundaries),
            ("lemmatizer", caps.lemmatizer),
            ("pos", caps.pos),
            ("parser", caps.parser),
            ("noun_chunks", caps.noun_chunks),
            ("ner", caps.ner),
        )
        if on
    ]
    console.print(f"source language: {report.source_language}")
    console.print(f"engine: {report.settings.engine_resolved}")
    console.print(f"capabilities: {', '.join(cap_names) or '(none)'}")
    console.print(f"records: {report.record_count}")
    console.print(f"chapters: {report.chapter_count}")
    console.print(f"candidates: {len(report.candidates)}")
    console.print(f"analysis sha256: {report.analysis_sha256}")
    if stale:
        console.print(f"[yellow]stale:[/yellow] {hint}")
    if report.warnings:
        console.print("warnings:")
        for warning in report.warnings:
            console.print(f"  - {warning}")
    if report.candidates:
        console.print("top candidates:")
        for cand in report.candidates[:10]:
            console.print(
                f"  {cand.id} {cand.text!r} bucket={cand.review_bucket} "
                f"kind={cand.kind} count={cand.count} "
                f"chapters={cand.chapter_frequency} "
                f"action={cand.suggested_context_action} risk={cand.risk_score:.2f}"
            )


@source_app.command(name="analyze")
def source_analyze_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory (project root)."),
    engine: str = typer.Option(
        "auto", "--engine", help="Analysis engine: auto, spacy, or simple."
    ),
    spacy_model: str | None = typer.Option(
        None, "--spacy-model", help="Explicit spaCy model."
    ),
    top: int = typer.Option(200, "--top", help="Global candidate limit after merging."),
    min_count: int = typer.Option(2, "--min-count", help="Minimum corpus count."),
    ngram_max: int = typer.Option(
        4, "--ngram-max", help="Maximum phrase length (1..4)."
    ),
    include_common: bool = typer.Option(
        False, "--include-common", help="Include common words as candidates."
    ),
    output_format: str = typer.Option(
        "human", "--format", help="Output format: human or json."
    ),
    write: bool = typer.Option(
        False, "--write", help="Write canonical JSON and Markdown."
    ),
    sync_profiles: bool = typer.Option(
        False,
        "--sync-profiles",
        help="Refresh all profile snapshots (requires --write).",
    ),
) -> None:
    """Analyze extracted source evidence (project root only; dry run by default)."""
    if engine not in {"auto", "spacy", "simple"}:
        _die("--engine must be auto, spacy, or simple")
    _validate_analysis_format(output_format)
    runtime = _load_runtime_or_exit(project_dir, require_profile=False)
    # Analyze/write/sync are collaborative project-root workflows.
    if runtime.mode.isolated_output:
        _die("source analyze is a project-root command; run it from the project root.")
    proj = runtime.project
    try:
        result = analyze_source(
            proj,
            engine_requested=engine,
            spacy_model=spacy_model,
            min_count=min_count,
            ngram_max=ngram_max,
            top=top,
            include_common=include_common,
            write=write,
            sync_profiles=sync_profiles,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    report = result.report
    if output_format == "json":
        payload = report.model_dump(by_alias=True, mode="json")
        if write:
            payload["_written"] = {
                "canonical_json": result.canonical_json_written,
                "canonical_md": result.canonical_md_written,
                "synced_profiles": result.refreshed_profiles,
            }
        console.print_json(json.dumps(payload, ensure_ascii=False))
    else:
        _print_analysis_human(report)
        if write:
            console.print(f"canonical json written: {result.canonical_json_written}")
            console.print(f"canonical markdown written: {result.canonical_md_written}")
            if result.canonical_md_error:
                console.print(f"[red]markdown error:[/red] {result.canonical_md_error}")
            if sync_profiles:
                console.print(f"snapshots refreshed: {len(result.refreshed_profiles)}")
                for sync in result.synced:
                    mark = "ok" if sync.json_written and not sync.error else "FAIL"
                    console.print(f"  [{mark}] {sync.profile}")
    if write and sync_profiles and result.failed_syncs:
        failed = ", ".join(s.profile for s in result.failed_syncs)
        _die(
            f"source-analysis snapshot sync failed for profile(s): {failed}; "
            f"{len(result.refreshed_profiles)} snapshot(s) refreshed."
        )


@source_app.command(name="analysis")
def source_analysis_cmd(
    project_dir: Path = typer.Argument(..., help="Project directory or profile root."),
    output_format: str = typer.Option(
        "human", "--format", help="Output format: human or json."
    ),
) -> None:
    """Read source-analysis evidence (canonical or current profile snapshot)."""
    _validate_analysis_format(output_format)
    runtime = _load_runtime_or_exit(project_dir, require_profile=False)
    proj = runtime.project
    try:
        read = read_source_analysis(proj, isolated=runtime.mode.isolated_output)
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    if read.missing:
        _die(read.hint or "no source-analysis evidence found")
        return
    if read.report is not None:
        report = read.report
    else:
        assert read.snapshot is not None
        report = read.snapshot.report
    if output_format == "json":
        console.print_json(
            json.dumps(
                report.model_dump(by_alias=True, mode="json"), ensure_ascii=False
            )
        )
    else:
        _print_analysis_human(report, stale=read.stale, hint=read.hint)


def _candidate_disposition_command(
    project_dir: Path,
    candidate_id: str,
    *,
    disposition: Literal["ignored", "reviewed"],
    reason: str,
    decided_by: str,
    write: bool,
) -> None:
    runtime = _load_runtime_or_exit(project_dir, require_profile=False)
    if runtime.mode.isolated_output:
        _die(f"source {disposition} is a project-root command")
    report = read_canonical_report(runtime.project)
    if report is None:
        _die("no canonical source analysis; run `booktx source analyze . --write`")
    assert report is not None
    try:
        decision, changed = set_disposition(
            runtime.project,
            report,
            candidate_id=candidate_id,
            disposition=disposition,
            reason=reason,
            decided_by=decided_by,
            write=write,
        )
    except BooktxError as exc:
        _handle_booktx_error(exc)
        return
    console.print(
        f"{'would write' if not write else 'wrote'} {decision.disposition} "
        f"decision for {decision.candidate_id}; changed={str(changed).lower()}"
    )


@source_app.command(name="ignore-candidate")
def source_ignore_candidate_cmd(
    project_dir: Path = typer.Argument(..., help="Project root."),
    candidate_id: str = typer.Argument(..., help="Stable candidate id."),
    reason: str = typer.Option("", "--reason", help="Review rationale."),
    decided_by: str = typer.Option("cli", "--decided-by", help="Decision provenance."),
    write: bool = typer.Option(False, "--write", help="Persist the decision."),
) -> None:
    """Ignore one source candidate (dry run by default)."""
    _candidate_disposition_command(
        project_dir,
        candidate_id,
        disposition="ignored",
        reason=reason,
        decided_by=decided_by,
        write=write,
    )


@source_app.command(name="review-candidate")
def source_review_candidate_cmd(
    project_dir: Path = typer.Argument(..., help="Project root."),
    candidate_id: str = typer.Argument(..., help="Stable candidate id."),
    reason: str = typer.Option("", "--reason", help="Review rationale."),
    decided_by: str = typer.Option("cli", "--decided-by", help="Decision provenance."),
    write: bool = typer.Option(False, "--write", help="Persist the decision."),
) -> None:
    """Mark one source candidate reviewed (dry run by default)."""
    _candidate_disposition_command(
        project_dir,
        candidate_id,
        disposition="reviewed",
        reason=reason,
        decided_by=decided_by,
        write=write,
    )
