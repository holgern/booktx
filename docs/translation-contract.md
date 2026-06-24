# Translation contract

`booktx` now keeps translation state per profile.

## Primary profile-local state

- `translations/<profile>/translation-store.json`
- `translations/<profile>/translation-version-ledger.json`
- `translations/<profile>/tasks/`
- `translations/<profile>/ingest/`
- `translations/<profile>/translated/`
- `translations/<profile>/todos/`

## Todo files

`translations/<profile>/todos/<todo-id>.{json,md}` are **run-control artifacts**
written by `booktx translate todo-next`. They describe a bounded multi-chapter
agent run (chapters to complete, per-task word budget, stop conditions).

Todo files are NOT translation submissions. The agent reads the markdown loop
instructions, fills ingest files, and runs `translate insert` for each batch.
Do not submit todo JSON files through `translate insert`.

## Task metadata

`booktx translate next` persists tasks with:

- `profile`
- `target_language`
- `target_locale`
- `translation_version`
- `context_sha256`
- `source_sha256`
- source/profile config hashes

`booktx translate insert` rejects:

- task/profile mismatches
- submission/profile mismatches
- stale task version metadata

## Compatibility exports

Compatibility translated chunk files remain JSON objects with `chunk_id` and
ordered record targets, but they are profile-local:

```text
translations/<profile>/translated/NNNN.json
```

## Comparison rules

- `booktx translation compare` is profile-local.
- Cross-profile comparison is explicit under `booktx profile compare`.

## Block submission schema

`booktx translate next --format block` writes a durable ingest file under
`translations/<profile>/ingest/<task>.block.txt`. Edit only that file and
submit it back. Example:

```text
# booktx block submission
# profile: de_gpt5_5
# task: bt-task-20260101T000000Z-ch01-0001r0001-a1b2c3d4
# translation_version: 1.2
>>> 0001-000001
Translated text here.
>>> 0001-000002
Second translated record.
```

Rules:

- Keep every `>>> RECORD_ID` header unchanged.
- Write only the target translation under each header.
- Preserve required placeholder tokens (for example `__NAME_001__`) exactly.
- Do not add commentary outside target text.
- Do not edit `tasks/<task>.source.block.txt` as the submission.

## JSON submission schema

Submissions may also be supplied as JSON (`schema_version` 2). Example:

```json
{
  "schema_version": 2,
  "profile": "de_gpt5_5",
  "task_id": "bt-task-...",
  "translation_version": "1.2",
  "records": [{ "id": "0001-000001", "target": "Translated text here." }]
}
```

The `profile` and `translation_version` fields must match the target profile
and the durable task metadata, or `booktx translate insert` rejects the
submission.
