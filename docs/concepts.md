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

## Chunk

A chunk is a JSON file containing a small ordered batch of source records. Chunks are written to `.booktx/chunks/`.

The chunk size is configured in `.booktx/config.toml` as `chunk_size`.

## Translated chunk

A translated chunk is a JSON file written by a human translator or coding agent. It must be placed in `.booktx/translated/` and must have the same file stem as the source chunk.

For example:

```text
.booktx/chunks/0007.json
.booktx/translated/0007.json
```

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

`.booktx/manifest.json` stores source metadata and format-specific rebuild metadata.

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

`booktx next` refuses to return translation work unless context is present and ready, unless the caller uses the explicit legacy override.

## Chapter map

`.booktx/chapter-map.json` is generated metadata that maps detected chapters to chunk ranges. It supports chapter-level workflows such as:

```bash
booktx next ./project --unit chapter
booktx next-chapter ./project
```
