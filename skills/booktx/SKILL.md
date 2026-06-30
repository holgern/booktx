---
name: booktx
description: Use this skill when working in a booktx project or when the user asks to translate, validate, build, migrate, or inspect booktx state.
---

# booktx

Context answers are user policy, not agent policy. Treat all agent-proposed answers as drafts until the user approves them.

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
booktx profile list .          # shows current profile only
booktx profile show . .         # defaults to current profile
booktx context status .
booktx translate next . --unit batch --max-words 800 --format block
booktx translate insert . --task-id TASK --file ingest/TASK.block.txt --format block
booktx translate todo-next . --chapters 3 --batch-words 800 --write
booktx translate todo-status . --todo-id TODO
booktx translate todo-resume . --todo-id TODO --format block
booktx validate .
booktx build .
```

`profile list` in profile-root mode shows only the current profile to avoid dead-ending the user; it never prints sibling profile names, absolute paths, or `../`. Cross-profile commands (`profile compare`, `profile select`, `profile create`, `profile migrate-current`) remain blocked in isolated mode.

The todo commands (`todo-next`, `todo-status`, `todo-resume`) are runtime-aware: in
profile-root mode they omit `--profile`, use profile-local paths (`todos/`,
`ingest/`, `tasks/`, `context.md`), and never print `translations/`, absolute
project paths, or `../`. Their generated todo markdown and block ingest templates
follow the same rule. A `translate insert` EPUB inline-XHTML preflight staging
failure is reported as a compact `error:`/`hint:` message, never a raw Pydantic
traceback.

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

### EPUB chapter completeness check

Before translating an EPUB, confirm the detected chapter map matches the
visible contents page. EPUB chapter detection uses upstream `epub2text` block
chapter annotations (`chapter_mapping="epub2text-block-v1"`) as the
authoritative source for new extractions; old manifests fall back to a
conservative navigation mapper. A truncated/preview EPUB or a partial
navigation document can make the map end early (for example at `TEN` while
the TOC lists `ONE` through `TWENTY-SIX`), which silently skips chapters.
`booktx extract` already writes the chapter map and audit and warns on
findings; `booktx status` recomputes the audit and shows it.

```bash
booktx chapters . --audit
```

Stop and resolve the source if the audit reports `epub_toc_chapter_missing_from_map`,
`epub_toc_href_extracted_but_unmapped` (a blocking `error` that prevents new
chapter/task/todo selection until resolved), or
`epub_toc_href_missing_from_extracted_spans`. Do not synthesize empty
chapters from missing TOC targets; re-extract from a complete source instead.
Existing projects must re-run `booktx extract` to gain upstream block
annotations.

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

Context is profile-local; never symlink or hand-share `context.json` across
books. For the same series across books, export reusable policy (style, global
rules, glossary, approved answers) from an approved profile and import it into
the next book's profile:

```bash
booktx context export-pack ./book1 --profile PROFILE \
  --series-id SERIES --output ./series.booktx-context-pack.json
booktx context import-pack ./book2 --profile PROFILE \
  --file ./series.booktx-context-pack.json            # dry run
booktx context import-pack ./book2 --profile PROFILE \
  --file ./series.booktx-context-pack.json --write     # commit
```

Import is a dry run by default; `--write` commits. It never touches records,
tasks, stores, ledgers, identity, or source state. Changed policy clears
readiness, so re-run `booktx context mark-ready` after approval. A task
created before a binding glossary import is rejected as stale; create a fresh
task to use the imported policy.

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
- If a source record is fully enclosed in dialogue quotes, preserve a complete enclosing quote pair in the target. German targets may use `„...“` or `»...«`; do not leave an opening `„` without the closing `“`.
- Do not translate protected names unless context explicitly allows it.
- Do not add commentary outside target text.
- Do not edit `tasks/TASK.source.block.txt` as the submission.

The block template header records the task chapter and the chunk ids its records
belong to (for example `# chapter: 0002 Contents` and `# record_chunks: 0001`).
Record ids are chunk-based, so a task whose target is chapter `0002` can contain
record ids prefixed with `0001` when that chapter starts inside source chunk
`0001`. This is expected, not a bug.

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

### EPUB output-language and hyphenation

Translated EPUB builds resolve a target-language policy: the profile target
locale is written to the primary OPF `dc:language` and targeted XHTML
`lang`/`xml:lang`, and one deterministic best-effort hyphenation style sheet
is injected. This is metadata/author-style correctness, not a guarantee of
identical rendering; automatic hyphenation still depends on the reader and
its dictionaries.

Defaults: translation and legacy translation projects default to `target` +
`auto`; pass-through profiles default to `preserve`/`preserve` and stay
byte-identical. Override under `[epub_output]` in the profile config. The
compatibility escape hatch for bad reader-side breaks is:

