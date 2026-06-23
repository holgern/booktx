# Translation contract

`booktx` now keeps translation state per profile.

## Primary profile-local state

- `translations/<profile>/translation-store.json`
- `translations/<profile>/translation-version-ledger.json`
- `translations/<profile>/tasks/`
- `translations/<profile>/ingest/`
- `translations/<profile>/translated/`

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
