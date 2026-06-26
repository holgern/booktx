# Agent workflow

## 1. Choose the access mode

### Collaborative translation workflow

Start at the project root when you need profile selection or cross-profile
review:

```bash
booktx status .
booktx profile list .
booktx profile select . de_gpt5_5
```

If multiple profiles exist, pass `--profile` on all translation-state commands.

### Isolated evaluation workflow

Start inside `translations/<profile>/` when you want unbiased model or context
evaluation for one profile:

```bash
booktx mode .
booktx doctor isolation .
booktx source status .
booktx context status .
```

In isolated mode, use only profile-local `booktx ... .` commands. Never use
parent paths, absolute paths, shell globs, interpreter snippets, or sibling
profile commands. If booktx prints a sibling profile or a parent path, stop and
report a booktx isolation bug.

## 2. Read the profile-local context

```text
context.md
```

Do not start translating when `context.json` is missing or not ready.

## 3. Request a task

```bash
booktx translate next . --unit batch --max-words 800 --format block
```

This writes:

- `tasks/TASK.source.block.txt`
- `ingest/TASK.block.txt`
- `ingest/TASK.json`

The task JSON also records the dotted baseline version plus the immutable
context-view snapshot used for that task.

## 4. Fill the durable ingest file

Translate only the record bodies. Keep record ids and placeholders unchanged.

## 5. Submit the result

```bash
booktx translate insert . \
  --task-id TASK \
  --file ingest/TASK.block.txt \
  --format block
```

## 6. Validate and build

```bash
booktx validate . --fail-on-warnings
booktx build . --require-complete
```

For per-batch validation within a bounded todo, use scoped `booktx check . --chapter CHAPTER --fail-on-warnings` instead. Use `booktx validate` only for the final pre-build check.

## 6b. Refresh editor QA indexes

After translation/review changes, refresh the three editor-friendly indexes for source-only search, target-only search, and side-by-side review:

```bash
booktx translate export-index .
```

This writes `source-index.json`, `target-index.json`, and `source-target-index.json` into the profile directory. Use `rg` to search translated terms without English source false positives (`rg "Wespen" target-index.json`) or source terms without target matches (`rg "Wasp" source-index.json`). Use `nvim source-target-index.json` for side-by-side scanning.

The three files are generated artifacts. Do not edit them manually and do not use them as build input.


## 7. Longer bounded runs

When the user asks to continue for multiple chapters, do not request one huge
chapter task. Create a todo instead:

```bash
booktx translate todo-next . --profile de_gpt5_5 --chapters 3 --batch-words 800 --write
booktx translate todo-status . --profile de_gpt5_5 --latest
booktx translate todo-resume . --profile de_gpt5_5 --latest --format block
```

Read the generated todo markdown and follow its loop. After each completed
chapter, fill the `booktx context chapter-note` template printed by
`booktx translate insert`; do not hand-edit `context.md` for chapter notes.

If validation flags an old accepted record during a bounded run, use
`booktx translation revise-record . RECORD_ID --target "..."` to fix it.
Never edit `translation-store.json` directly.
That chapter-note append affects the next task's context view, but it does not
mint a new dotted version by itself.
Stop when the todo goal is complete, when `todo-status` says it is complete, or
when a stop condition occurs. Report partial progress if conversation or tool
budget runs low. `--max-run-words` is advisory only.

## Guardrails

- Never mix files between profiles.
- Cross-profile reference work is allowed only from project-root collaborative
  mode.
- Never edit `.booktx/chunks/*.json` directly during normal translation work.
- Never edit `translations/<profile>/translation-store.json` directly. If validation flags an old accepted record, use `booktx translation revise-record` to fix it.
- Never edit `translations/<profile>/translated/*.json` directly; use `booktx translate export`.
- Use `booktx profile compare` for cross-profile review instead of mixing store files manually.
- If a `todo-status`, `todo-resume`, or `todo-next` command fails with an internal
  booktx error, stop and report the tool failure. Do not silently switch to a
  large unbounded `translate next --unit chapter` task. Bounded todos exist to
  keep agent runs within budget; bypassing them defeats that purpose.
  Only use `translate next --unit chapter` for small chapters or when the user
  explicitly requests a whole-chapter task.

## Finish a single large chapter

If the user asks to finish a chapter and that chapter has more than the safe
task budget, booktx automatically creates a single-chapter todo and returns
bounded batch tasks. Do not create a giant chapter task:

```bash
booktx translate next . --chapter 0005 --unit chapter --max-words 800 --format block
# booktx auto-creates a single-chapter todo and returns a bounded batch
booktx translate insert . --task-id TASK --file ingest/TASK.block.txt --format block
booktx check . --chapter 0005 --fail-on-warnings
booktx translate todo-resume . --latest --format block
# repeat until chapter complete
```

Only use `--force-chapter` for small chapters or when explicitly requested.

After each chapter, always run `booktx check` before adding the chapter note:

```bash
booktx check . --chapter 0005 --fail-on-warnings
booktx context chapter-note . 0005 --title "ONE" ...
```

## Context approval hard stop

Stop and ask the user whenever context questions are open or only recommended. Do not translate from a context that you generated yourself. Prepare a user review form, then wait for explicit approval or edited answers before running `booktx context approve` and `booktx context mark-ready`.

## EPUB inline XHTML translation rule

For EPUB records, preserve inline XHTML tags and attributes in the target. Translate text nodes only. Do not convert `<em>` or other inline tags to Markdown markers.

## 7b. Quality review pass workflow

After validation passes, optional quality review improves the accepted target:

1. `booktx review status .` -- check which records still need review per pass
2. `booktx review next . --pass 1` -- create a review task for un-reviewed records
3. Edit the prefilled ingest block under `translations/<profile>/reviews/`
4. `booktx review insert . --review-task-id TASK --file reviews/TASK.block.txt`
5. Repeat for pass 2: `booktx review next . --pass 2`, review, insert
6. Validate and build: `booktx validate . --fail-on-warnings && booktx build . --require-complete --require-reviewed`

During review pass tasks, review the existing target critically. Preserve meaning,
placeholders, protected terms, and inline XHTML. If the current target is already
good, submit it unchanged -- booktx stores an explicit review candidate either way.
