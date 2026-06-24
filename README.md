[![PyPI - Version](https://img.shields.io/pypi/v/booktx)](https://pypi.org/project/booktx/)
![PyPI - Python Version](https://img.shields.io/pypi/pyversions/booktx)

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

Python 3.10+ is supported.

## Core model

```text
Profile = hard isolation boundary
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
booktx context mark-ready ./demo --profile de_gpt5_5 --force

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
The agent reads the todo and loops `translate next -> fill -> insert -> validate`
until complete or a stop condition occurs. Prefer batches over chapter-sized tasks.

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
booktx profile list ./demo
booktx profile show ./demo de_gpt5_5
booktx whoami ./demo --profile de_gpt5_5
booktx version current ./demo --profile de_gpt5_5
booktx translate task-status ./demo --profile de_gpt5_5 --task-id TASK
booktx translation compare ./demo --profile de_gpt5_5 74@38 --versions 1.1,1.2
booktx profile compare ./demo --profiles de_gpt5_5,de_glm_5_2 --record 0001-000001
```

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
