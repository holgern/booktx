# Agent workflow

## 1. Resolve the profile first

```bash
booktx status .
booktx profile list .
booktx profile select . de_gpt5_5
```

If multiple profiles exist, pass `--profile` on all translation-state commands.

## 2. Read the profile-local context

```text
translations/<profile>/context.md
```

Do not start translating when `context.json` is missing or not ready.

## 3. Request a task

```bash
booktx translate next . --profile de_gpt5_5 --unit batch --max-words 800 --format block
```

This writes:

- `translations/<profile>/tasks/TASK.source.block.txt`
- `translations/<profile>/ingest/TASK.block.txt`
- `translations/<profile>/ingest/TASK.json`

## 4. Fill the durable ingest file

Translate only the record bodies. Keep record ids and placeholders unchanged.

## 5. Submit the result

```bash
booktx translate insert . \
  --profile de_gpt5_5 \
  --task-id TASK \
  --file translations/de_gpt5_5/ingest/TASK.block.txt \
  --format block
```

## 6. Validate and build

```bash
booktx validate . --profile de_gpt5_5
booktx build . --profile de_gpt5_5
```

## 7. Longer bounded runs

When the user asks to continue for multiple chapters, do not request one huge
chapter task. Create a todo instead:

```bash
booktx translate todo-next . --profile de_gpt5_5 --chapters 3 --batch-words 800 --write
```

Read the generated todo markdown and follow its loop. Stop only when the todo
goal is complete or a stop condition occurs. Report partial progress if context
budget runs low.

## Guardrails

- Never mix files between profiles.
- Never edit `.booktx/chunks/*.json` directly during normal translation work.
- Never edit `translations/<profile>/translation-store.json` directly.
- Never edit `translations/<profile>/translated/*.json` directly; use `booktx translate export`.
- Use `booktx profile compare` for cross-profile review instead of mixing store files manually.
