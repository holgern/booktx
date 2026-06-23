# Command reference

All commands take a project directory unless stated otherwise.

## Version

```bash
booktx version
booktx --version
booktx version current PROJECT_DIR
booktx version list PROJECT_DIR
booktx version show PROJECT_DIR 1.2 --json
booktx version select PROJECT_DIR 1.2
booktx version set-label PROJECT_DIR 1 "gpt-5.5 low"
booktx version fork-context PROJECT_DIR --note "manual split"
```

``booktx --version` prints the installed package version. `booktx version` with
no subcommand exits non-zero and points you at `booktx --version` plus the
translation-version subcommands. The subcommands inspect and manage the
project-wide translation-version ledger.

## Identity defaults

```bash
booktx whoami PROJECT_DIR
booktx whoami PROJECT_DIR --json
booktx identity whoami PROJECT_DIR
booktx actor whoami PROJECT_DIR
booktx harness whoami PROJECT_DIR
booktx model whoami PROJECT_DIR
booktx actor set PROJECT_DIR user:nahrstaedt
booktx actor set user:nahrstaedt PROJECT_DIR
booktx actor set --project PROJECT_DIR user:nahrstaedt
booktx actor clear PROJECT_DIR
booktx harness set PROJECT_DIR pi
booktx harness set pi PROJECT_DIR
booktx harness set --project PROJECT_DIR pi
booktx harness clear PROJECT_DIR
booktx model set PROJECT_DIR codex-openai/gpt-5.5@low
booktx model set codex-openai/gpt-5.5@low PROJECT_DIR
booktx model set --project PROJECT_DIR codex-openai/gpt-5.5@low
booktx model clear PROJECT_DIR
```

These commands manage `.booktx/identity.json`, which supplies default actor,
harness, and model values for new major tracks in the version ledger. `booktx whoami`
shows the resolved actor/harness/model plus active translation version, context
status and hash, source hash, and translation-store summary without failing
solely because optional state is missing.

## Initialize a project

```bash
booktx init PROJECT_DIR --target TARGET_LANG
booktx init PROJECT_DIR --target TARGET_LANG --source-file SOURCE_FILE --source-lang SOURCE_LANG
booktx init PROJECT_DIR --target TARGET_LANG --chunk-size 25
```

Creates the project layout and writes initial config and names files.

Important options:

| Option           | Meaning                                         |
| ---------------- | ----------------------------------------------- |
| `--target`, `-t` | Required target language code, for example `de` |
| `--source`, `-s` | Source language code; default is `en`           |
| `--source-file`  | Optional source document copied into `source/`  |
| `--chunk-size`   | Maximum records per chunk; default is `50`      |

## Inspect

```bash
booktx inspect PROJECT_DIR
```

Shows:

- source filename
- detected format
- source language
- target language
- estimated record count
- protected terms
- format-specific details

## Extract

```bash
booktx extract PROJECT_DIR
booktx extract PROJECT_DIR --force-rechunk
```

Writes `.booktx/chunks/*.json`.

Extraction is idempotent:

- `chunks/` is rebuilt.
- `translation-store.json` is preserved.
- `translated/` is preserved as compatibility output.
- EPUB extraction writes manifest v2 metadata.
- Fresh EPUB chunks are rejected if they contain `__TAG_` or `__SPANTX_` tokens.

When the source SHA and extraction settings match the previous manifest,
re-extract must reproduce byte-identical chunk files. If `chunk_size` changes
under `record_id_scheme=chunk-local:v1` while accepted translations exist,
extract refuses by default and requires `--force-rechunk`.

## Context commands

### Initialize context

```bash
booktx context init PROJECT_DIR --non-interactive
booktx context init PROJECT_DIR --interactive
booktx context init PROJECT_DIR --force
```

Creates `.booktx/context.json` and `.booktx/context.md`.

`--force` overwrites an existing context.

### List questions

```bash
booktx context questions PROJECT_DIR
```

Prints context questions, status, and any current answers.

### Show status

```bash
booktx context status PROJECT_DIR
```

Prints whether the context is ready, how many required questions remain open, and where the rendered context lives.

### Answer a question

```bash
booktx context answer PROJECT_DIR Q001 --text de-DE
```

Stores the answer in `context.json`, applies known style-field hydration, and regenerates `context.md`.

### Add or update a glossary term

```bash
booktx context add-term PROJECT_DIR Lowlands --target "Tieflande" --forbid Niederlande --forbid Holland --category place --enforce error
```

`--enforce` accepts:

| Value   | Meaning                   |
| ------- | ------------------------- |
| `off`   | Do not validate this term |
| `warn`  | Emit warnings             |
| `error` | Fail validation           |

### Mark context ready

```bash
booktx context mark-ready PROJECT_DIR
booktx context mark-ready PROJECT_DIR --force
```

Without `--force`, this fails while required questions are open.

## Status

```bash
booktx status PROJECT_DIR
booktx status PROJECT_DIR --json
booktx status PROJECT_DIR --chapter 0006
```

Reports deterministic record-, chunk-, chapter-, and word-level progress.
JSON output also includes `version_coverage` and `track_coverage`.

## Command workflow

### Next task

```bash
booktx translate next PROJECT_DIR
booktx translate next PROJECT_DIR --json
booktx translate next PROJECT_DIR --unit paragraph
booktx translate next PROJECT_DIR --unit batch --max-words 500 --format block
booktx translate next PROJECT_DIR --chapter 0006 --unit batch --max-words 500 --format block
booktx translate next PROJECT_DIR --format block --show-sources
booktx translate next PROJECT_DIR --format tsv
```

Returns the next pending work unit, persists a task id, and prints a concise summary with the source file, the durable block file, and a submit command. By default `--format block` does NOT print the source text or heredoc body; add `--show-sources` or `--show-template` to print them inline. It writes `.booktx/tasks/TASK.source.block.txt` (source text), `.booktx/ingest/TASK.block.txt` (editable durable target file with metadata headers, including `translation_version`), and `.booktx/ingest/TASK.json` for JSON compatibility (`schema_version: 2` plus `translation_version`).

### Insert translated records

```bash
booktx translate insert PROJECT_DIR --task-id TASK --file .booktx/ingest/TASK.block.txt --format block
booktx translate insert PROJECT_DIR --task-id TASK --stdin --format block
booktx translate insert PROJECT_DIR --task-id TASK --stdin
booktx translate insert PROJECT_DIR --record-id 0001-000001 --target "..."
booktx translate insert PROJECT_DIR --stdin --format tsv
booktx translate insert PROJECT_DIR --json-file .booktx/ingest/TASK.json
```

Prefer submitting the generated `.booktx/ingest/TASK.block.txt` durable file for normal agent work; use a stdin heredoc only for very small manual fixes. `translate insert` validates submitted records before writing `.booktx/translation-store.json`. Invalid submissions are rejected atomically. If the task was created under translation version `1.1` and the current resolved version is now `1.2`, `translate insert --task-id TASK ...` rejects the stale task and tells you to request a fresh one.
Accepted writes print the resolved dotted version ref, for example `version: 1.2`.

### Task status

```bash
booktx translate task-status PROJECT_DIR --task-id TASK
booktx translate task-status PROJECT_DIR --task-id TASK --json
```

Reports how many task records are accepted vs missing (and stale), the first missing record id, and the source/ingest/submit paths. Makes interrupted runs diagnosable without inspecting JSON. Exits `0` only when every task record is accepted and current, otherwise `1`.

### Set a single record

```bash
booktx translate set-record PROJECT_DIR --task-id TASK --record-id RECORD_ID --stdin
booktx translate set-record PROJECT_DIR --task-id TASK --record-id RECORD_ID --target "..."
```

Commits one translated record at a time. Reads the target from stdin (multiline text preserved) or `--target`, validates that single record, writes it to `.booktx/translation-store.json`, and prints accepted progress. Use this when worried about truncation; committed work survives interruption.

Missing or unreadable submission files (for `--file` / `--json-file`) produce a concise `error: submission file not found: ...` message with an ingest hint, never a Python traceback. Never use `/tmp`.

### Legacy import/export

```bash
booktx translate import-legacy PROJECT_DIR
booktx translate export PROJECT_DIR
booktx translate export PROJECT_DIR --version 1.2
booktx translate export PROJECT_DIR --track 1 --latest-subversion
booktx translate export PROJECT_DIR --all-versions
booktx translate migrate-store PROJECT_DIR
booktx translate migrate-store PROJECT_DIR --write --actor user:nahrstaedt --harness pi --model codex-openai/gpt-5.5@low
```

`import-legacy` copies valid compatibility chunk files from `translated/` into
the nested record-level store. `migrate-store` inspects or rewrites a legacy v1
flat store into the v2 nested shape. `export` materializes compatibility chunk
files from the active accepted candidates by default, can target one exact
version ref, can export the latest accepted subversion for one track, and can
write every accepted version into `translated/<version-ref>/`. Exported records
may include per-record accepted translation `version` metadata such as `1.1`.

### Record inspection and review

```bash
booktx translation get-record PROJECT_DIR 74@38 --before 2 --after 2
booktx translation get-record PROJECT_DIR 74@38 --json
booktx translation list PROJECT_DIR --range 74@38..74@50 --json
booktx translation list PROJECT_DIR --chapter 11 --version 1.2 --json
booktx translation compare PROJECT_DIR 74@38 --versions 1.1,1.2
booktx translation activate PROJECT_DIR 74@38 1.2
booktx translation review PROJECT_DIR 74@38 --activate 1.2 --note "Better in context."
```

Canonical store keys remain padded ids such as `0074-000038`, but the CLI also
accepts shorthand refs such as `74@38`.

## Legacy next summary

```bash
booktx next PROJECT_DIR
booktx next PROJECT_DIR --allow-missing-context
```

Prints the next pending chunk summary and points callers at `booktx translate next`
and `booktx translate insert`.

Exit codes:

| Code | Meaning                                                                     |
| ---- | --------------------------------------------------------------------------- |
| `0`  | A pending chunk was found                                                   |
| `1`  | Context is missing/not ready, no chunks exist, or every chunk is translated |

The default command requires ready context. Use `--allow-missing-context` only for legacy workflows and tests.

## Legacy chapter summary

```bash
booktx chapters PROJECT_DIR
booktx next PROJECT_DIR --unit chapter
booktx next-chapter PROJECT_DIR
```

`chapters` detects chapter ranges and writes `.booktx/chapter-map.json`.

`next --unit chapter` and `next-chapter` print the next incomplete chapter with
record ranges, pending chunk boundaries, and the recommended `booktx translate`
command.

## Validate

```bash
booktx validate PROJECT_DIR
booktx validate PROJECT_DIR --all-versions-strict
```

Checks translated chunks against the contract and context rules, writes `.booktx/reports/validation-report.json`, and exits non-zero on errors. Validation also warns on manifest-vs-config `chunk_size` drift, errors on unsupported `record_id_scheme`, and errors when chunk metadata no longer matches the manifest.

## Build

```bash
booktx build PROJECT_DIR
booktx build PROJECT_DIR --require-complete
```

Writes the final translated document to `output/`.

By default, missing records still fall back to source text. `--require-complete`
fails when any record is missing or invalid. For EPUB, build verifies source
checksums and scans the rebuilt EPUB for unresolved placeholder tokens.
