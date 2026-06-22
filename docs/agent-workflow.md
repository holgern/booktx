# Agent workflow

This page is written for coding agents and human translators working inside a booktx project.

## Required sequence

From the project root:

```bash
booktx extract .
booktx context status .
booktx status .
booktx translate next . --unit batch --max-words 700 --format block
```

If context is missing or not ready, stop translating and build the context first.

## Before opening work items

Read:

```text
.booktx/context.md
```

Then request the next task from:

```bash
booktx translate next . --unit batch --max-words 700 --format block
```

or a chapter-focused task from:

```bash
booktx status . --chapter 0010
booktx translate next . --chapter 0010 --unit batch --max-words 700 --format block
```

`booktx translate next` returns a task id, the exact record ids to translate, ingest paths, and submit hints. It also creates `.booktx/ingest/TASK.json` for JSON compatibility and `.booktx/ingest/TASK.block.txt` as a durable block template. Do not infer chunk ranges manually.

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

Prefer a direct heredoc for normal agent work:

```bash
booktx translate insert . --task-id TASK --stdin --format block <<'BOOKTX'
>>> RECORD_ID
Translated target.

>>> NEXT_RECORD_ID
Translated target.
BOOKTX
```

Use `.booktx/ingest/TASK.block.txt` only when you want a durable submission file under version control:

```bash
booktx translate insert . --task-id TASK --file .booktx/ingest/TASK.block.txt --format block
```

JSON remains available for compatibility:

```bash
booktx translate insert . --task-id TASK --json-file .booktx/ingest/TASK.json
```

Do not request `--unit chapter --json` for normal translation, do not write Python helper scripts to build submission payloads, and do not edit `.booktx/translated/*.json` directly during normal work. That directory is compatibility output managed by `booktx translate export`.

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
booktx translate next . --chapter 0010 --unit batch --max-words 700 --format block
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
