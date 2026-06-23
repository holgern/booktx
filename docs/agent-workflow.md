# Agent workflow

This page is written for coding agents and human translators working inside a booktx project.

## Required sequence

From the project root:

```bash
booktx extract .
booktx context status .
booktx status .
booktx translate next . --unit batch --max-words 500 --format block
```

If context is missing or not ready, stop translating and build the context first.

Before translating, inspect the resolved defaults and active version with:

```bash
booktx whoami .
booktx actor whoami .
booktx harness whoami .
booktx model whoami .
```

## Before opening work items

Read:

```text
.booktx/context.md
```

Then request the next task from:

```bash
booktx translate next . --unit batch --max-words 500 --format block
```

or a chapter-focused task from:

```bash
booktx status . --chapter 0010
booktx translate next . --chapter 0010 --unit batch --max-words 500 --format block
```

`booktx translate next` returns a task id and a concise summary: the chapter, unit, record count, source words, the source file, the editable durable block file, and the submit command. It writes three files per task: `.booktx/tasks/TASK.source.block.txt` (the source text to translate), `.booktx/ingest/TASK.block.txt` (the editable durable target file you fill in), and `.booktx/ingest/TASK.json` for JSON compatibility. Do not infer chunk ranges manually.

When you need a focused reread, inspect one record plus nearby source context with:

```bash
booktx translation get-record . 74@38 --before 2 --after 2
```

`74@38` is CLI shorthand only; the canonical store key remains the padded id
`0074-000038`.

## Translate only task records

For each source record:

- copy the `id` exactly
- translate `source` into `target`
- keep placeholders exactly
- keep record order exactly
- keep one target per source record

Do not add commentary, Markdown fences, or alternate translations.

## Preserve placeholders

Example source:

```json
{
  "id": "0001-000001",
  "source": "__NAME_001__ said, \"Look at __NAME_002__.\"",
  "protected_terms": ["Alice", "Baker Street"],
  "placeholders": [
    { "token": "__NAME_001__", "original": "Alice", "kind": "name" },
    { "token": "__NAME_002__", "original": "Baker Street", "kind": "name" }
  ]
}
```

Valid target:

```json
{
  "id": "0001-000001",
  "target": "__NAME_001__ sagte: „Sieh dir __NAME_002__ an.“"
}
```

Invalid targets:

```json
{ "id": "0001-000001", "target": "Alice sagte: „Sieh dir Baker Street an.“" }
{ "id": "0001-000001", "target": "__NAME_1__ sagte: ..." }
{ "id": "0001-000001", "target": "__NAME_001__ sagte: ..." }
```

The first replaces placeholders with originals. The second changes token padding. The third drops a required token.

## Submit through the CLI

Prefer the generated durable block file for normal agent work:

1. Run `booktx translate next . --unit batch --max-words 500 --format block`.
2. Read `.booktx/tasks/TASK.source.block.txt` for the source text.
3. Fill `.booktx/ingest/TASK.block.txt` with the translated targets under each `>>> RECORD_ID` header (small edits are fine; work already committed to `translation-store.json` survives interruption).
4. Submit it:

```bash
booktx translate insert . --task-id TASK --file .booktx/ingest/TASK.block.txt --format block
```

The success output now includes the resolved dotted version ref, for example
`version: 1.2`.

Check progress after an interruption without inspecting JSON by hand:

```bash
booktx translate task-status . --task-id TASK
```

To commit one record at a time (for example when worried about truncation), read the target from stdin — multiline text is preserved:

```bash
booktx translate set-record . --task-id TASK --record-id RECORD_ID --stdin
```

Use a direct stdin heredoc only for very small manual fixes:

```bash
booktx translate insert . --task-id TASK --stdin --format block <<'BOOKTX'
>>> RECORD_ID
Translated target.
BOOKTX
```

JSON remains available for compatibility:

```bash
booktx translate insert . --task-id TASK --json-file .booktx/ingest/TASK.json
```

Never use `/tmp`; Termux and some restricted environments do not provide it, and booktx reports a missing submission file with a concise error rather than a traceback. Do not request `--unit chapter --json` for normal translation, do not write Python helper scripts to build submission payloads, do not edit `.booktx/chunks/*.json` or `.booktx/translation-store.json` directly during normal work, and do not edit `.booktx/translated/*.json` directly. That directory is compatibility output managed by `booktx translate export`.

If `translate insert --task-id TASK ...` reports a stale translation task, do not
reuse the old task file. Request a fresh task with `booktx translate next . ...`,
fill the new `.booktx/ingest/` file, and resubmit under the current translation
version.

## Version-aware review

Accepted translations are tracked as nested candidates under each source record.
The version ledger stores actor, harness, and model once per major track and
stores context SHA once per subversion. Do not duplicate model metadata on every
candidate.

Useful commands:

```bash
booktx translation compare . 74@38 --versions 1.1,1.2
booktx translation activate . 74@38 1.2
booktx translation review . 74@38 --activate 1.2 --note "Better in context."
booktx version current .
booktx version list .
```

## Validate often

Run:

```bash
booktx validate .
```

Fix errors immediately. Validation catches structural issues that may otherwise corrupt the rebuild.

## Build only after validation

```bash
booktx build .
```

For EPUB, build can fail if:

- the source EPUB changed after extraction
- the manifest is from the legacy pipeline
- a replacement no longer matches the expected source block
- unresolved placeholder tokens leak into the rebuilt EPUB

## Chapter workflow

Use chapter-aware progress to keep style continuity while still translating in manageable batches:

```bash
booktx chapters .
booktx status . --chapter 0010
booktx translate next . --chapter 0010 --unit batch --max-words 500 --format block
```

Repeat batch requests until `booktx status . --chapter 0010` reports zero remaining records. After completing the chapter, add or update chapter notes in the context if new terminology, voice decisions, or open issues appeared.

## Repair workflow

If validation reports structural errors:

1. Inspect the source chunk in `.booktx/chunks/`.
2. Inspect the affected store/task payload or compatibility translated chunk.
3. Compare `chunk_id`, record count, record order, and ids.
4. Restore all visible placeholders from source to target.
5. Remove commentary outside the expected payload format.
6. Re-run `booktx validate .`.

Validate often, but do not run `booktx build .` after every small batch unless you explicitly need a rebuilt book at that milestone.

If extraction produced EPUB chunks containing `__TAG_` or `__SPANTX_`, treat that as a package defect or legacy extraction artifact. Re-extract after upgrading the EPUB pipeline.
