# Translation contract

The contract is the boundary between booktx and the translator or coding agent.

booktx writes source chunks and accepts translated records through CLI commands.
Validation checks that accepted records and compatibility translated chunks
preserve the structure and all required tokens.

## Primary nested store and ledger

`booktx` now treats `.booktx/translation-store.json` as the primary record-level
state and `.booktx/translation-version-ledger.json` as the project-wide version
ledger.

- Canonical record keys remain padded ids such as `0074-000038`.
- The CLI accepts `74@38` as shorthand and normalizes it to the canonical key.
- Each stored candidate carries integer `version`, integer `subversion`, and
  string `version_ref` such as `1.2`.
- Actor, harness, and model live on the major ledger track, not on each
  candidate.
- Context SHA lives on the ledger subversion, not on each candidate.
- Each source record keeps its own `active_version`.

That means `1.1` and `1.2` may share actor/harness/model metadata while
differing only by context SHA, and a model change allocates a new major track
such as `2.1`.

## Source chunk shape

```json
{
  "chunk_id": "0001",
  "source_language": "en",
  "target_language": "de",
  "records": [
    {
      "id": "0001-000001",
      "source": "__NAME_001__ looked at Mr. Smith.",
      "protected_terms": ["Alice"],
      "placeholders": [
        { "token": "__NAME_001__", "original": "Alice", "kind": "name" }
      ]
    }
  ]
}
```

## Compatibility translated chunk shape

```json
{
  "chunk_id": "0001",
  "records": [
    {
      "id": "0001-000001",
      "target": "__NAME_001__ sah Mr. Smith an."
    }
  ]
}
```

## Hard rules

A translated chunk is invalid if any of these are true:

| Rule                                       | Why it matters                                            |
| ------------------------------------------ | --------------------------------------------------------- |
| JSON is invalid                            | Build and validation need machine-readable files          |
| Commentary appears outside the JSON object | Agents must not wrap translations in prose or code fences |
| `chunk_id` changed                         | The file no longer maps to the source chunk               |
| Record count changed                       | Booktx cannot align source and target streams             |
| Any record `id` changed                    | The record mapping is broken                              |
| A `target` is empty                        | The translation is incomplete                             |
| A visible placeholder was removed          | Rebuild cannot restore protected material                 |
| A visible placeholder was changed          | Rebuild cannot safely identify the protected material     |
| A new placeholder was invented             | The new token has no stored original                      |
| A protected name appears translated        | Protected terms must survive exactly                      |

## Placeholder rules

Preserve visible placeholders exactly:

```text
__NAME_001__
__TAG_001__
```

Do not:

- translate a placeholder
- remove a placeholder
- replace a placeholder with the original visible text
- change zero padding
- create new placeholder ids
- move a placeholder to another record unless it was visible in that record and the sentence requires it

The build step restores placeholders after joining translated records back into spans.

## One source sentence to one translated sentence

The validator expects each source record to have exactly one target record.

Do not merge records:

```json
[
  { "id": "0001-000001", "target": "..." },
  { "id": "0001-000002", "target": "..." }
]
```

must not become:

```json
[{ "id": "0001-000001", "target": "... ..." }]
```

Do not split one source record into multiple translated records.

## Missing translations

If a source chunk has no accepted record in the translation store and no valid
legacy translated chunk, validation reports it as missing but does not treat
that as a contract error for the existing translated data. Build falls back to
source text for missing translated records unless `booktx build --require-complete`
is used.

For production translation, treat missing chunks as incomplete work even if the validator can still inspect the project.

## Ledger-backed structural validation

For nested store data, validation treats these as structural problems:

- an invalid canonical record key
- an invalid or missing `active_version`
- a `version_ref` that does not match its integer tuple fields
- duplicate `version_ref` values within one record
- a store candidate whose version ref is missing from the ledger
- an active version whose candidate is not `accepted`
- a stored source text or source SHA that no longer matches the extracted source
- a stale `context.md` render that no longer matches `context.json` (warning)

## Stale translations

If a translated file exists for a chunk id that no longer exists in `chunks/`, validation reports a warning. This usually means the source was edited and extraction was rerun.

Review stale files before deleting them; they may contain useful translation work.

## Minimal valid translated file

```json
{
  "chunk_id": "0001",
  "records": [
    {
      "id": "0001-000001",
      "target": "Translated sentence."
    }
  ]
}
```

No Markdown fences. No comments. No trailing explanation.
