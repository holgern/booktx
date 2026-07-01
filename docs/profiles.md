# Profiles

`booktx` translation profiles let one source book support multiple translation
efforts without mixing mutable state.

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

## Access modes

Profiles own mutable translation state, but **visibility of sibling profiles**
depends on how the harness starts:

### Collaborative project-root mode

Start the harness at the book project root when you need:

- profile administration
- profile comparison
- cross-profile reference work
- migration and debugging

In this mode, project-relative paths and explicit cross-profile commands are
expected and allowed.

### Isolated profile-root mode

Start the harness inside `translations/<profile>/` when you want unbiased model
or context evaluation for one target profile.

This is **booktx-mediated isolation**, not OS sandboxing. It assumes:

- the harness blocks parent paths, absolute paths, sibling profile paths, shell
  globs, and arbitrary filesystem inspection snippets;
- the agent uses only profile-local `booktx ... .` commands;
- `booktx` itself never requires or prints parent/sibling paths for the normal
  isolated workflow.

Use:

```bash
booktx mode .
booktx doctor isolation .
booktx source status .
booktx profile list .          # shows current profile only, no sibling names
booktx profile show . .         # defaults to current profile
booktx context status .
booktx translate next . --unit batch --max-words 800 --format block
booktx translate insert . --task-id TASK --file ingest/TASK.block.txt --format block
booktx validate .
booktx build .
```

`profile list` in isolated mode shows only the current profile (no sibling profile names, no absolute paths, no `../`). Cross-profile commands like `profile compare`, `profile select`, `profile create`, and `profile migrate-current` remain blocked.

If a command in profile-root mode suggests `../`, prints an absolute path, or
reveals another profile, stop and report a booktx isolation bug.

Before starting the harness inside a profile, prepare the matching harness
instructions so the agent does not have to rediscover them:

```bash
booktx agents write . --mode isolated --profile PROFILE
```

This writes a profile-local `AGENTS.md` (safe to read from inside the profile
root: no parent paths, absolute paths, sibling profile names, or `--profile`)
and removes project-root/collaborative generated instructions. From inside the
profile root, `booktx agents write . --mode isolated` refreshes the local file
without printing parent paths. `booktx agents status .` reports ownership and
staleness for the local file only.

## What is isolated?

Each profile owns its own copy of all mutable translation state under
`translations/<profile>/`:

| Path                              | Meaning                               |
| --------------------------------- | ------------------------------------- |
| `.booktx-profile.json`            | Profile-root runtime marker           |
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

Profile-root isolated mode reads that shared source state only through booktx's
brokered commands such as `booktx source ...` and `booktx translate next .`.

Translation context is **not** shared across profiles. To keep style,
global rules, glossary, and approved question answers consistent across
several books in the same series, export a series context pack from one
approved profile and import it into another with `booktx context export-pack`
and `booktx context import-pack`. The pack carries reusable policy only; it
never carries records, tasks, stores, ledgers, identity, or chapter contexts.

For sibling profiles inside the **same** book project, use `booktx context sync`
from project-root collaborative mode instead of repeatedly exporting and
re-importing pack files. Sync still copies policy into each target profile's
own context files; it does not make context shared.

## When to create a new profile?

Create a new profile whenever you want a hard isolation boundary:

- **Different target language**: `de_gpt5_5`, `fr_gpt5_5`, `es_gpt5_5`.
- **Different model experiment**: `de_gpt5_5` vs `de_glm_5_2` for the same
  language, so the two outputs never contaminate each other.
- **Different context decisions**: a re-translation under revised glossary or
  style rules, kept separate from a previous accepted run.

Do **not** create a new profile for a routine re-translation of the same
language/model/context; that is a _version_, not a profile.

## Pass-through profiles

A pass-through profile is a **generated validation fixture**, not a translation.
Its target language equals the source language, and every translated record's
target is set to the source text. Use it to verify that extraction and EPUB
reconstruction include all content before involving a translator.

- Pass-through profiles are generated fixtures; they must not be used for
  human or LLM translation.
- They use the source language as the target language.
- They are isolated under `translations/<profile>/`, just like any profile, so
  they cannot contaminate real translation profiles.
- `booktx pass-through` requires an explicit `--profile` and refuses to run
  against a profile whose `kind` is not `pass-through`.

A non-empty translation store can silently override generated chunks, so
pass-through refuses a profile with store records unless you pass
`--clear-store` (which rewrites only `translation-store.json`).

## Selection profiles

A selection profile is a normal buildable profile whose accepted output is
assembled from cross-profile judge decisions.

- `kind = "selection"` in `translations/<profile>/config.toml`
- it keeps its own `context.json`, `translation-store.json`, and output files
- accepted judge output is written into the normal translation store so
  `booktx validate` and `booktx build` work without special build rules
