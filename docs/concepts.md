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

Subversions are baseline-scoped, not full-live-context-scoped:

- a chapter-note append updates the next task's effective context but keeps the
  same dotted version;
- a baseline policy change (style, glossary, answered questions, global rules,
  readiness, source metadata, language metadata) creates or selects the next
  subversion inside the current track.

## Translation store

`translations/<profile>/translation-store.json` is the primary record-level
translation state for that profile.

`translations/<profile>/translated/*.json` remains a compatibility/export
surface managed by `booktx translate export`.

## Editor QA indexes

`booktx translate export-index` writes three generated profile-local artifacts:

- `source-index.json` -- source text only, for isolated profile workflows and source-language search
- `target-index.json` -- effective target text only, for target-language search without source false positives
- `source-target-index.json` -- slim source/target side-by-side view for translation-fit scanning

All three are derived from the store, source chunks, and chapter map. They are
safe to delete and regenerate. Do not edit them manually and do not use them as
build input. The canonical state remains `translation-store.json`.

## Context

`translations/<profile>/context.json` is authoritative.

`translations/<profile>/context.md` is a rendered agent view and must not be
treated as the durable source of truth.

Each task composes a context view from the current baseline plus the chapter
notes that come before the target chapter in chapter-map order. That composed
view is snapshotted under `translations/<profile>/context-history/views/<sha>/`
and becomes immutable task evidence.

Context is profile-local and never shared by linking or symlinking. For books
in the same series, export reusable policy (style, global rules, glossary,
approved question answers) as a series context pack and import it into the
other profile: see `booktx context export-pack` / `import-pack`.