```toml
[epub_output]
hyphenation = "none"
```

Build is transactional: a failed policy resolution, rebuild, or audit leaves
the last good output untouched. The build report adds an `epub_output_policy`
object. Audit an existing output without rebuilding with
`booktx check . --profile PROFILE --epub-output --json`.

Never promise identical hyphenation across readers; report source CSS
conflict warnings as best-effort signals, not guarantees.

## Editor QA indexes

After translation/review changes, run `booktx translate export-index` when the user wants editor-search artifacts current:

```bash
booktx translate export-index .
```

This writes `source-index.json`, `target-index.json`, and `source-target-index.json` into the profile directory.

- Use `source-index.json` for source-only term search or isolated profile source reading.
- Use `target-index.json` for target-only term search without English source false positives.
- Use `source-target-index.json` for quick translation-fit scans side by side.
- Never edit any of the three files manually.
- Never use any editor index as canonical source for build; the canonical state remains `translation-store.json`.

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
translations/PROFILE/source-index.json
translations/PROFILE/target-index.json
translations/PROFILE/source-target-index.json
```

Use commands instead:

```bash
booktx translate insert ...
booktx translation activate ...
booktx translate export ...
booktx translate export-index ...
booktx translation revise-record . RECORD_ID --target "..."
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

In isolated profile-root mode, drop `--profile` from all three and run them with
project argument `.` from inside `translations/<profile>/`; the written todo
markdown and resume hints then use local paths only.

Use scoped `booktx check . --chapter CHAPTER --fail-on-warnings` for per-batch
validation within a bounded todo. Use `booktx validate . --fail-on-warnings`
only for the final pre-build check. If validation flags an old accepted record,
use `booktx translation revise-record` to fix it; never edit
`translation-store.json` directly.

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

1. Enable quality review via CLI (no manual TOML edits):
   `booktx review configure . --enable --pass 1 --name "Flow review" --mode manual --enforce warn`
2. `booktx review configure . --show` -- inspect current config
3. `booktx review status .` -- check review coverage per pass; JSON output includes
   `next_command`, `first_missing_record`, and `first_missing_chapter`
4. `booktx review next . --pass 1` -- create a review task for pass 1
   Output includes readable source path, editable ingest path, submit command,
   per-chapter check, and status hints.
5. Edit the ingest block under `translations/<profile>/reviews/`
6. `booktx review insert . --review-task-id TASK --file reviews/TASK.block.txt --format block`
7. Repeat for pass 2 if configured: `booktx review next . --pass 2`
8. `booktx validate . --fail-on-warnings` # both passes
9. `booktx build . --require-complete --require-reviewed`
   submit it unchanged so booktx records an explicit review candidate.

Review candidates are stored in `reviews[]` under each record in the translation
store. The effective output uses the `active_review` when valid, falling back
to the `active_version`. Use `booktx translation compare . RECORD --versions 1.1,R1.1,R2.1`
to inspect the full chain.

### Review-first routing

When the user asks to review, polish, improve grammar, improve flow, or run a
second pass over existing translations, start with the review workflow, not
the canonical store:

1. Run `booktx review status .` first. Its JSON includes `next_command`,
   `first_missing_record`, and `first_missing_chapter`.
2. If quality review is disabled, enable it with `booktx review configure . --enable ...`.
   Do not hand-edit the profile `config.toml`.
3. Create review work with `booktx review next . --pass N`. Use
   `--selection reviewed --base active_review` to rerun a pass over records
   that already have an accepted review (this creates `R1.2` from `R1.1`, not a
   new translation version). Default selection is `missing`.
4. Submit improvements with `booktx review insert .`; accepted candidates
   activate by default. Use `--no-activate` only when you intentionally keep
   the current effective target.
5. To revise an already-accepted review candidate, use
   `booktx review revise-record . RECORD --base-review R1.2 --stdin`. This
   creates a new same-pass rerun (`R1.3` from `R1.2`) without mutating the
   existing candidate. Use `booktx review deactivate . RECORD` to fall back to
   the acti

### Deterministic fixes vs review passes

For deterministic mechanical corrections (forbidden glossary terms,
detected by script or `qa scan`), use the batch revision path, not the
review workflow:

```bash
booktx translate export-index . --jsonl
# audit current targets for forbidden terms, then:
booktx translation revise-block . --file ingest/glossary-fixes.block.txt --format block --activate
booktx validate . --fail-on-warnings
```

For literary flow, grammar, or style improvements, use the review workflow
(`review next` / `review insert`) so provenance stays separate from
translation versions.

### Termux-safe file policy

