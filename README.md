[![PyPI - Version](https://img.shields.io/pypi/v/booktx)](https://pypi.org/project/booktx/)
![PyPI - Python Version](https://img.shields.io/pypi/pyversions/booktx)
![PyPI - Downloads](https://img.shields.io/pypi/dm/booktx)
[![codecov](https://codecov.io/gh/holgern/booktx/graph/badge.svg?token=EFO4GQF52W)](https://codecov.io/gh/holgern/booktx)

# booktx

`booktx` is a deterministic local CLI that prepares **Markdown** and **EPUB**
documents for translation by a coding agent or human translator.

It:

- extracts source text into stable record chunks,
- tracks progress and translation versions,
- hands out safe translation tasks,
- validates submissions,
- rebuilds translated output.

`booktx` never translates text itself and makes no network calls.

## Install

```bash
pip install -e .
```

For development and docs:

```bash
python -m pip install -e ".[dev,docs]"
```

Python 3.10+ is supported.

## Core model

```text
Profile = hard boundary for mutable translation state
Access mode = determines whether sibling profiles are visible
Version = history/candidate boundary inside that profile
```

`.booktx/` now holds only shared source-derived state. Mutable translation state
lives under `translations/<profile>/`.

## Project layout

```text
book/
  source/
    book.epub

  .booktx/
    source-config.toml
    source-manifest.json
    names.json
    chapter-map.json
    profile-state.json
    chunks/

  translations/
    de_gpt5_5/
      .booktx-profile.json
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
        book.de.epub
```

## Quickstart

```bash
booktx init ./demo --source-file book.epub --source-lang en
booktx extract ./demo

booktx profile create ./demo de_gpt5_5 \
  --target de \
  --target-locale de-DE \
  --model codex-openai/gpt-5.5@low \
  --select

booktx context init ./demo --profile de_gpt5_5 --non-interactive
booktx context questions ./demo --profile de_gpt5_5
# Ask the user to approve or edit answers before continuing.
booktx context approve ./demo --profile de_gpt5_5 Q001 --text "<USER_APPROVED_TEXT>" --approved-by "user:<USER>"
booktx context render ./demo --profile de_gpt5_5 --write
booktx context mark-ready ./demo --profile de_gpt5_5

booktx translate next ./demo \
  --profile de_gpt5_5 \
  --unit batch \
  --max-words 800 \
  --format block

booktx translate insert ./demo \
  --profile de_gpt5_5 \
  --task-id TASK \
  --file translations/de_gpt5_5/ingest/TASK.block.txt \
  --format block

booktx validate ./demo --profile de_gpt5_5
booktx build ./demo --profile de_gpt5_5
```

## Collaborative vs isolated profile-root mode

`booktx` supports two deliberate access modes:

1. **Collaborative project-root mode**: start the harness at the book project
   root when you need profile administration, profile comparison, or other
   cross-profile work.
2. **Isolated profile-root mode**: start the harness inside
   `translations/<profile>/` when you want unbiased model evaluation without
   normal booktx workflows revealing sibling profiles.

Profile-root isolation is **booktx-mediated isolation**, not OS sandboxing. It
depends on two things:

- the harness starts inside `translations/<profile>/` and blocks parent paths,
  absolute paths, sibling profile paths, shell globs, and arbitrary filesystem
  inspection snippets;
- `booktx` commands are used with project argument `.` and do not print parent
  or sibling paths.

If a profile-root command suggests `../`, prints an absolute path, or reveals a
sibling profile, stop and report a booktx isolation bug.

### Isolated evaluation workflow

From `book/translations/de_gpt5_5/`:

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

In this mode, `booktx` automatically binds the current profile, brokers source
access internally, and renders profile-local paths such as `tasks/...`,
`ingest/...`, `reports/...`, and `output/...`.

## Bounded agent runs

When asking an agent to continue for several chapters, create a durable todo:

```bash
booktx translate todo-next ./demo \
  --profile de_gpt5_5 \
  --chapters 3 \
  --batch-words 800 \
  --max-run-words 12000 \
  --write
```

This writes a todo file (not translations) under `translations/<profile>/todos/`.
Continue bounded runs with:

```bash
booktx translate todo-status ./demo --profile de_gpt5_5 --latest
booktx translate todo-resume ./demo --profile de_gpt5_5 --latest --format block
booktx validate ./demo --profile de_gpt5_5 --fail-on-warnings
```

`--max-run-words` is advisory only: it tells the agent when to stop and report
progress, but booktx does not hard-stop accepted work at that threshold. Prefer
batches over chapter-sized tasks.

After a chapter completes, use the `booktx context chapter-note` template
printed by `booktx translate insert` instead of hand-editing `context.md`.

Chapter-note appends update the next task's effective context, but they do
**not** create a new dotted translation version. Dotted versions track baseline
policy changes such as style, glossary, answered questions, global rules,
readiness, source metadata, language metadata, or actor/model track changes.

## Final release output

For final release output, prefer:

```bash
booktx validate ./demo --profile de_gpt5_5 --fail-on-warnings
booktx build ./demo --profile de_gpt5_5 --require-complete
```

## Pass-through validation profile

Use a pass-through profile to verify that extraction and EPUB reconstruction
include all text before doing real translation:

```bash
booktx extract ./demo
booktx pass-through ./demo --profile passthrough_en --create
```

This writes source-as-target translated chunks under
`translations/passthrough_en/translated/`, validates complete coverage, and
builds `translations/passthrough_en/output/...`. Compare the output EPUB against
`source/book.epub` with an EPUB diff viewer. The included EPUB fixture should be
byte-identical, but real-world EPUBs should be treated as reconstruction checks,
not guaranteed byte-for-byte copies. Never run pass-through against a real
translation profile.

## Multiple profiles

Create one profile per target language, model experiment, or hard-isolated
context experiment. Two profiles can target the same language with different
models, or the same model with different languages:

```bash
booktx profile create ./demo de_gpt5_5 --target de --model codex-openai/gpt-5.5@low
booktx profile create ./demo de_glm_5_2 --target de --model glm-5.2
booktx profile create ./demo fr_gpt5_5 --target fr --model codex-openai/gpt-5.5@low
```

### Profile resolution

When a command needs a single profile, booktx resolves it in this order:

```text
--profile wins; otherwise the active profile; otherwise exactly one profile;
otherwise fail for target-state commands.
```

If a project has more than one profile, always pass `--profile`.

### Live identity

`profile list` and `profile show` render the **current** identity from
`translations/<profile>/identity.json`, which is updated by
`booktx model set`, `actor set`, and `harness set`. The identity embedded in
`config.toml` is only the initial default captured at creation.

## Legacy projects

Old single-layout projects can be migrated in place:

```bash
booktx profile migrate-current ./demo de_gpt5_5 --select
```

CLI identity overrides (`--model`, `--actor`, `--harness`) are honored over any
legacy `.booktx/identity.json`.

## Common commands

```bash
booktx status ./demo
booktx status ./demo --profile de_gpt5_5
booktx mode ./demo
booktx profile list ./demo
booktx profile show ./demo de_gpt5_5
booktx whoami ./demo --profile de_gpt5_5
booktx version current ./demo --profile de_gpt5_5
booktx translate task-status ./demo --profile de_gpt5_5 --task-id TASK
booktx translation compare ./demo --profile de_gpt5_5 74@38 --versions 1.1,1.2
booktx profile compare ./demo --profiles de_gpt5_5,de_glm_5_2 --record 0001-000001
booktx source status ./demo
```

`booktx translate next` also snapshots the exact effective task context under
`translations/<profile>/context-history/views/<sha>/`. New tasks carry both the
baseline version (for example `1.2`) and the immutable context-view evidence
used for that task, and accepted candidates preserve that evidence.

## Translation contract

- record ids must stay unchanged;
- placeholders must stay unchanged;
- targets must be non-empty;
- submissions must stay in the selected profile;
- `translations/<profile>/translation-store.json` is the primary record-level state;
- `translations/<profile>/translated/*.json` is compatibility/export output.

## Documentation

- [quickstart](docs/quickstart.md)
- [project layout](docs/project-layout.md)
- [profiles](docs/profiles.md)
- [commands](docs/commands.md)
- [context](docs/context.md)
- [agent workflow](docs/agent-workflow.md)

## Context approval

booktx never decides translation policy by itself. An agent may propose context answers, but the user must approve them before translation begins. Do not use `context mark-ready --force` during normal translation work.

### EPUB inline XHTML records

EPUB records may expose constrained inline XHTML fragments such as `<em>`, `<strong>`, `<span class="...">`, `<a href="...">`, `<sup>`, `<sub>`, or `<code>`. Translators must preserve tags and attributes around the equivalent target-language phrase and must not replace XHTML with Markdown markers.

## Quality review commands

Quality review is an optional workflow that improves already-accepted translations:

- `booktx review status .` -- report review coverage
- `booktx review next . --pass 1` -- create a review task for pass 1
- `booktx review insert . --review-task-id TASK --file reviews/TASK.block.txt` -- accept review results
- `booktx review activate . RECORD R1.2` -- manually activate a review candidate

Review candidates are stored separately from translation versions in
`translations/<profile>/reviews/`. The effective output resolves as
`active_review (if valid) -> active_version -> missing`.

Enable quality review by adding `[quality_review]` to the profile's `config.toml`.
See `docs/profiles.md` for configuration reference.
