---
name: booktx
description: Use this skill when working with booktx projects
---

# booktx Skill

## Primary goal

Work safely with `booktx`, a deterministic local CLI that prepares Markdown and
EPUB documents for translation. Source extraction is shared; mutable
translation work is profile-local.

## Core profile rule

```text
Profile = hard isolation boundary
Version = history/candidate boundary inside that profile
```

## Required profile gate

Before translating:

1. Run `booktx status .` or `booktx profile list .`.
2. Identify the selected or active profile.
3. If multiple profiles exist, always pass `--profile`.
4. Read `translations/<profile>/context.md`.
5. Stop if `translations/<profile>/context.json` is missing or not ready.

## Translation workflow

```bash
booktx extract .
booktx status .
booktx translate next . --profile PROFILE --unit batch --max-words 500 --format block
```

The durable workflow writes:

- `translations/<profile>/tasks/TASK.source.block.txt`
- `translations/<profile>/ingest/TASK.block.txt`
- `translations/<profile>/ingest/TASK.json`

Submit with:

```bash
booktx translate insert . \
  --profile PROFILE \
  --task-id TASK \
  --file translations/PROFILE/ingest/TASK.block.txt \
  --format block
```

Validate and build with:

```bash
booktx validate . --profile PROFILE
booktx build . --profile PROFILE
```

## Non-negotiable rules

- Never edit `.booktx/chunks/*.json` directly.
- Never edit `translations/<profile>/translation-store.json` directly.
- Never mix files between profiles.
- Never treat `.booktx/context.md` or `.booktx/ingest/` as the primary workflow in a profile project.
- Treat versions as profile-local.

## Compatibility files

`translations/<profile>/translated/*.json` remains compatibility/export output.
Do not edit it directly; use `booktx translate export`.

## Migration

Legacy single-layout projects can be migrated with:

```bash
booktx profile migrate-current ./book PROFILE --select
```
