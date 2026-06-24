# Commands

## Source-first setup

```bash
booktx init ./book --source-file book.epub --source-lang en
booktx extract ./book
```

Legacy one-step initialization still works:

```bash
booktx init ./book --target de --source-file book.epub --source-lang en
```

That creates and selects a default profile such as `de_default`.

## Profile commands

```bash
booktx profile create ./book de_gpt5_5 --target de --target-locale de-DE --model codex-openai/gpt-5.5@low --select
booktx profile list ./book
booktx profile show ./book de_gpt5_5
booktx profile select ./book de_gpt5_5
booktx profile compare ./book --profiles de_gpt5_5,de_glm_5_2 --record 0001-000001
booktx profile migrate-current ./book de_gpt5_5 --select
booktx profile create-pass-through ./book passthrough_en --select
```

## Context commands

All context files are profile-local:

```bash
booktx context init ./book --profile de_gpt5_5 --non-interactive
booktx context questions ./book --profile de_gpt5_5
booktx context answer ./book --profile de_gpt5_5 Q001 --text de-DE
booktx context mark-ready ./book --profile de_gpt5_5
booktx context render ./book --profile de_gpt5_5 --write
booktx context chapter-note ./book --profile de_gpt5_5 0010 --decision "Keep title literal"
```

## Status and identity

```bash
booktx status ./book
booktx status ./book --profile de_gpt5_5
booktx whoami ./book --profile de_gpt5_5
booktx actor whoami ./book --profile de_gpt5_5
booktx harness whoami ./book --profile de_gpt5_5
booktx model whoami ./book --profile de_gpt5_5
```

When multiple profiles exist and none is active, target-dependent commands fail
until you pass `--profile` or select one.

## Translation workflow

```bash
booktx translate next ./book --profile de_gpt5_5 --unit batch --max-words 800 --format block
booktx translate insert ./book --profile de_gpt5_5 --task-id TASK --file translations/de_gpt5_5/ingest/TASK.block.txt --format block
booktx translate task-status ./book --profile de_gpt5_5 --task-id TASK
booktx translate set-record ./book --profile de_gpt5_5 --task-id TASK --record-id RECORD_ID --stdin
booktx translation get-record ./book --profile de_gpt5_5 74@38 --before 2 --after 2
booktx translation list ./book --profile de_gpt5_5 --chapter 10
booktx translation compare ./book --profile de_gpt5_5 74@38 --versions 1.1,1.2
booktx translation activate ./book --profile de_gpt5_5 74@38 1.2
booktx translation review ./book --profile de_gpt5_5 74@38 --activate 1.2 --note "Better rhythm"
booktx translate export ./book --profile de_gpt5_5
```

## Bounded agent runs

```bash
booktx translate todo-next ./book --profile de_gpt5_5 --chapters 3 --batch-words 800 --write
booktx translate todo-next ./book --profile de_gpt5_5 --chapters 3 --batch-words 800 --max-run-words 12000 --write --json
booktx translate todo-status ./book --profile de_gpt5_5 --latest
booktx translate todo-status ./book --profile de_gpt5_5 --todo-id TODO --json
booktx translate todo-resume ./book --profile de_gpt5_5 --latest --format block
booktx translate todo-resume ./book --profile de_gpt5_5 --todo-id TODO --format block
booktx translate todo-next ./book --profile de_gpt5_5 --chapters 5 --batch-words 800 --skip-current --write
booktx translate todo-next ./book --profile de_gpt5_5 --chapters 3 --start-chapter 0017 --batch-words 800 --write
```

Creates a durable todo under `translations/<profile>/todos/` that describes the
bounded run: chapters to complete, per-task word budget, advisory run budget,
and stop conditions. The agent reads the todo markdown and follows
`todo-status -> todo-resume -> insert -> validate --fail-on-warnings` until
complete or a stop condition fires. This is NOT a translation submission; the
agent still fills ingest files and runs `translate insert` for each batch.
`--max-run-words` is advisory only.

## Version commands

Versions are profile-local:

```bash
booktx version current ./book --profile de_gpt5_5
booktx version list ./book --profile de_gpt5_5
booktx version show ./book --profile de_gpt5_5 1.2
booktx version select ./book --profile de_gpt5_5 1.2
booktx version set-label ./book --profile de_gpt5_5 1 "GPT 5.5"
booktx version fork-context ./book --profile de_gpt5_5 --note "Manual context split"
```

`version list` now reports baseline-scoped subversions. Routine chapter-note
appends keep the same dotted version; baseline policy changes create or select
the next subversion. `translate next` task output also includes baseline and
context-view metadata for the immutable task snapshot it created.

## Validate and build

```bash
booktx validate ./book --profile de_gpt5_5
booktx validate ./book --profile de_gpt5_5 --fail-on-warnings
booktx build ./book --profile de_gpt5_5
booktx build ./book --profile de_gpt5_5 --require-complete
```

`--fail-on-warnings` keeps default validate behavior unchanged unless you opt
into warning-fatal automation.

Outputs land under:

```text
translations/<profile>/reports/
translations/<profile>/output/
```

## Pass-through validation

`booktx pass-through` generates source-as-target translated chunks from the
extracted source chunks, validates complete coverage, and rebuilds the output.
It is a reconstruction fixture, not a translation:

```bash
booktx pass-through ./book --profile passthrough_en --create
booktx pass-through ./book --profile passthrough_en --no-build
```

`--profile` is always required. Use `--clear-store` only when reusing a
pass-through profile that has stray store records. Compare the rebuilt output
against the source with an EPUB diff viewer.

## JSON output for machine consumers

Most read commands accept `--json`. Examples:

```bash
booktx profile list ./book --json
booktx profile show ./book de_gpt5_5 --json
booktx whoami ./book --profile de_gpt5_5 --json
booktx status ./book --profile de_gpt5_5 --json
booktx version show ./book --profile de_gpt5_5 1.2 --json
```

`profile list`/`profile show`/`whoami` report the live identity from
`translations/<profile>/identity.json`, so they stay consistent after
`booktx model set`, `actor set`, or `harness set`.
