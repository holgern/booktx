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

| Option | Meaning |
|---|---|
| `--target`, `-t` | Required target language code, for example `de` |
| `--source`, `-s` | Source language code; default is `en` |
| `--source-file` | Optional source document copied into `source/` |
| `--chunk-size` | Maximum records per chunk; default is `50` |

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
- `translated/` is preserved.
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

| Value | Meaning |
|---|---|
| `off` | Do not validate this term |
| `warn` | Emit warnings |
| `error` | Fail validation |

### Mark context ready

```bash
booktx context mark-ready PROJECT_DIR
booktx context mark-ready PROJECT_DIR --force
```

Without `--force`, this fails while required questions are open.

## Next chunk

```bash
booktx next PROJECT_DIR
booktx next PROJECT_DIR --allow-missing-context
```

Prints the first source chunk without a matching translated chunk.

Exit codes:

| Code | Meaning |
|---|---|
| `0` | A pending chunk was found |
| `1` | Context is missing/not ready, no chunks exist, or every chunk is translated |

The default command requires ready context. Use `--allow-missing-context` only for legacy workflows and tests.

## Chapter workflow

```bash
booktx chapters PROJECT_DIR
booktx next PROJECT_DIR --unit chapter
booktx next-chapter PROJECT_DIR
```

`chapters` detects chapter ranges and writes `.booktx/chapter-map.json`.

`next --unit chapter` and `next-chapter` print the next incomplete chapter and every chunk it covers.

## Validate

```bash
booktx validate PROJECT_DIR
```

Checks translated chunks against the contract and context rules, writes `.booktx/reports/validation-report.json`, and exits non-zero on errors.

## Build

```bash
booktx build PROJECT_DIR
```

Writes the final translated document to `output/`.

For EPUB, build verifies source checksums and scans the rebuilt EPUB for unresolved placeholder tokens.
