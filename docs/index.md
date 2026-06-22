# booktx documentation

`booktx` is a deterministic command-line tool for preparing Markdown and EPUB books for translation by a coding agent or a human translator. It extracts translatable prose into JSON chunks, validates translated JSON against a strict contract, and rebuilds the target document.

`booktx` does not translate text itself. It performs local extraction, bookkeeping, validation, and rebuild operations only.

```{toctree}
:maxdepth: 2
:caption: User guide

quickstart
concepts
project-layout
commands
translation-contract
context
agent-workflow
markdown
epub
troubleshooting
```

```{toctree}
:maxdepth: 2
:caption: Maintainer guide

architecture
development
api
```

## What booktx is for

Use booktx when you need a repeatable translation workflow with explicit files:

1. Put exactly one source document into `source/`.
2. Run extraction to create `.booktx/chunks/*.json`.
3. Build or approve `.booktx/context.json` and `.booktx/context.md`.
4. Translate each chunk by writing `.booktx/translated/*.json`.
5. Run validation.
6. Rebuild the final document into `output/`.

The core safety rule is simple: a translated chunk must keep the same chunk id, record ids, record count, and placeholders as the extracted source chunk.

## Supported source formats

| Format | Source suffixes | Extraction path | Rebuild path |
|---|---|---|---|
| Markdown | `.md`, `.markdown` | `booktx.markdown_io` | `booktx.markdown_io` |
| EPUB | `.epub` | `epub2text` via `booktx.epub_io` | `text2epub` via `booktx.build` |

Unsupported formats are rejected during project initialization or source discovery.

## Non-goals

booktx v1 does not provide a web UI, automatic LLM calls, translation memory, DRM handling, PDF/DOCX input, automatic publishing, or concurrent agent orchestration. These are deliberately outside the deterministic core.