Never create or write files under `/tmp` during agent work. All generated
work files (block templates, index files, reports, editor artifacts) must
be profile-local under `translations/<profile>/` or its subdirectories.
The `reviews/`, `ingest/`, `tasks/`, and index files are already
profile-local by default.ve translation version. 6. For source/target term search, refresh and read the
`booktx translate export-index .` artifacts (or `--jsonl` for line-per-record
output). Do not grep `translation-store.json` for normal review work.
Generated current-only surfaces include `source-index.json[l]`,
`target-index.json[l]`, and `source-target-index.json[l]`. 7. Never write Python scripts to parse `translation-store.json` for normal
review or polish work. Treat `translation-store.json` as canonical history,
not a search interface. Reserve raw-store reads for debugging when a booktx
command is genuinely missing.

## Glossary correction workflow

Never edit `context.json` directly. Use CLI commands for all glossary changes:

```bash
# Replace all forbidden targets (full replacement, not append).
booktx context add-term . "empire" --target "Imperium" --forbid "Reich" --forbid "Empire"

# Append forbidden targets explicitly without removing existing ones.
booktx context add-term . "empire" --append-forbid "Kaiserreich"

# Clear all forbidden targets.
booktx context add-term . "empire" --clear-forbidden

# Remove a wrong glossary entry entirely.
booktx context remove-term . "empire"
booktx context remove-term . "empire" --missing-ok

# Replace one entry atomically (target, forbidden targets, category, notes, enforce).
booktx context reset-term . "empire" \
  --target "Imperium" \
  --forbid "Reich" --forbid "Empire" \
  --category "concept" --enforce error

# Atomic reset of an entire chapter note.
booktx context chapter-note . 0006 \
  --replace-all \
  --title "TWO" \
  --source-summary "..." \
  --translation-summary "..." \
  --decision "Keep Apt" \
  --open-issue "Check title rendering"
```

`--forbid` now replaces the full forbidden-target list. When the target changes, any forbidden term equal to the new target is pruned automatically. Use `--append-forbid` when you want to add terms without removing existing ones.

For **user terminology decisions** (e.g. \u201calways translate `tenday` as
`Dekade`\u201d), prefer `mandate-term` over `add-term`/`reset-term`:

```bash
booktx context mandate-term . "tenday" \
  --source-variant "tendays" \
  --target "Dekade" \
  --target-variant "Dekaden" \
  --forbid "Zehntag" --forbid "Zehntage" --forbid "zehn Tage" \
  --category "calendar"
```

`mandate-term` always sets `require_target = true` and defaults to
`enforce = error` so the approved target is positively enforced. It never
accepts `--enforce off`.

**Never set `--enforce off`** to silence validation warnings unless the
user explicitly says the term is advisory only. If you must intentionally
disable a mandatory rule, use `--allow-disable-enforcement`:

```bash
booktx context reset-term . "tenday" \
  --target "Dekade" --forbid "Zehntag" \
  --enforce off --allow-disable-enforcement
```

After a mandatory glossary change, audit the effective output:

```bash
booktx context audit-term . "tenday" --profile de_deepseekv4_flash
```

To generate a safe correction-block template for violating records:

```bash
booktx context audit-term . "tenday" \
  --write-block ingest/glossary-tenday-fixes.block.txt
```

This writes two files: the ingest block (editable targets only, parseable
with `parse_block_submission`) and a companion source block for reference.
Only violating effective records are included; the generator never guesses
the corrected translation. Revise and validate:

```bash
booktx translation revise-block . \
  --file ingest/glossary-tenday-fixes.block.txt --format block --activate
booktx validate . --fail-on-warnings
```

**Active-only validation:** `booktx validate` checks only the effective
(current) output by default. Historical inactive versions that contain
forbidden terms no longer cause warnings. Use `--include-inactive` only
for history audits:

```bash
booktx validate . --include-inactive --fail-on-history-warnings
```

## Deterministic terminology correction

When the user asks to fix a specific terminology decision and update context:

```bash
booktx mode .
booktx context status .
booktx translation search . --source "TERM" --jsonl
booktx translation search . --target "WRONG_TARGET" --jsonl
booktx translation search . --source "TERM" --target "WRONG_TARGET" --match all --write-block ingest/term-fix.block.txt
booktx translation revise-block . --file ingest/term-fix.block.txt --format block --activate
booktx check . --chapter CHAPTER --fail-on-warnings
booktx validate . --fail-on-warnings
booktx build .
```

Use booktx commands, not raw Python/store scripts. In isolated profile-root mode, if a needed search or inspection requires parent `.booktx/` data, use a profile-local booktx command (`source record`, `source chapter`, `translation search`, `translate export-index`). If no command exists, stop and report the missing booktx command instead of reading parent files directly.

Glossary entries are binding only when `enforce != "off"` and `require_target` or `forbidden_targets` is set. `enforce` alone is advisory. If `glossary_alignment_ambiguous` is reported, inspect the companion `.sources.txt` block before revising; booktx is warning that a mixed compound/standalone record cannot be deterministically aligned at target-occurrence level.
