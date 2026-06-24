# Profiles

`booktx` translation profiles let one source book support multiple isolated
translation efforts safely.

Examples:

- `de_gpt5_5`
- `de_glm_5_2`
- `fr_gpt5_5`

## Why profiles exist

Without profiles, all mutable translation state lands in one shared store. That
mixes different languages, different model experiments, and different context
decisions.

Profiles prevent that by moving mutable translation state under
`translations/<profile>/`.

## Commands

```bash
booktx profile create ./book de_gpt5_5 --target de --target-locale de-DE --select
booktx profile list ./book
booktx profile show ./book de_gpt5_5
booktx profile select ./book de_gpt5_5
booktx profile compare ./book --profiles de_gpt5_5,de_glm_5_2 --record 0001-000001
booktx profile migrate-current ./book de_gpt5_5 --select
```

## Resolution rules

1. Explicit `--profile` wins.
2. Otherwise the active profile from `.booktx/profile-state.json` is used.
3. Otherwise exactly one existing profile is auto-resolved.
4. Otherwise target-dependent commands fail until a profile is chosen explicitly.

## What is isolated?

Each profile owns its own copy of all mutable translation state under
`translations/<profile>/`:

| Path                              | Meaning                               |
| --------------------------------- | ------------------------------------- |
| `config.toml`                     | Profile config (target, output name)  |
| `identity.json`                   | Live actor/harness/model identity     |
| `context.json` / `context.md`     | Translation context and rendered form |
| `translation-store.json`          | Primary record-level translations     |
| `translation-version-ledger.json` | Version tracks and subversions        |
| `tasks/`                          | Durable translation task files        |
| `ingest/`                         | Submission templates (agent edits)    |
| `translated/`                     | Generated compatibility export        |
| `reports/`                        | Validation/build reports              |
| `output/`                         | Final rebuilt document                |

Two profiles never share any of the above. Translations accepted into one
profile are invisible to another.

## What is shared?

Source-derived state under `.booktx/` is shared by all profiles:

| Path                   | Meaning                             |
| ---------------------- | ----------------------------------- |
| `source-config.toml`   | Source language/format/chunking     |
| `source-manifest.json` | Source hash and extraction manifest |
| `names.json`           | Protected-term glossary             |
| `chapter-map.json`     | Cached chapter boundaries           |
| `chunks/`              | Immutable extracted source records  |
| `profile-state.json`   | Active-profile selector             |

Re-extracting the source updates the shared state for every profile at once.

## When to create a new profile?

Create a new profile whenever you want a hard isolation boundary:

- **Different target language**: `de_gpt5_5`, `fr_gpt5_5`, `es_gpt5_5`.
- **Different model experiment**: `de_gpt5_5` vs `de_glm_5_2` for the same
  language, so the two outputs never contaminate each other.
- **Different context decisions**: a re-translation under revised glossary or
  style rules, kept separate from a previous accepted run.

Do **not** create a new profile for a routine re-translation of the same
language/model/context; that is a _version_, not a profile.

## What stays a version?

Versions live _inside_ a profile. Two profiles may both contain version `1.1`;
they are unrelated.

- A **model/actor/harness identity change** creates or selects a major track
  (e.g. `1`).
- A **baseline policy change** creates or selects a subversion inside that
  track (e.g. `1.2`).
- A **chapter-note append** changes the next task's composed context view but
  does **not** create a new dotted version on its own.

Use:

```bash
booktx version current . --profile PROFILE
booktx version list . --profile PROFILE
booktx translation compare . --profile PROFILE RECORD --versions 1.1,1.2
booktx translation activate . --profile PROFILE RECORD 1.2
```

## Migration from legacy layout

A legacy single-layout project keeps all state under `.booktx/`. Migrate it
into the profile layout:

```bash
booktx profile migrate-current ./book PROFILE --select
```

Before:

```text
book/.booktx/{config.toml, translation-store.json, tasks/, ingest/, ...}
```

After:

```text
book/.booktx/{source-config.toml, source-manifest.json, chunks/, ...}
book/translations/PROFILE/{identity.json, translation-store.json, tasks/, ingest/, ...}
```

CLI identity overrides (`--model`, `--actor`, `--harness`) are honored over any
legacy `.booktx/identity.json`. Migration is staged: mutable files move
first, then the final profile config/identity/state are written, and the
legacy `config.toml` is removed only after all moves succeed.

## Failure modes

- **`multiple_profiles_ambiguous`**: more than one profile exists and no
  `--profile` was given for a target-state command. Pass `--profile`.
- **`task_profile_mismatch`**: a submission's profile header does not match
  the selected profile. Re-request the task in the correct profile.
- **`submission_profile_mismatch`**: a JSON submission's `profile` field
  differs from the target profile. Fix the submission or switch profile.
- **`legacy_project_required`**: the project still uses the legacy layout.
  Run `booktx profile migrate-current` first.
- **`migration_target_exists`**: the destination profile directory already
  exists and is non-empty. Remove it or pick a new profile name.
