# Concepts

## Project

A booktx project is a directory with a `source/` folder, a `.booktx/` state directory, and an `output/` folder. Project configuration is stored in `.booktx/config.toml`.

## Source document

The source document is the Markdown or EPUB file to translate. A project should contain exactly one supported source file in `source/`, unless `.booktx/config.toml` points to a specific `source_file`.

Supported suffixes are:

- `.md`
- `.markdown`
- `.epub`

## Prose span

A prose span is a block of translatable text found by a format-specific extractor.

For Markdown, a span usually comes from an inline token inside a paragraph, heading, list item, blockquote, or table cell.

For EPUB, a span maps to a text block extracted by `epub2text` and tracked through the EPUB manifest used by `text2epub`.

## Record

A record is one translatable sentence. Records are produced by segmenting prose spans with `phrasplit`.

Record ids are assigned during chunk packing:

```text
NNNN-NNNNNN
```

Example:

```text
0003-000012
```

This means the twelfth record inside chunk `0003`.

The current extraction records `record_id_scheme = "chunk-local:v1"`. Under that
scheme, changing `chunk_size` can renumber later record ids, so `booktx extract`
refuses risky rechunking when accepted translations already exist unless you
pass `--force-rechunk`.

## Chunk

A chunk is a JSON file containing a small ordered batch of source records. Chunks are written to `.booktx/chunks/`.

Chunks now also record `schema_version`, `chunk_size`, and `record_id_scheme`.
The chunk size is configured in `.booktx/config.toml` as `chunk_size`.

## Translation store

`.booktx/translation-store.json` is the primary record-level translation state
owned by `booktx`. `booktx translate insert` writes accepted records here after
validation.

## Translated chunk

`.booktx/translated/*.json` remains a compatibility/export layer. Valid legacy
chunk files still count as progress, and `booktx translate export` can
materialize full translated chunk files from the accepted store.

## Placeholder

A placeholder is a token that hides text that must not be translated directly.

Common placeholders:

```text
__NAME_001__
__TAG_001__
```

`__NAME_NNN__` protects names, brands, places, and other user-approved verbatim terms.

`__TAG_NNN__` protects non-translatable inline material in Markdown and legacy EPUB chunks. New EPUB extraction should not emit `__TAG_NNN__` tokens.

## Protected term

A protected term is an original source string listed in `.booktx/names.json` or derived from placeholder metadata. Protected terms are hidden behind `__NAME_NNN__` during extraction and restored during build.

## Manifest

`.booktx/manifest.json` stores source metadata, extraction settings
(`chunk_size`, `record_id_scheme`, segmenter metadata, protected-name hash), and
format-specific rebuild metadata.

For EPUB projects, the manifest is critical. It stores the source EPUB checksum, the `epub2text` to `text2epub` mapping, span references, and navigation references. Build fails if the source EPUB bytes no longer match the manifest.

## Translation context

`.booktx/context.json` stores user-approved translation decisions. `.booktx/context.md` is a rendered view for agents.

The context includes:

- target locale
- style rules
- glossary entries
- forbidden terms
- open questions
- chapter notes

`booktx translate next` refuses to return translation work unless context is
present and ready, unless the caller uses the explicit legacy override.

## Chapter map

`.booktx/chapter-map.json` is generated metadata that maps detected chapters to
chunk and record ranges. It supports chapter-level workflows such as:

```bash
booktx status ./project --chapter 0006
booktx translate next ./project --chapter 0006 --unit batch --max-words 500 --format block
```
