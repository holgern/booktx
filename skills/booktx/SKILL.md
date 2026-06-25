---
name: booktx
description: Use this skill when working in a booktx project or when the user asks to translate, validate, build, migrate, or inspect booktx state.
---

# booktx

Context answers are user policy, not agent policy. Treat all agent-proposed answers as drafts until the user approves them. Skill

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

## Access modes

### Collaborative project-root mode

Start at the book project root when you need profile administration,
cross-profile comparison, migration, or debugging:

```bash
booktx status .
booktx profile list .
```

### Isolated profile-root mode

For unbiased model or context evaluation, start inside `translations/<profile>/`
and use only profile-local booktx commands with project argument `"."`.

Never use parent paths, absolute paths, shell globs, interpreter snippets, or
sibling profile commands. Never inspect sibling profiles. If a command suggests
a parent path or prints a sibling profile, stop and report a booktx isolation
bug.

Use:

```bash
booktx mode .
booktx doctor isolation .
booktx source status .
booktx context status .
booktx translate next . --unit batch --max-words 800 --format block
booktx translate insert . --task-id TASK --file ingest/TASK.block.txt --format block
booktx validate .
booktx build .
```

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

Do not hand-edit `context.md` for chapter notes during normal operation. Use
`booktx context chapter-note`. If `context.md` already has manual notes, run
`booktx context import-md . --profile PROFILE --write` before validating.

Typical context initialization:

```bash
booktx context init . --profile PROFILE --non-interactive
booktx context questions . --profile PROFILE
booktx context render . --profile PROFILE --stdout
# Show recommendations to the user and wait for explicit approval or edits.
booktx context approve . --profile PROFILE Q001 --text "<USER_APPROVED_TEXT>" --approved-by "user:<USER>"
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

In isolated profile-root mode, omit `--profile` and use profile-local paths such
as `ingest/TASK.block.txt`.

When a task file exists, its recorded `translation_version` and context-view
snapshot are authoritative for submission. A live baseline change alone should
not make that task stale. Request a fresh task only when the existing task is
missing, points at the wrong profile, or you intentionally want a new task
under the updated baseline/context view.

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

For a complete final build:

```bash
booktx validate . --profile PROFILE --fail-on-warnings
booktx build . --profile PROFILE --require-complete
```

## Pass-through reconstruction check

When the user asks to verify that EPUB reconstruction includes all content,
create or refresh a pass-through profile instead of translating manually:

```bash
booktx pass-through . --profile passthrough_en --create
```

Never run pass-through against a real translation profile. Pass-through writes
generated source-as-target chunks under `translations/<profile>/translated/`.

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

A model/actor/harness change creates or selects a major track. A baseline
policy change creates or selects a subversion inside the track. A routine
chapter-note append updates the next task's effective context view but does not
create a new dotted version on its own.

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
booktx check . --chapter CHAPTER --fail-on-warnings
booktx validate . --profile PROFILE
```

## Bounded multi-chapter runs

If the user asks to continue for multiple chapters, do not request one huge
chapter task. Create a todo:

```bash
booktx translate todo-next . --profile PROFILE --chapters 3 --batch-words 800 --write
booktx translate todo-status . --profile PROFILE --latest
booktx translate todo-resume . --profile PROFILE --latest --format block
```

## Single large chapters

If a user asks to complete a chapter and that chapter has more than the safe
task budget, do not use a giant `translate next --unit chapter` task. Let
booktx create/reuse a single-chapter todo, or create it explicitly:

```bash
booktx translate todo-next . --profile PROFILE --start-chapter CHAPTER --chapters 1 --batch-words 800 --write
booktx translate todo-resume . --profile PROFILE --latest --format block
```

Only force a giant chapter task with `--force-chapter` when explicitly requested.

After each completed chapter, always run `booktx check` before adding the
chapter note:

```bash
booktx check . --profile PROFILE --chapter CHAPTER --fail-on-warnings
```

Read the generated todo markdown and follow its loop. Stop only when the todo
Do not use old profile-state paths in a profile project:

```text
.booktx/context.json
.booktx/context.md
.booktx/tasks/
.booktx/ingest/
.booktx/translated/
.booktx/translation-store.json
```

If a `todo-status`, `todo-resume`, or `todo-next` command fails with an internal
booktx error, stop and report the tool failure. Do not silently switch to a
large unbounded `translate next --unit chapter` task. Bounded todos exist to
keep agent runs within budget; bypassing them defeats that purpose. Only use
`translate next --unit chapter` for small chapters or when the user explicitly
requests a whole-chapter task.

## Migration

For a legacy single-layout project:

```bash
booktx profile migrate-current ./book PROFILE --select
```

After migration, use only `translations/PROFILE/...` for translation work.
Run `booktx status ./book --profile PROFILE` before continuing.

## Context gate

Before translation, context must be approved by the user. Never answer initial context questions from your own judgment. You may recommend answers, but you must show the questions and recommendations to the user and wait for explicit approval or edited answers before writing them with `booktx context approve`. Do not run `booktx context approve`, `booktx context answer`, `booktx context render --write`, or `booktx context mark-ready` until the user has replied with approval or custom answers. Do not use `booktx context mark-ready --force` during normal translation work.

Recommended prompt: I reviewed the source and recommend the following context answers. Please approve all, edit specific answers, or provide your own text.

## EPUB inline XHTML records

For EPUB records, the source may contain inline XHTML fragments such as `<em>`, `<strong>`, `<span class="...">`, `<a href="...">`, `<sup>`, `<sub>`, or `<code>`. Translate only the human-readable text nodes. Preserve the inline tags and attributes around the equivalent target-language phrase. Do not replace XHTML with Markdown markers. Do not invent new tags or attributes. Keep opaque tags such as `<code>...</code>` unchanged.

## Quality review workflow

Quality review improves already-accepted translations without overwriting the
first-pass output:

1. Enable quality review in the profile `config.toml` (see `docs/profiles.md`)
2. `booktx review status .` -- check review coverage per pass
3. `booktx review next . --pass 1` -- create a review task for pass 1
4. Edit the ingest block under `translations/<profile>/reviews/`
5. `booktx review insert . --review-task-id TASK --file reviews/TASK.block.txt --format block`
6. Repeat for pass 2 if configured
7. `booktx validate . --fail-on-warnings` (# both passes
8. `booktx build . --require-complete --require-reviewed`

During review, do not retranslate freely. Review the existing target and improve
only where quality can be meaningfully raised. If the target is already good,
submit it unchanged so booktx records an explicit review candidate.

Review candidates are stored in `reviews[]` under each record in the translation
store. The effective output uses the `active_review` when valid, falling back
to the `active_version`. Use `booktx translation compare . RECORD --versions 1.1,R1.1,R2.1`
to inspect the full chain.
