# Architecture

booktx is intentionally small: format adapters extract prose spans, chunking turns spans into stable sentence records, validation enforces the translation contract, and build reconstructs the output.

## Data flow

```text
source document
  |
  v
format extractor
  |
  v
ProseSpan[]
  |
  v
segment_spans()
  |
  v
Record[]
  |
  v
pack_chunks()
  |
  v
.booktx/chunks/*.json
  |
  v
translator / coding agent
  |
  v
.booktx/translation-store.json
  |
  +--> .booktx/translated/*.json (compatibility export / legacy input)
  |
  v
validate_project()
  |
  v
build_project()
  |
  v
output/book.<target>.<ext>
```

## Main modules

| Module                 | Responsibility                                             |
| ---------------------- | ---------------------------------------------------------- |
| `booktx.cli`           | Typer command surface                                      |
| `booktx.config`        | Project layout, config, names, manifest IO                 |
| `booktx.models`        | Pydantic models for chunk and manifest contracts           |
| `booktx.context`       | Translation context models, questions, glossary, rendering |
| `booktx.markdown_io`   | Markdown extraction and rebuild                            |
| `booktx.epub_io`       | EPUB extraction and rebuild adapter                        |
| `booktx.epub_manifest` | EPUB v2 manifest conversion and raw block mapping          |
| `booktx.html_io`       | Legacy/shared XHTML extraction and rebuild helpers         |
| `booktx.chunking`      | Sentence segmentation and chunk packing                    |
| `booktx.placeholders`  | Name/tag placeholder protection and restoration            |
| `booktx.validate`      | Contract and context validation                            |
| `booktx.build`         | Final Markdown/EPUB rebuild                                |
| `booktx.chapters`      | Chapter detection and chapter map persistence              |

## Project loading

`booktx.config.load_project()` resolves paths and validates `.booktx/config.toml`.

`booktx.config.find_source_file()` selects the source document:

1. use `config.source_file` if present and valid
2. otherwise scan `source/` for exactly one supported file
3. update config when a single source is discovered

## Extraction

Markdown extraction creates a text template with internal span tokens and a list of protected prose spans.

EPUB extraction delegates text discovery to `epub2text`, then maps every block back to raw source offsets so `text2epub` can replace changed blocks.

## Chunking

`booktx.chunking.segment_spans()` uses `phrasplit.split_with_offsets()` with the regex backend. Language codes are mapped from BCP-47-ish project codes to phrasplit model names for abbreviation handling.

`pack_chunks()` assigns stable chunk ids and record ids.

## Validation

Validation is intentionally stricter than build. It catches malformed JSON, record drift, placeholder drift, empty targets, protected-name problems, stale translated files, and context glossary violations.

## Build

Build loads source chunks and translated chunks into a target stream.

For Markdown, it re-extracts source spans, aligns records by segmentation counts, restores placeholders, and substitutes replacements into the Markdown template.

For EPUB, it loads the stored manifest, verifies source checksums, creates a `text2epub.ReplacementPlan`, rebuilds the EPUB, and scans for unresolved tokens.

## Important invariants

- Source chunks are generated files.
- Translated chunks are user/agent-owned files.
- Extraction must not delete translations.
- Record ids must remain stable for a fixed source and chunk size.
- EPUB builds must not proceed if the source checksum changed.
- `context.json` is authoritative; `context.md` is rendered.
- Fresh EPUB chunks must not contain `__TAG_` or `__SPANTX_`.