- provenance is stored separately in
  `translation-selection-ledger.json`
- durable judge task artifacts live under `judge-tasks/` and `judge-ingest/`

Create one with:

```bash
booktx judge create-profile ./book de_judge_gpt5_5 \
  --target de \
  --target-locale de-DE \
  --sources de_gpt5_5,de_glm_5_2 \
  --model gpt-5.5 \
  --select
```

Before judging, initialize the selection profile context, sync policy from a
compatible source profile (or otherwise configure the same policy), and mark it
ready:

```bash
booktx context init ./book --profile de_judge_gpt5_5 --non-interactive
booktx context sync ./book \
  --from de_gpt5_5 \
  --to de_judge_gpt5_5 \
  --section glossary \
  --section style \
  --section global-rules \
  --write
booktx context mark-ready ./book --profile de_judge_gpt5_5
```

Judge workflows are cross-profile and therefore project-root only:

```bash
booktx judge status ./book --profile de_judge_gpt5_5 --sources de_gpt5_5,de_glm_5_2
booktx judge next ./book --profile de_judge_gpt5_5 --sources de_gpt5_5,de_glm_5_2 --unit chapter --chapter 0001
booktx judge insert ./book --profile de_judge_gpt5_5 --judge-task-id TASK --file translations/de_judge_gpt5_5/judge-ingest/TASK.block.txt --format block
```

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
- **`profile_root_marker_missing`**: the profile-root marker is missing. Recreate
  or backfill the profile marker before using isolated mode.
- **`profile_root_marker_mismatch`**: the marker no longer matches the profile
  directory, project root, or profile config. Regenerate or repair the marker.
- **`stale_profile_root_marker`**: the marker is bound to an older extracted
  source identity. Refresh the marker after source extraction changes.
- **`task_profile_mismatch`**: a submission's profile header does not match
  the selected profile. Re-request the task in the correct profile.
- **`submission_profile_mismatch`**: a JSON submission's `profile` field
  differs from the target profile. Fix the submission or switch profile.
- **`legacy_project_required`**: the project still uses the legacy layout.
  Run `booktx profile migrate-current` first.
- **`migration_target_exists`**: the destination profile directory already
  exists and is non-empty. Remove it or pick a new profile name.

## Quality review configuration

Add or update quality review through the CLI (preferred) or by editing
the profile `config.toml` directly:

```bash
# Show current config
booktx review configure . --show

# Enable with one pass
booktx review configure . --enable --pass 1 --name "Flow review" \
  --mode manual --enforce warn --base active_translation \
  --before 2 --after 2 --batch-words 900 \
  --instructions "Improve reading flow and pronoun continuity."

# Add a second pass
booktx review configure . --enable --pass 2 --name "Final polish" \
  --base active_review --required-base-pass 1 --enforce error \
  --instructions "Polish final prose. Prefer minimal edits."

# Disable quality review entirely
booktx review configure . --disable
```

Manual TOML equivalent (kept for reference):
Add a `[quality_review]` table to the profile `config.toml`:

```toml
[quality_review]
enabled = true
active_passes = [1]
require_all_active_passes = true

[[quality_review.passes]]
pass_number = 1
name = "Flow review"
enabled = true
mode = "after_chapter"
enforce = "warn"
base = "active_translation"
before_records = 2
after_records = 2
batch_words = 900
instructions = "Improve reading flow and pronoun continuity."
```

Two-pass example:

```toml
[quality_review]
enabled = true
active_passes = [1, 2]

[[quality_review.passes]]
pass_number = 1
name = "Flow review"
base = "active_translation"
enforce = "warn"

[[quality_review.passes]]
pass_number = 2
name = "Final polish"
base = "active_review"
required_base_pass = 1
enforce = "error"
instructions = "Polish final prose. Prefer minimal edits."
```

Fields:

- `enabled` -- enable or disable quality review for this profile
- `active_passes` -- which passes are currently active (reported by `review status`)
- `require_all_active_passes` -- when true, validation reports missing active passes

Per-pass fields:

- `pass_number` -- unique pass identifier (1, 2, ...)
- `name` -- human-readable label
- `enabled` -- enable or disable this specific pass
- `mode` -- `manual`, `after_chapter`, or `before_build`
- `enforce` -- `off` (no findings), `warn` (warning), `error` (blocking)
- `base` -- `active_translation` (first-pass version) or `active_review` (prior review)
- `required_base_pass` -- pass that must be completed first (for chaining)
- `before_records` / `after_records` -- neighbor context window size
- `batch_words` -- maximum source words per review task
- `instructions` -- prompt for the reviewing agent

Pass-through profiles must not set `[quality_review]`.
