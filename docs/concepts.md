# Concepts

## Source project

A source project is the shared book and its source-derived state:

- `source/`
- `.booktx/source-config.toml`
- `.booktx/source-manifest.json`
- `.booktx/names.json`
- `.booktx/chapter-map.json`
- `.booktx/chunks/`

## Translation profile

A translation profile is an isolated translation effort under
`translations/<profile>/`.

It owns:

- target language and locale
- default actor / harness / model identity
- profile-local context
- translation store
- version ledger
- task files
- ingest files
- compatibility exported chunks
- validation reports
- rebuilt output

## Active profile

`.booktx/profile-state.json` records the currently selected profile. When
multiple profiles exist, commands that read or mutate translation state should
use `--profile` or rely on the active selection.

## Versions inside a profile

Versions are scoped inside a profile, not across the whole project.

- `1.1` and `1.2` are two candidates or context forks within the same profile
- a model change may create a new major track such as `2.1`
- two profiles may both have a `1.1`, and those are intentionally independent

## Translation store

`translations/<profile>/translation-store.json` is the primary record-level
translation state for that profile.

`translations/<profile>/translated/*.json` remains a compatibility/export
surface managed by `booktx translate export`.

## Context

`translations/<profile>/context.json` is authoritative.

`translations/<profile>/context.md` is a rendered agent view and must not be
treated as the durable source of truth.
