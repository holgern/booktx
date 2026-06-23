# Project layout

`booktx` now uses a **source-first, profile-aware** layout.

```text
book/
  source/
    book.md

  .booktx/
    source-config.toml
    source-manifest.json
    names.json
    chapter-map.json
    profile-state.json
    chunks/
      0001.json
      0002.json

  translations/
    de_gpt5_5/
      config.toml
      identity.json
      context.json
      context.md
      translation-store.json
      translation-version-ledger.json
      tasks/
      ingest/
      translated/
      reports/
      output/
        book.de.md
```

## Shared source scope

`.booktx/` contains only source-derived or source-shared state.

| Path | Scope | Notes |
| --- | --- | --- |
| `.booktx/source-config.toml` | shared | Source language, source file, format, chunk size |
| `.booktx/source-manifest.json` | shared | Source digest and rebuild metadata |
| `.booktx/names.json` | shared | Protected terms |
| `.booktx/chapter-map.json` | shared | Chapter-to-record/chunk metadata |
| `.booktx/profile-state.json` | shared | Active profile selection only |
| `.booktx/chunks/` | shared | Extracted source chunks |

Do **not** put target-language translation state under `.booktx/` in a profile
project.

## Translation profile scope

Every translation effort lives under `translations/<profile>/`.

| Path | Scope | Notes |
| --- | --- | --- |
| `translations/<profile>/config.toml` | profile-local | Target language, locale, output filename, default identity |
| `translations/<profile>/identity.json` | profile-local | Stored actor/harness/model defaults |
| `translations/<profile>/context.json` | profile-local | Authoritative translation context |
| `translations/<profile>/context.md` | profile-local | Rendered context for agents |
| `translations/<profile>/translation-store.json` | profile-local | Primary record-level translation state |
| `translations/<profile>/translation-version-ledger.json` | profile-local | Version history inside this profile |
| `translations/<profile>/tasks/` | profile-local | Persisted translation tasks |
| `translations/<profile>/ingest/` | profile-local | Durable submission files |
| `translations/<profile>/translated/` | profile-local | Compatibility/export chunk JSON |
| `translations/<profile>/reports/` | profile-local | Validation reports |
| `translations/<profile>/output/` | profile-local | Rebuilt translated documents |

## Safety rules

1. A profile is the hard isolation boundary.
2. Different languages must not share one translation store.
3. Model experiments should usually be separate profiles, even for the same target language.
4. When multiple profiles exist, pass `--profile` or select one with `booktx profile select`.
5. Legacy single-layout projects should be migrated with `booktx profile migrate-current`.
