# Command reference

All commands take a project directory unless stated otherwise.

## Version

```bash
booktx version
booktx --version
```

Prints the installed package version.

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
```

Writes `.booktx/chunks/*.json`.

Extraction is idempotent:

- `chunks/` is rebuilt.
- `translation-store.json` is preserved.
- `translated/` is preserved as compatibility output.
- EPUB extraction writes manifest v2 metadata.
- Fresh EPUB chunks are rejected if they contain `__TAG_` or `__SPANTX_` tokens.

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

Returns the next pending work unit, persists a task id, and prints a concise summary with the source file, the durable block file, and a submit command. By default `--format block` does NOT print the source text or heredoc body; add `--show-sources` or `--show-template` to print them inline. It writes `.booktx/tasks/TASK.source.block.txt` (source text), `.booktx/ingest/TASK.block.txt` (editable durable target file with metadata headers), and `.booktx/ingest/TASK.json` for JSON compatibility.

### Insert translated records

```bash
booktx translate insert PROJECT_DIR --task-id TASK --file .booktx/ingest/TASK.block.txt --format block
booktx translate insert PROJECT_DIR --task-id TASK --stdin --format block
booktx translate insert PROJECT_DIR --task-id TASK --stdin
booktx translate insert PROJECT_DIR --record-id 0001-000001 --target "..."
booktx translate insert PROJECT_DIR --stdin --format tsv
booktx translate insert PROJECT_DIR --json-file .booktx/ingest/TASK.json
```

Prefer submitting the generated `.booktx/ingest/TASK.block.txt` durable file for normal agent work; use a stdin heredoc only for very small manual fixes. `translate insert` validates submitted records before writing `.booktx/translation-store.json`. Invalid submissions are rejected atomically.

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
```

`import-legacy` copies valid compatibility chunk files from `translated/` into
the record-level store. `export` materializes full translated chunk files for
chunks whose records are all accepted in the store.

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
```

Checks translated chunks against the contract and context rules, writes `.booktx/reports/validation-report.json`, and exits non-zero on errors.

## Build

```bash
booktx build PROJECT_DIR
booktx build PROJECT_DIR --require-complete
```

Writes the final translated document to `output/`.

By default, missing records still fall back to source text. `--require-complete`
fails when any record is missing or invalid. For EPUB, build verifies source
checksums and scans the rebuilt EPUB for unresolved placeholder tokens.
