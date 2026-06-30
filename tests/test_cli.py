"""Smoke tests for the booktx Typer CLI."""

from __future__ import annotations

import json
from pathlib import Path

import tomli_w
from ebooklib import epub
from typer.testing import CliRunner

from booktx.cli import app
from booktx.config import load_manifest, load_project, translation_store_path

runner = CliRunner()


MARKDOWN_DOC = """\
---
title: Demo
---

# Hello

Alice met Bob. They were happy.

```python
print("x")
```
"""


def _make_markdown_project(tmp_path: Path) -> Path:
    project_dir = tmp_path / "book"
    src = tmp_path / "novel.md"
    src.write_text(MARKDOWN_DOC, encoding="utf-8")
    res = runner.invoke(
        app,
        ["init", str(project_dir), "--target", "de", "--source-file", str(src)],
    )
    assert res.exit_code == 0, res.output
    return project_dir


def _make_epub_project(tmp_path: Path) -> Path:
    project_dir = tmp_path / "epub-book"
    src = tmp_path / "novel.epub"
    book = epub.EpubBook()
    book.set_identifier("cli-epub-id")
    book.set_title("CLI EPUB")
    book.set_language("en")
    chapter = epub.EpubHtml(title="Chapter One", file_name="ch1.xhtml", lang="en")
    chapter.content = (
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        "<head><title>Chapter One</title></head><body>"
        "<h1>Chapter One</h1>"
        "<p>Alice met Bob.</p>"
        "</body></html>"
    )
    book.add_item(chapter)
    book.add_item(epub.EpubNav())
    book.add_item(epub.EpubNcx())
    book.spine = ["nav", chapter]
    epub.write_epub(str(src), book, {})
    res = runner.invoke(
        app,
        ["init", str(project_dir), "--target", "de", "--source-file", str(src)],
    )
    assert res.exit_code == 0, res.output
    return project_dir


def _rewrite_project_chunk_size(project_dir: Path, chunk_size: int) -> None:
    from booktx.config import tomllib

    config_path = project_dir / ".booktx" / "source-config.toml"
    with config_path.open("rb") as fh:
        data = tomllib.load(fh)
    data["chunk_size"] = chunk_size
    config_path.write_bytes(tomli_w.dumps(data).encode("utf-8"))


