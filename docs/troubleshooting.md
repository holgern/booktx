# Troubleshooting

## `multiple translation profiles exist`

Pass `--profile` or select one first:

```bash
booktx profile select ./book de_gpt5_5
```

## `no translation profile exists`

Create one:

```bash
booktx profile create ./book de_gpt5_5 --target de
```

## `task profile mismatch`

The task was created for another profile. Request a fresh task in the selected
profile.

## `submission profile mismatch`

The durable submission file or JSON payload declares a different profile than
the selected one. Use the matching `translations/<profile>/ingest/` file.

## `output filename ... does not match target language ...`

Choose an output filename that matches the profile target, for example
`book.de.epub`.

## `legacy path used after migration`

After migrating, do not use:

- `.booktx/context.json`
- `.booktx/context.md`
- `.booktx/tasks/`
- `.booktx/ingest/`
- `.booktx/translated/`
- `.booktx/translation-store.json`

Use the selected profile paths under `translations/<profile>/` instead.

## Stale translation task version

`booktx translate insert` reports a stale task version when the durable task
was created against an older context/version than the current one. Do not
force the old file through. Request a fresh task:

```bash
booktx translate next ./book --profile PROFILE --format block
```

and submit the newly generated ingest file.

## `context_render_drift`

`context.md` differs from `context.json`. If the difference is chapter notes
you want to keep, run:

```bash
booktx context import-md ./book --profile PROFILE --write
```

Prefer `booktx context chapter-note` for future chapter summaries.

## Source drift after extraction

If the source file changed since the last `booktx extract`, the recorded
source hash no longer matches and inserts/builds are blocked. Re-extract to
realign the chunks and source manifest:

```bash
booktx extract ./book
```

Then re-request tasks against the refreshed source.

## Validation warnings during bounded todo runs

Bounded todo runs should use scoped validation per batch:

```bash
booktx check ./book --profile PROFILE --chapter CHAPTER --fail-on-warnings
```

Use `booktx validate ./book --profile PROFILE --fail-on-warnings` only for the
final pre-build check. If validation flags an old accepted record, use
`booktx translation revise-record` to fix it; never edit
`translation-store.json` directly.

Warnings remain non-fatal for plain `booktx validate`, but `todo-resume` and
the generated todo workflow expect warnings to be cleared before continuing.

## Latest todo is incomplete

Inspect the live bounded-run state before requesting more work:

```bash
booktx translate todo-status ./book --profile PROFILE --latest
booktx translate todo-resume ./book --profile PROFILE --latest --format block
```

If `todo-status` reports overlap ambiguity, re-run with `--todo-id TODO`.

## Todo planned chapters are already complete

When the planned chapter set is finished, `booktx translate todo-resume` stops
instead of issuing a task for the next chapter. Start a new bounded run if you
want more work:

```bash
booktx translate todo-next ./book --profile PROFILE --chapters 3 --batch-words 800 --write
```

## Task created outside a todo

If a user asked to continue a bounded run but the current task was created with
plain `booktx translate next`, switch back to the todo controller:

```bash
booktx translate todo-status ./book --profile PROFILE --latest
booktx translate todo-resume ./book --profile PROFILE --latest --format block
```

## Context is not ready

Translation work requires a ready context. If you see `translation context is missing or not ready`, initialize and mark it ready:

```bash
booktx context init ./book --profile PROFILE --non-interactive
booktx context questionnaire ./book --profile PROFILE --stdout
# Ask the user to approve or edit answers, then use context approve before mark-ready.
booktx context mark-ready ./book --profile PROFILE
```

## Missing source chunks

`No source chunks found` means extraction has not run (or the source file is
missing). Check `.booktx/source-config.toml` points at an existing source
file, then:

```bash
booktx extract ./book
```

## Output filename mismatch

The output filename must match the profile target language. For Markdown the
rebuilt file is `translations/<profile>/output/<name>.md`; for EPUB it is
`translations/<profile>/output/<name>.epub`. If `booktx build` complains,
create a profile with a matching `--target`/output filename, or override with
`booktx profile create ... --output-filename book.de.md`.

## Old `.booktx/ingest` path after migration

After `booktx profile migrate-current`, submissions belong under
`translations/<profile>/ingest/`, never `.booktx/ingest/`. If a missing-file
error hints at the profile-local ingest path, switch to that file. Re-running
`booktx translate next` regenerates the correct ingest file.

## `TranslationTodo` is not fully defined

This indicates an internal booktx model initialization bug, not a translation
error. The message looks like:

```text
`TranslationTodo` is not fully defined; you should define `StatusTotals`,
then call `TranslationTodo.model_rebuild()`.
```

Do not edit todo JSON manually. Upgrade booktx or run the fixed version where
`StatusTotals` is defined in `booktx.models` and `TranslationTodo` no longer
requires a late Pydantic `model_rebuild()`.

If you see `internal todo model initialization failed` instead, the error
classifier detected a schema/program error rather than a data validation error.
Report this as a booktx bug with the full error message.

Do not use `context mark-ready --force` during normal translation setup. If a legacy migration truly needs it, pass `--reason` and document the external approval.

## EPUB inline XHTML validation failures

If validation reports `inline_xhtml_preserved`, `inline_xhtml_no_new_attributes`, `inline_xhtml_no_block_tags`, or `inline_xhtml_opaque_preserved`, correct the target so it preserves the source inline XHTML skeleton. Use `booktx translate audit-inline ./book --profile PROFILE` to list active records that need review.

## Validate passed but build failed (inline XHTML)

If `booktx build` fails with `target inline XHTML skeleton does not match the source`
but `booktx validate` reported no errors, this indicates a validation gap that
should no longer occur with the current version. The EPUB inline-XHTML preflight
is now shared between validate/check and build.

If you encounter this, run `booktx check` which uses the build-grade preflight:

```bash
booktx check . --chapter 0005 --fail-on-warnings
```

The check output will show the exact record, chapter, source/target snippet, and
suggested fix commands. File a booktx bug if check also misses the error that
build catches.

## TOC lists more chapters than chapter-map

An EPUB contents page can advertise numbered chapters that were not extracted
or not detected. Symptoms: `booktx validate` reports
`epub_toc_chapter_missing_from_map` or `epub_toc_href_extracted_but_unmapped`,
or the chapter map ends early (for example at `TEN` while the TOC lists
`ONE` through `TWENTY-SIX`).

Diagnose with the read-only audit:

```bash
booktx chapters . --audit
```

Interpret the findings:

- `epub_toc_href_missing_from_extracted_spans` means the target XHTML was not
  extracted. The source is likely a preview/truncated EPUB or extraction
  skipped a spine document. Do not synthesize empty chapters; re-extract from a
  complete source.
- `epub_toc_href_extracted_but_unmapped` means the target was extracted but no
  chapter boundary covers it, so translation would skip it. It is a blocking
  `error` finding: `next`, `next-chapter`, `translate next --chapter`, and todo
  creation will refuse new work until it is resolved. Re-extract to refresh
  upstream block annotations, or inspect the source with `booktx epub inspect .`.
- `epub_navigation_partial` indicates navigation is a strict subset of the
  visible chapter signals.

Run `booktx chapters .` to refresh the map after fixing the source or
re-extracting.
