---
name: booktx
description: Use this skill when working with booktx projects
---

# booktx Skill

## Primary goal

Work safely with `booktx`, a deterministic local CLI that prepares Markdown and EPUB documents for translation. `booktx` extracts source text into JSON chunks, a coding agent or human requests manageable task batches, submits translated targets through the CLI, then `booktx validate` checks the contract and `booktx build` reconstructs the output document.

Do not alter source files unless the user explicitly asks for package maintenance.

## When to use this skill

Use this skill for any of these tasks:

- Translate the records returned by `booktx translate next` and submit them with `booktx translate insert`.
- Inspect, validate, or repair translation-store entries or compatibility translated chunk files.
- Run `booktx extract`, `booktx status`, `booktx translate next`, `booktx translate insert`, `booktx validate`, or `booktx build`.
- Maintain the `booktx` Python package, especially extraction, placeholders, validation, rebuild, or CLI behavior.
- Review EPUB/Markdown translation safety and placeholder preservation.

## Core contract

A source chunk looks like this:

```json
{
  "schema_version": 2,
  "chunk_id": "0001",
  "chunk_size": 50,
  "record_id_scheme": "chunk-local:v1",
  "source_language": "en",
  "target_language": "de",
  "records": [
    {
      "id": "0001-000001",
      "source": "__NAME_001__ looked at __NAME_002__.",
      "protected_terms": ["Alice", "Mr. Smith"],
      "placeholders": [
        { "token": "__NAME_001__", "original": "Alice", "kind": "name" },
        { "token": "__NAME_002__", "original": "Mr. Smith", "kind": "name" }
      ]
    }
  ]
}
```

Compatibility translated chunk files still look like this when exported:

```json
{
  "chunk_id": "0001",
  "records": [
    {
      "id": "0001-000001",
      "version": "1.1",
      "target": "__NAME_001__ sah __NAME_002__ an."
    }
  ]
}
```

## Non-negotiable translation rules

- Use the payload shape the workflow asks for. For normal CLI submission, prefer the block format with `>>> RECORD_ID` headers. For compatibility translated chunk files, return or write only a JSON object.
- Keep `chunk_id` exactly unchanged when you are working with compatibility translated chunk files.
- Keep every record `id` exactly unchanged.
- Keep the same number and order of records unless the user is explicitly asking to repair source chunks. For normal translation, never merge, split, add, or delete records.
- Translate only the `source` text into `target` text.
- Preserve every visible `__NAME_NNN__` token exactly, and preserve any visible legacy `__TAG_NNN__` token exactly. New EPUB chunks should not contain TAG tokens at all.
- Do not invent new placeholder tokens.
- Do not replace a `__NAME_NNN__` token with the visible original name. Build restores names later.
- Do not translate inline code, URLs, tag fragments, or protected names hidden behind placeholders.
- Keep each `target` non-empty.
- Never edit `.booktx/chunks/*.json` directly.
- Never edit `.booktx/translation-store.json` directly.

## Required context gate

Before translating any chunk or chapter, read `.booktx/context.md`. If it does not exist, or `.booktx/context.json` has `ready: false`, do not translate. Ask the user the context questionnaire first and write the answers to `.booktx/context.json`, then render `.booktx/context.md`.

Glossary entries in the context override ordinary dictionary translations. Do not use a target listed under `forbidden_targets`. For this book, do not translate `Lowlands` / `Lowlander` as `Niederlande` / `Niederländer` unless the user explicitly approves it in context.

Required sequence:

1. Run or ask for context building before translation.
2. Read `.booktx/context.md` before opening any chunk.
3. If `.booktx/context.md` or `.booktx/context.json` is missing or `ready=false`, stop translating and ask the user the initial questionnaire.
4. Before translating a new chapter, read context again.
5. Use the glossary as stronger than general dictionary intuition.
6. Never use any `forbidden_targets` listed in the context.
7. After each completed chapter, update the chapter summary/open issues in context.
8. Run `booktx validate` and fix both contract errors and context terminology errors.

## Translation workflow

From a project root:

```bash
booktx extract .
booktx whoami .
booktx context status .
booktx status .
booktx translate next . --unit batch --max-words 500 --format block
booktx translate next . --chapter 0010 --unit batch --max-words 500 --format block
```

Open `.booktx/context.md` first, then use `booktx translate next` to fetch the exact records to translate. The command prints a concise summary and writes `.booktx/tasks/TASK.source.block.txt` (source text) plus `.booktx/ingest/TASK.block.txt` (editable durable target file). Prefer the durable-file workflow for normal agent work:

```bash
# 1. fetch the task (writes the source + editable block files)
booktx translate next . --unit batch --max-words 500 --format block
# 2. read the source file, then fill .booktx/ingest/TASK.block.txt
# 3. submit the durable file
booktx translate insert . --task-id TASK --file .booktx/ingest/TASK.block.txt --format block
```

Diagnose an interrupted run and commit one record at a time when truncation is a concern:

```bash
booktx translate task-status . --task-id TASK
booktx translate set-record . --task-id TASK --record-id RECORD_ID --stdin
```