def _write_accepted_store_record(project_dir: Path) -> None:
    chunk = json.loads(
        next((project_dir / ".booktx" / "chunks").glob("*.json")).read_text("utf-8")
    )
    record = chunk["records"][0]
    translation_store_path(load_project(project_dir)).write_text(
        json.dumps(
            {
                "version": 2,
                "records": {
                    record["id"]: {
                        "chunk_id": int(record["id"].split("-", 1)[0]),
                        "part_id": 1,
                        "source_sha256": "abc123",
                        "source": record["source"],
                        "active_version": "1.1",
                        "versions": [
                            {
                                "version": 1,
                                "subversion": 1,
                                "version_ref": "1.1",
                                "target": record["source"],
                                "status": "accepted",
                                "created_at": "2026-06-22T12:00:00Z",
                                "updated_at": "2026-06-22T12:00:00Z",
                            }
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _selected_project(project_dir: Path):
    return load_project(project_dir)


def test_version_flag():
    res = runner.invoke(app, ["--version"])
    assert res.exit_code == 0
    # Version is sourced from _version.py; just assert it is non-empty and
    # does not report the stale hardcoded 0.1.0 when a generated version exists.
    import booktx

    assert booktx.__version__
    assert booktx.__version__ != "0.1.0"
    assert booktx.__version__ in res.output


def test_version_group_without_subcommand_errors():
    res = runner.invoke(app, ["version"])
    assert res.exit_code == 2
    assert "booktx --version" in res.output
    assert "version current PROJECT_DIR" in res.output


def test_whoami_reports_missing_context_without_failing(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)

    res = runner.invoke(app, ["whoami", str(project_dir)])

    assert res.exit_code == 0, res.output
    assert f"booktx identity: {project_dir}" in res.output
    assert "active_version:" in res.output
    assert "none" in res.output
    assert "context:" in res.output
    assert "MISSING translations/de_default/context.json" in res.output
    assert "store_version:" in res.output
    assert "store_records:" in res.output


def test_whoami_json_is_stable_when_optional_state_is_missing(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)

    res = runner.invoke(app, ["whoami", str(project_dir), "--json"])

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["project_dir"] == str(project_dir)
    assert payload["active_version"] is None
    assert payload["context"]["path"] == "translations/de_default/context.json"
    assert payload["context"]["exists"] is False
    assert payload["context"]["ready"] is None
    assert payload["context"]["sha256"] is None
    assert payload["store"]["exists"] is False
    assert payload["store"]["version"] is None
    assert payload["store"]["record_count"] is None


def test_harness_set_accepts_all_supported_argument_orders(tmp_path: Path, monkeypatch):
    project_dir = _make_markdown_project(tmp_path)

    monkeypatch.chdir(project_dir)
    res = runner.invoke(app, ["harness", "set", "pi"])
    assert res.exit_code == 0, res.output
    assert runner.invoke(app, ["harness", "whoami", "."]).output.strip() == "pi"

    res = runner.invoke(app, ["harness", "set", "qa", str(project_dir)])
    assert res.exit_code == 0, res.output
    assert (
        runner.invoke(app, ["harness", "whoami", str(project_dir)]).output.strip()
        == "qa"
    )

    res = runner.invoke(app, ["harness", "set", str(project_dir), "ops"])
    assert res.exit_code == 0, res.output
    assert (
        runner.invoke(app, ["harness", "whoami", str(project_dir)]).output.strip()
        == "ops"
    )

    res = runner.invoke(app, ["harness", "set", "--project", str(project_dir), "pi"])
    assert res.exit_code == 0, res.output
    assert (
        runner.invoke(app, ["harness", "whoami", str(project_dir)]).output.strip()
        == "pi"
    )


def test_actor_and_model_set_support_project_option(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)

    actor_res = runner.invoke(
        app, ["actor", "set", "--project", str(project_dir), "user:test"]
    )
    model_res = runner.invoke(
        app, ["model", "set", "--project", str(project_dir), "codex-openai/gpt-5.5@low"]
    )

    assert actor_res.exit_code == 0, actor_res.output
    assert model_res.exit_code == 0, model_res.output
    assert (
        runner.invoke(app, ["actor", "whoami", str(project_dir)]).output.strip()
        == "user:test"
    )
    assert (
        runner.invoke(app, ["model", "whoami", str(project_dir)]).output.strip()
        == "codex-openai/gpt-5.5@low"
    )


def test_init_accepts_source_lang_alias(tmp_path: Path):
    project_dir = tmp_path / "alias-book"
    src = tmp_path / "novel.md"
    src.write_text(MARKDOWN_DOC, encoding="utf-8")
    res = runner.invoke(
        app,
        [
            "init",
            str(project_dir),
            "--target",
            "fr",
            "--source-file",
            str(src),
            "--source-lang",
            "en",
        ],
    )
    assert res.exit_code == 0, res.output
    from booktx.config import tomllib

    with (project_dir / ".booktx" / "source-config.toml").open("rb") as fh:
        cfg = tomllib.load(fh)
    assert cfg["source_language"] == "en"


def test_init_without_target_creates_source_only_layout(tmp_path: Path):
    project_dir = tmp_path / "source-only-book"
    src = tmp_path / "novel.md"
    src.write_text(MARKDOWN_DOC, encoding="utf-8")

    res = runner.invoke(
        app,
        [
            "init",
            str(project_dir),
            "--source-file",
            str(src),
            "--source-lang",
            "en",
        ],
    )

    assert res.exit_code == 0, res.output
    assert (project_dir / ".booktx" / "source-config.toml").is_file()
    assert (project_dir / ".booktx" / "chunks").is_dir()
    assert (project_dir / "translations").is_dir()
    assert not any((project_dir / "translations").iterdir())
    assert not (project_dir / ".booktx" / "translated").exists()


def test_init_creates_layout(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    from booktx.config import tomllib

    with (project_dir / ".booktx" / "source-config.toml").open("rb") as fh:
        cfg = tomllib.load(fh)
    assert cfg["source_language"] == "en"
    assert cfg["format"] == "markdown"
    for sub in (
        "source",
        ".booktx",
        ".booktx/chunks",
        "translations/de_default/translated",
        "translations/de_default/output",
    ):
        assert (project_dir / sub).is_dir()


def test_inspect_prints_summary(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    res = runner.invoke(app, ["inspect", str(project_dir)])
    assert res.exit_code == 0, res.output
    assert "markdown" in res.output
    assert "estimated_records" in res.output


def test_inspect_epub_prints_spine_document_details(tmp_path: Path):
    project_dir = _make_epub_project(tmp_path)
    res = runner.invoke(app, ["inspect", str(project_dir)])
    assert res.exit_code == 0, res.output
    assert "epub" in res.output
    assert "spine document(s) with text blocks" in res.output


def test_extract_writes_chunks(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    res = runner.invoke(app, ["extract", str(project_dir)])
    assert res.exit_code == 0, res.output
    chunks = list((project_dir / ".booktx" / "chunks").glob("*.json"))
    assert chunks, "no chunks written"
    first = json.loads(chunks[0].read_text("utf-8"))
    assert set(first.keys()) == {
        "schema_version",
        "chunk_id",
        "chunk_size",
        "record_id_scheme",
        "source_language",
        "target_language",
        "records",
    }
    assert first["schema_version"] == 2
    assert first["chunk_size"] == 50
    assert first["record_id_scheme"] == "chunk-local:v1"
    assert first["records"][0]["id"].count("-") == 1
    manifest = load_manifest(_selected_project(project_dir)).model_dump(mode="json")
    assert manifest["chunk_size"] == 50
    assert manifest["record_id_scheme"] == "chunk-local:v1"
    assert manifest["segmenter"]["name"] == "phrasplit"
    assert manifest["names_sha256"]


def test_extract_epub_writes_manifest_metadata(tmp_path: Path):
    project_dir = _make_epub_project(tmp_path)

    res = runner.invoke(app, ["extract", str(project_dir)])

    assert res.exit_code == 0, res.output
    manifest = load_manifest(_selected_project(project_dir)).model_dump(mode="json")
    assert manifest["chunk_size"] == 50
    assert manifest["record_id_scheme"] == "chunk-local:v1"
    assert manifest["segmenter"]["name"] == "phrasplit"
    assert manifest["names_sha256"]


def test_extract_source_only_writes_shared_manifest(tmp_path: Path):
    project_dir = tmp_path / "source-only-book"
    src = tmp_path / "novel.md"
    src.write_text(MARKDOWN_DOC, encoding="utf-8")
    assert (
        runner.invoke(
            app,
            [
                "init",
                str(project_dir),
                "--source-file",
                str(src),
                "--source-lang",
                "en",
            ],
        ).exit_code
        == 0
    )

    res = runner.invoke(app, ["extract", str(project_dir)])

    assert res.exit_code == 0, res.output
    manifest = load_manifest(load_project(project_dir))
    assert manifest is not None
    assert manifest.source.target_language == ""


def test_extract_is_idempotent_and_preserves_translated(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    runner.invoke(app, ["extract", str(project_dir)])
    # Pretend a translation exists
    translated_dir = _selected_project(project_dir).translated_dir
    assert translated_dir is not None
    translated_dir.mkdir(parents=True, exist_ok=True)
    (translated_dir / "0001.json").write_text(
        '{"chunk_id": "0001", "records": []}', encoding="utf-8"
    )
    before = (project_dir / ".booktx" / "chunks" / "0001.json").read_text("utf-8")
    # Re-extract
    res = runner.invoke(app, ["extract", str(project_dir)])
    assert res.exit_code == 0, res.output
    after = (project_dir / ".booktx" / "chunks" / "0001.json").read_text("utf-8")
    assert before == after  # deterministic
    # translated file survives
    assert (translated_dir / "0001.json").is_file()


def test_extract_refuses_chunk_size_change_with_existing_store_for_legacy_ids(
    tmp_path: Path,
):
    project_dir = _make_markdown_project(tmp_path)
    assert runner.invoke(app, ["extract", str(project_dir)]).exit_code == 0
    _write_accepted_store_record(project_dir)
    _rewrite_project_chunk_size(project_dir, 25)

    res = runner.invoke(app, ["extract", str(project_dir)])

    assert res.exit_code != 0
    assert "chunk_size changed" in res.output
    assert "renumber record ids" in res.output


def test_extract_force_rechunk_allows_chunk_size_change(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    assert runner.invoke(app, ["extract", str(project_dir)]).exit_code == 0
    _write_accepted_store_record(project_dir)
    _rewrite_project_chunk_size(project_dir, 25)

    res = runner.invoke(app, ["extract", str(project_dir), "--force-rechunk"])

    assert res.exit_code == 0, res.output


def test_extract_leaves_chunks_intact_when_write_fails(tmp_path: Path, monkeypatch):
    """An interrupted extract must not corrupt the existing chunks dir.

    The atomic chunk-dir swap writes into a sibling temp dir and only
    replaces .booktx/chunks/ after every chunk is written. If a write fails
    mid-extract, the previous chunks directory must survive unchanged.
    """
    project_dir = _make_markdown_project(tmp_path)
    runner.invoke(app, ["extract", str(project_dir)])
    chunks_dir = project_dir / ".booktx" / "chunks"
    before = sorted(p.name for p in chunks_dir.glob("*.json"))
    assert before
    first_before = (chunks_dir / before[0]).read_text("utf-8")

    # Now break the atomic writer so the next extract fails mid-write.
    from booktx import io_utils

    real_write = io_utils.write_text_atomic
    call = {"n": 0}

    def failing_write(path, text):
        # Only fail once, and only for writes inside the temp chunks swap dir,
        # so we target the extract phase regardless of how many chunks exist.
        if path.parent.name.startswith(".chunks.") and not call["n"]:
            call["n"] += 1
            raise OSError("simulated interruption")
        real_write(path, text)

    monkeypatch.setattr(io_utils, "write_text_atomic", failing_write)
    res = runner.invoke(app, ["extract", str(project_dir)])
    assert res.exit_code != 0

    # The public chunks dir must be untouched: same files, same content.
    after = sorted(p.name for p in chunks_dir.glob("*.json"))
    assert after == before
    assert (chunks_dir / before[0]).read_text("utf-8") == first_before
    # No leftover temp chunks dir should remain in .booktx/.
    booktx_dir = project_dir / ".booktx"
    leftovers = [p.name for p in booktx_dir.iterdir() if p.name.startswith(".chunks.")]
    assert leftovers == []


def test_next_prints_first_untranslated_then_exits_nonzero_when_done(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    runner.invoke(app, ["extract", str(project_dir)])
    # First untranslated
    res = runner.invoke(app, ["next", str(project_dir), "--allow-missing-context"])
    assert res.exit_code == 0, res.output
    assert "0001" in res.output
    # Provide a translation for every chunk
    translated_dir = _selected_project(project_dir).translated_dir
    assert translated_dir is not None
    for chunk_file in (project_dir / ".booktx" / "chunks").glob("*.json"):
        chunk = json.loads(chunk_file.read_text("utf-8"))
        payload = {
            "chunk_id": chunk["chunk_id"],
            "records": [
                {"id": r["id"], "target": r["source"]} for r in chunk["records"]
            ],
        }
        (translated_dir / chunk_file.name).write_text(
            json.dumps(payload), encoding="utf-8"
        )
    res2 = runner.invoke(app, ["next", str(project_dir), "--allow-missing-context"])
    assert res2.exit_code == 1
    assert "All" in res2.output


def test_next_requires_ready_context(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    runner.invoke(app, ["extract", str(project_dir)])
    res = runner.invoke(app, ["next", str(project_dir)])
    assert res.exit_code == 1
    assert "context" in res.output.lower()
    assert "booktx context init" in res.output


def test_next_allow_missing_context_legacy_override(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    runner.invoke(app, ["extract", str(project_dir)])
    res = runner.invoke(app, ["next", str(project_dir), "--allow-missing-context"])
    assert res.exit_code == 0
    assert "0001" in res.output


def test_next_without_chunks_tells_user_to_extract(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    res = runner.invoke(app, ["next", str(project_dir), "--allow-missing-context"])
    assert res.exit_code == 1
    assert "booktx extract" in res.output


def test_next_unit_chapter_without_chunks_tells_user_to_extract(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    res = runner.invoke(
        app,
        ["next", str(project_dir), "--unit", "chapter", "--allow-missing-context"],
    )
    assert res.exit_code == 1
    assert "booktx extract" in res.output


def test_next_prints_context_path_when_context_ready(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    runner.invoke(app, ["extract", str(project_dir)])
    runner.invoke(app, ["context", "init", str(project_dir), "--non-interactive"])
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
    res = runner.invoke(app, ["next", str(project_dir)])
    assert res.exit_code == 0, res.output
    assert "context:" in res.output
    assert "context.md" in res.output
    assert "0001" in res.output


def test_validate_passes_with_identity_translation(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    runner.invoke(app, ["extract", str(project_dir)])
    translated_dir = _selected_project(project_dir).translated_dir
    assert translated_dir is not None
    for chunk_file in (project_dir / ".booktx" / "chunks").glob("*.json"):
        chunk = json.loads(chunk_file.read_text("utf-8"))
        payload = {
            "chunk_id": chunk["chunk_id"],
            "records": [
                {"id": r["id"], "target": r["source"]} for r in chunk["records"]
            ],
        }
        (translated_dir / chunk_file.name).write_text(
            json.dumps(payload), encoding="utf-8"
        )
    res = runner.invoke(app, ["validate", str(project_dir)])
    assert res.exit_code == 0, res.output
    assert "errors=0" in res.output


def test_validate_fails_on_empty_target(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    runner.invoke(app, ["extract", str(project_dir)])
    translated_dir = _selected_project(project_dir).translated_dir
    assert translated_dir is not None
    chunk_file = next((project_dir / ".booktx" / "chunks").glob("*.json"))
    chunk = json.loads(chunk_file.read_text("utf-8"))
    payload = {
        "chunk_id": chunk["chunk_id"],
        "records": [{"id": r["id"], "target": "   "} for r in chunk["records"]],
    }
    (translated_dir / chunk_file.name).write_text(json.dumps(payload), encoding="utf-8")
    res = runner.invoke(app, ["validate", str(project_dir)])
    assert res.exit_code == 1
    assert "empty_target" in res.output


def test_build_produces_output(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    runner.invoke(app, ["extract", str(project_dir)])
    translated_dir = _selected_project(project_dir).translated_dir
    assert translated_dir is not None
    for chunk_file in (project_dir / ".booktx" / "chunks").glob("*.json"):
        chunk = json.loads(chunk_file.read_text("utf-8"))
        payload = {
            "chunk_id": chunk["chunk_id"],
            "records": [
                {"id": r["id"], "target": r["source"]} for r in chunk["records"]
            ],
        }
        (translated_dir / chunk_file.name).write_text(
            json.dumps(payload), encoding="utf-8"
        )
    res = runner.invoke(app, ["build", str(project_dir)])
    assert res.exit_code == 0, res.output
    out_file = project_dir / "translations" / "de_default" / "output" / "novel.de.md"
    assert out_file.is_file()
    out = out_file.read_text("utf-8")
    assert "Alice" in out and "Bob" in out
    for token in ("__NAME_", "__TAG_", "__SPANTX_"):
        assert token not in out


def test_full_pipeline_end_to_end(tmp_path: Path):
    project_dir = _make_markdown_project(tmp_path)
    # extract
    assert runner.invoke(app, ["extract", str(project_dir)]).exit_code == 0
    # next
    res_next = runner.invoke(app, ["next", str(project_dir), "--allow-missing-context"])
    assert res_next.exit_code == 0
    # translate identity
    translated_dir = _selected_project(project_dir).translated_dir
    assert translated_dir is not None
    for chunk_file in (project_dir / ".booktx" / "chunks").glob("*.json"):
        chunk = json.loads(chunk_file.read_text("utf-8"))
        payload = {
            "chunk_id": chunk["chunk_id"],
            "records": [
                {"id": r["id"], "target": r["source"]} for r in chunk["records"]
            ],
        }
        (translated_dir / chunk_file.name).write_text(
            json.dumps(payload), encoding="utf-8"
        )
    # validate + build
    assert runner.invoke(app, ["validate", str(project_dir)]).exit_code == 0
    assert runner.invoke(app, ["build", str(project_dir)]).exit_code == 0
    assert (
        project_dir / "translations" / "de_default" / "output" / "novel.de.md"
    ).is_file()


def test_init_rejects_unsupported_source(tmp_path: Path):
    bad = tmp_path / "x.pdf"
    bad.write_bytes(b"%PDF-1.4")
    res = runner.invoke(
        app,
        ["init", str(tmp_path / "p"), "--target", "de", "--source-file", str(bad)],
    )
    assert res.exit_code == 1


def _epub_proj(tmp_path: Path, *, build: bool = False) -> Path:
    import tests.test_epub_io as epub_fixtures
    from booktx.config import create_profile, find_source_file, init_source_project

    root = tmp_path / "book"
    proj = init_source_project(root)
    epub_fixtures._make_epub(str(proj.source_dir / "book.epub"))
    find_source_file(proj)
    create_profile(root, "p", target_language="de", target_locale="de-DE")
    runner.invoke(app, ["extract", str(root)])
    if build:
        runner.invoke(app, ["build", str(root), "--profile", "p"])
    return root


def test_check_epub_output_errors_when_no_output(tmp_path: Path) -> None:
    root = _epub_proj(tmp_path, build=False)
    res = runner.invoke(
        app, ["check", str(root), "--profile", "p", "--epub-output", "--json"]
    )
    assert res.exit_code == 1
    payload = json.loads(res.output)
    rules = [f["rule"] for f in payload["findings"]]
    assert "epub_output_missing" in rules


def test_check_epub_output_audits_existing_output(tmp_path: Path) -> None:
    root = _epub_proj(tmp_path, build=True)
    res = runner.invoke(
        app, ["check", str(root), "--profile", "p", "--epub-output", "--json"]
    )
    # A built German translation output should audit cleanly (no errors).
    payload = json.loads(res.output)
    errors = [f for f in payload["findings"] if f["severity"] == "error"]
    assert errors == []
    assert payload["policy"]["applied"] is True
    assert payload["policy"]["language"] == "de-DE"


def test_check_epub_output_rejects_markdown_project(tmp_path: Path) -> None:
    from booktx.config import create_profile, find_source_file, init_source_project

    root = tmp_path / "book"
    proj = init_source_project(root)
    (proj.source_dir / "book.md").write_text("# Hi\n\nText.\n", encoding="utf-8")
    find_source_file(proj)
    create_profile(root, "p", target_language="de")
    res = runner.invoke(
        app, ["check", str(root), "--profile", "p", "--epub-output", "--json"]
    )
    assert res.exit_code == 1
    payload = json.loads(res.output)
    rules = [f["rule"] for f in payload["findings"]]
    assert "not_an_epub_project" in rules


# ---------------------------------------------------------------------------
# Command-tree snapshot (Phase 0): pin the full Typer command surface so
# accidental removal/rename of a command (or the translate/translation alias)
# fails loudly. Uses live Typer/Click introspection, not grep.
# ---------------------------------------------------------------------------


def _command_tree() -> tuple[set[str], dict[str, set[str]]]:
    import typer

    group = typer.main.get_command(app)
    assert hasattr(group, "commands")
    top = set(group.commands.keys())
    sub: dict[str, set[str]] = {}
    for name in sorted(top):
        cmd = group.commands[name]
        if hasattr(cmd, "commands"):
            sub[name] = set(cmd.commands.keys())
    return top, sub


def test_command_tree_top_level_snapshot():
    top, _ = _command_tree()
    expected = {
        "actor",
        "agents",
        "build",
        "chapters",
        "check",
        "context",
        "doctor",
        "epub",
        "extract",
        "harness",
        "identity",
        "init",
        "inspect",
        "mode",
        "model",
        "next",
        "next-chapter",
        "pass-through",
        "profile",
        "qa-scan",
        "review",
        "source",
        "status",
        "translate",
        "translation",
        "validate",
        "version",
        "whoami",
    }
    assert top == expected, (
        f"top-level command set changed.\n"
        f"  missing: {sorted(expected - top)}\n"
        f"  added:   {sorted(top - expected)}"
    )


def test_translation_alias_matches_translate():
    _, sub = _command_tree()
    assert "translate" in sub and "translation" in sub
    assert sub["translate"] == sub["translation"], (
        "translation alias group diverged from translate"
    )


def test_command_tree_group_snapshots():
    _, sub = _command_tree()
    expected = {
        "agents": {"clean", "status", "write"},
        "actor": {"clear", "set", "whoami"},
        "harness": {"clear", "set", "whoami"},
        "model": {"clear", "set", "whoami"},
        "identity": {"whoami"},
        "doctor": {"isolation"},
        "epub": {"extract-text", "grep", "inspect"},
        "review": {
            "activate",
            "configure",
            "deactivate",
            "insert",
            "next",
            "revise-record",
            "status",
            "todo-next",
            "todo-resume",
            "todo-status",
        },
        "source": {"chapter", "record", "status"},
        "context": {
            "add-question",
            "add-term",
            "answer",
            "approve",
            "audit-term",
            "chapter-note",
            "export-pack",
            "import-md",
            "import-pack",
            "init",
            "mandate-term",
            "mark-ready",
            "questionnaire",
            "questions",
            "recommend",
            "remove-term",
            "render",
            "reset-term",
            "status",
        },
        "profile": {
            "compare",
            "create",
            "create-pass-through",
            "list",
            "migrate-current",
            "select",
            "show",
        },
        "version": {
            "current",
            "fork-context",
            "list",
            "select",
            "set-label",
            "show",
        },
        "translate": {
            "activate",
            "audit-inline",
            "compare",
            "export",
            "export-index",
            "get-record",
            "import-legacy",
            "insert",
            "list",
            "migrate-inline-xhtml",
            "migrate-store",
            "next",
            "review",
            "revise-block",
            "revise-record",
            "search",
            "set-record",
            "task-status",
            "todo-next",
            "todo-resume",
            "todo-status",
        },
    }
    for group_name, cmds in expected.items():
        assert group_name in sub, f"group {group_name!r} disappeared"
        assert sub[group_name] == cmds, (
            f"group {group_name!r} changed.\n"
            f"  missing: {sorted(cmds - sub[group_name])}\n"
            f"  added:   {sorted(sub[group_name] - cmds)}"
        )


def test_fixed_commands_present_in_tree():
    """The Phase 0 defect-repair commands must all remain registered."""
    top, sub = _command_tree()
    assert {"review", "translate", "epub"} <= top
    assert {"configure", "revise-record", "todo-next"} <= sub["review"]
    assert {"search", "revise-record"} <= sub["translate"]
    assert {"inspect", "grep", "extract-text"} <= sub["epub"]


def test_cli_help_runs_for_each_group():
    """`<group> --help` exits 0 for every top-level group (smoke)."""
    top, sub = _command_tree()
    groups = [name for name in top if name in sub]
    for name in groups:
        res = runner.invoke(app, [name, "--help"])
        assert res.exit_code == 0, f"`{name} --help` failed: {res.output}"
