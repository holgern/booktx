---
name: booktx
description: Use this skill when working in a booktx project or when the user asks to translate, validate, build, migrate, or inspect booktx state.
---

# booktx Skill

## Purpose

Use `booktx` to prepare Markdown or EPUB books for translation by an agent or
human translator. `booktx` does not translate by itself. It extracts source
records, creates durable translation tasks, accepts submissions, validates
translation state, and rebuilds output.

## Core invariants

```text
Profile = hard isolation boundary
Version = history/candidate boundary inside that profile
```

Shared source state lives under `.booktx/`.
Mutable translation state lives under `translations/<profile>/`.

## First commands in any existing project

Run:

```bash
booktx status .
booktx profile list .
```

Then determine the target profile.

Resolution rules:

1. Explicit `--profile PROFILE` wins.
2. Otherwise `.booktx/profile-state.json` active profile is used.
3. Otherwise exactly one existing profile may be auto-resolved.
4. If multiple profiles exist, always pass `--profile`.

Never mix ingest files, context, stores, ledgers, translated exports, reports,
or output between profiles.

## Source setup

For a new source-only project:

```bash
booktx init ./book --source-file book.epub --source-lang en
booktx extract ./book
```

Create a profile before translating:

```bash
booktx profile create ./book PROFILE \
  --target de \
  --target-locale de-DE \
  --model MODEL_LABEL \
  --select
```

Use a new profile for each target language, model experiment, or hard-isolated
context experiment.

## Context gate

Before requesting translation work:

```bash
booktx context status . --profile PROFILE
```

Read:

```text
translations/PROFILE/context.md
```

Stop before translating if `context.json` is missing or not ready. Initialize or
update context with `booktx context ...` commands, not by directly editing
`context.json`.

Typical context initialization:

```bash
booktx context init . --profile PROFILE --non-interactive
booktx context questions . --profile PROFILE
booktx context answer . --profile PROFILE Q001 --text "..."
booktx context render . --profile PROFILE --write
booktx context mark-ready . --profile PROFILE
```

## Translation workflow

Request a durable block task:

```bash
booktx translate next . --profile PROFILE --unit batch --max-words 800 --format block
```

This creates:

```text
translations/PROFILE/tasks/TASK.json
translations/PROFILE/tasks/TASK.source.block.txt
translations/PROFILE/ingest/TASK.block.txt
translations/PROFILE/ingest/TASK.json
```

Translate by editing only the generated ingest file:

```text
translations/PROFILE/ingest/TASK.block.txt
```

In block files:

- Keep every `>>> RECORD_ID` header unchanged.
- Write only the target translation under each header.
- Preserve required placeholder tokens exactly.
- Do not translate protected names unless context explicitly allows it.
- Do not add commentary outside target text.
- Do not edit `tasks/TASK.source.block.txt` as the submission.

Submit:

```bash
booktx translate insert . \
  --profile PROFILE \
  --task-id TASK \
  --file translations/PROFILE/ingest/TASK.block.txt \
  --format block
```

If insertion reports a stale task version, request a fresh task with
`booktx translate next` for the same profile. Do not force old task files into
the current profile/version.

## Validate and build

After accepting translations:

```bash
booktx validate . --profile PROFILE
booktx build . --profile PROFILE
```

Output is written under:

```text
translations/PROFILE/output/
```

For a complete final build, use the project option that requires complete
translations if available in the current CLI help.

## Versions

Versions are profile-local. Two profiles may both contain version `1.1`; these
are unrelated.

Use:

```bash
booktx version current . --profile PROFILE
booktx version list . --profile PROFILE
booktx translation compare . --profile PROFILE RECORD --versions 1.1,1.2
booktx translation activate . --profile PROFILE RECORD 1.2
```

A model/actor/harness change creates or selects a major track. A context change
creates or selects a subversion inside the track.

## Guardrails

Never edit these directly:

```text
.booktx/chunks/*.json
translations/PROFILE/translation-store.json
translations/PROFILE/translation-version-ledger.json
translations/PROFILE/translated/*.json
```

Use commands instead:

```bash
booktx translate insert ...
booktx translation activate ...
booktx translate export ...
booktx validate ...
```

## Bounded multi-chapter runs

If the user asks to continue for multiple chapters, do not request one huge
chapter task. Create a todo:

```bash
booktx translate todo-next . --profile PROFILE --chapters 3 --batch-words 800 --write
```

Read the generated todo markdown and follow its loop. Stop only when the todo
goal is complete or a stop condition occurs. Report partial progress if context
budget runs low.

The todo files are run-control artifacts under `translations/<profile>/todos/`.
They are NOT translation submissions.
Do not use old profile-state paths in a profile project:

```text
.booktx/context.json
.booktx/context.md
.booktx/tasks/
.booktx/ingest/
.booktx/translated/
.booktx/translation-store.json
```

## Migration

For a legacy single-layout project:

```bash
booktx profile migrate-current ./book PROFILE --select
```

After migration, use only `translations/PROFILE/...` for translation work.
Run `booktx status ./book --profile PROFILE` before continuing.