Keep `.booktx/ingest/TASK.json` and `--json-file` for compatibility tooling. Use a stdin heredoc only for very small manual fixes. Never use `/tmp`; Termux and some restricted environments do not provide it. Do not request `--unit chapter --json` for normal translation, do not write Python helper scripts to create translation submissions, and do not edit `.booktx/translated/*.json` directly.

If `booktx translate insert . --task-id TASK ...` reports a stale translation
task, stop using that task file. Run `booktx translate next . ...` again, fill
the fresh `.booktx/ingest/` file, and resubmit under the current translation
version.

After writing translations:

```bash
booktx validate .
booktx build .
```

If validation fails, repair the translated JSON. Do not patch the source chunk to make validation pass unless the source extraction itself is defective and the user asked for maintenance.

## Placeholder checklist before saving a translated submission

For each record:

1. Copy the `id` exactly.
2. Translate `source` into `target`.
3. Search the source for tokens matching `__(NAME|TAG)_\d+__`.
4. Confirm every token appears in the target.
5. Confirm no additional tokens appear in the target.
6. Confirm the target is a string and not empty.
7. Confirm the final payload is valid block text or valid JSON, depending on the workflow you are using.

A simple verification snippet for one chunk:

```python
import json, re, sys
from pathlib import Path

src = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
tgt = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
assert src["chunk_id"] == tgt["chunk_id"]
assert len(src["records"]) == len(tgt["records"])
rx = re.compile(r"__(?:NAME|TAG)_\d+__")
for s, t in zip(src["records"], tgt["records"], strict=True):
    assert s["id"] == t["id"]
    assert t["target"].strip()
    assert sorted(rx.findall(s["source"])) == sorted(rx.findall(t["target"]))
```

Prefer `booktx validate` as the authoritative check.

## Identity and version inspection

- Use `booktx --version` for the CLI package version.
- Treat `booktx version` as the translation-version command group; use subcommands such as `current`, `list`, `show`, `select`, `fork-context`, and `set-label`.
- Use `booktx whoami .`, `booktx actor whoami .`, `booktx harness whoami .`, and `booktx model whoami .` to inspect the resolved identity and active version state.

## Package maintenance map

- `booktx/models.py`: Pydantic models for source and translated JSON contracts.
- `booktx/placeholders.py`: placeholder token creation and restoration.
- `booktx/chunking.py`: sentence segmentation and chunk packing.
- `booktx/markdown_io.py`: Markdown extraction and rebuild.
- `booktx/html_io.py`: XHTML extraction and rebuild.
- `booktx/epub_io.py`: EPUB extraction/build adapter over epub2text and text2epub.
- `booktx/epub_manifest.py`: EPUB manifest conversion helpers for epub2text/text2epub.
- `booktx/config.py`: project layout, config TOML, manifest, names, source discovery.
- `booktx/validate.py`: contract validation and validation report writing.
- `booktx/build.py`: maps translated records back to spans and rebuilds outputs.
- `booktx/cli.py`: Typer command surface.

## Maintenance guardrails

- Keep booktx deterministic, local, and network-free.
- Do not add automatic translation API calls to core.
- Do not change chunk IDs, record IDs, or JSON field names without migration and tests.
- Keep `booktx extract` idempotent: it may rebuild `.booktx/chunks`, but must not delete `.booktx/translated`.
- Stop and resolve extraction drift, chunk-size drift, or record-id drift before translating further.
- Use `booktx extract --force-rechunk` only for an explicit risky rechunk after backing up or migrating accepted translations.
- Keep build/rebuild structure-preserving for Markdown and EPUB.
- Add tests before refactoring extractor internals.
- Treat `booktx validate` as the gate before build.

## Known current maintenance priorities

- Add Python 3.10 `tomli` fallback because `tomllib` is not available in Python 3.10.
- Align CLI docs and options: `--source`/`--source-file`/`--source-lang` are currently easy to confuse.
- Prefer console script target `booktx.cli:main`.
- Remove duplicate unreachable `return` in `booktx/epub_io.py`.
- Consider making `booktx build` fail on invalid present translations instead of silently using partial fallback behavior.

## Maintainer note: sentence segmentation

`booktx` uses `phrasplit` for deterministic sentence segmentation in chunk extraction.
When editing `booktx/chunking.py`, keep the simple backend forced with
`use_spacy=False` unless the user explicitly requests an opt-in spaCy mode.
Do not allow environment-dependent auto-detection in normal extraction.

## EPUB pipeline guidance

- The current EPUB production path uses `epub2text` for extraction and `text2epub` for rebuilds.
- New EPUB chunks must not expose `__TAG_NNN__` or `__SPANTX_NNNN__` tokens.
- For new EPUB projects, visible placeholders should usually be `__NAME_NNN__` only.
- If a freshly extracted EPUB chunk contains TAG tokens, treat it as a maintenance defect and re-run extraction after fixing the pipeline.
- Identity/no-op EPUB builds must stay byte-identical to the source EPUB.
- Changed EPUB blocks may lose inner inline formatting in the current MVP rebuild mode; do not reintroduce TAG placeholders as a workaround.
