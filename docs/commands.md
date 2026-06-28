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
booktx context recommend ./book --profile de_gpt5_5 Q001 --text de-DE --reason "profile target locale"
booktx context questionnaire ./book --profile de_gpt5_5 --stdout
# Stop for user approval, then record the approved answer.
booktx context approve ./book --profile de_gpt5_5 Q001 --text de-DE --approved-by "user:<USER>"
booktx context mark-ready ./book --profile de_gpt5_5
booktx context render ./book --profile de_gpt5_5 --write
booktx context chapter-note ./book --profile de_gpt5_5 0010 --decision "Keep title literal"
```

## Chapter detection and audit

```bash
booktx chapters ./book                       # detect, persist, and list chapter ranges
booktx chapters ./book --audit               # audit EPUB TOC vs. extracted spans and map
booktx chapters ./book --audit --json        # machine-readable audit output
```

`booktx chapters` refreshes `.booktx/chapter-map.json` and lists each chapter's
chunk and record range. `--audit` is EPUB-only and read-only: it compares the
visible contents page against extracted spans, navigation, and the chapter
map, then writes `.booktx/reports/chapter-audit.json`. EPUB `booktx extract`
already generates both files and prints a warning when findings exist; run
`--audit` for details. `booktx status` recomputes the audit summary, and new
work selection blocks only on `error` findings (warning-only findings stay
non-blocking).

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
booktx translation revise-record ./book --profile de_gpt5_5 74@38 --target "Revised target text"
booktx translation revise-block ./book --profile de_gpt5_5 --file ingest/fixes.block.txt --format block --activate
booktx translate export ./book --profile de_gpt5_5
booktx translate export-index ./book --profile de_gpt5_5
booktx translate export-index ./book --profile de_gpt5_5 --kind source
booktx translate export-index ./book --profile de_gpt5_5 --kind target
booktx translate export-index ./book --profile de_gpt5_5 --kind source-target
booktx translate export-index ./book --profile de_gpt5_5 --json
booktx translate export-index ./book --profile de_gpt5_5 --fail-on-warn
```

`translate export` writes store-backed accepted translations as legacy-compatible chunk files under `translated/`.

`translate export-index` writes three generated editor QA indexes under `translations/<profile>/`: `source-index.json` (source text only), `target-index.json` (target text only), and `source-target-index.json` (slim side-by-side view). Use `--kind source`, `--kind target`, or `--kind source-target` (repeatable) to write only selected kinds. `--fail-on-warn` blocks target-based indexes on warnings. `--json` prints the summary as JSON. All three files are generated artifacts safe to delete and regenerate. They never contain canonical state and must not be used as build input.

Profile-root mode works without `--profile`:

```bash
cd translations/de_default
booktx translate export-index .
rg "Wespen" target-index.json
nvim source-target-index.json
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
`todo-status -> todo-resume -> insert -> check --chapter CHAPTER` until
complete or a stop condition fires. Use `booktx validate --fail-on-warnings`
for the final pre-build check only. This is NOT a translation submission; the
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
booktx validate ./book --profile de_gpt5_5 --chapter 0005
booktx validate ./book --profile de_gpt5_5 --task-id TASK_ID
booktx validate ./book --profile de_gpt5_5 --json
booktx build ./book --profile de_gpt5_5
booktx build ./book --profile de_gpt5_5 --require-complete
```

`--chapter` and `--task-id` scope validation to a specific chapter or task.
Use `--json` for machine-readable output.

`--fail-on-warnings` keeps default validate behavior unchanged unless you opt
into warning-fatal automation.

## `check` -- scoped build-preflight validation

```bash
booktx check ./book --profile de_gpt5_5 --chapter 0005 --fail-on-warnings
booktx check ./book --profile de_gpt5_5 --task-id TASK_ID --json
```

`check` is a human-friendly alias for scoped validation + EPUB inline-XHTML
preflight. It defaults to `--fail-on-warnings`. Use it after each chapter
translation and before build.

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

## Context question lifecycle

Questions start as `open`. Agents may store draft defaults with `context recommend`, which sets `recommended` but does not answer the question or change style policy. User-approved decisions are recorded with `context approve`, which stores `answer_source=user`, approval metadata, and applies style updates. Required dynamic questions can be added with `context add-question --required` after source review. Use `context questionnaire --stdout` to show a user-facing approval form. `context mark-ready --force --reason ...` is only for emergency or migration cases.

## Review commands (`booktx review`)

- `booktx review configure . --show` -- show current quality review config
- `booktx review configure . --enable --pass 1 --name "Flow review" --mode manual --enforce warn` -- enable review with one pass (see `docs/profiles.md` for all flags)
- `booktx review configure . --disable` -- disable quality review entirely
- `booktx review status .` -- report review coverage by pass (eligible/reviewed/missing/stale/blocked); JSON includes `next_command`, `first_missing_record`, `first_missing_chapter`
- `booktx review next . --pass 1` -- create the next durable review task for a pass; supports `--selection missing|stale|reviewed|all|changed-base` and `--base active_translation|active_review|pass:N`
- `booktx review next . --pass 1 --selection reviewed --base active_review` -- rerun a pass over already-reviewed records, creating `R1.2` from `R1.1`
- `booktx review insert . --review-task-id TASK --file reviews/TASK.block.txt --format block` -- parse and accept a review submission
- `booktx review activate . RECORD R1.2` -- manually activate an existing review candidate for a record
- `booktx review deactivate . RECORD` -- deactivate the active review, falling back to the active translation version
- `booktx review revise-record . RECORD --base-review R1.2 --stdin` -- revise an accepted review candidate by creating a new same-pass rerun

Enable quality review via CLI (preferred) or TOML:

````bash
booktx review configure . --enable --pass 1 --name "Flow review" --mode manual --enforce warn
## Glossary repair and chapter note reset

```bash
# Replace forbidden targets (full replacement, not append).
booktx context add-term . "empire" --target "Imperium" --forbid "Reich" --forbid "Empire"

# Append forbidden targets explicitly.
booktx context add-term . "empire" --append-forbid "Kaiserreich"

# Clear all forbidden targets.
booktx context add-term . "empire" --clear-forbidden

# Remove a wrong glossary entry.
booktx context remove-term . "empire"
booktx context remove-term . "empire" --missing-ok

# Reset one entry atomically.
booktx context reset-term . "empire" \
  --target "Imperium" \
  --forbid "Reich" --forbid "Empire" \
  --category "concept" --enforce error

# Replace an entire chapter note.
booktx context chapter-note . 0006 \
  --replace-all \
  --title "TWO" \
  --source-summary "..." \
  --translation-summary "..." \
  --decision "Keep Apt" \
  --open-issue "Check title rendering"
````
