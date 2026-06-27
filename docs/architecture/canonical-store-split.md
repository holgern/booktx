# Canonical Store v3 Split Plan

Status: draft, documentation only. No code changes in this phase.

## Current design (v2)

`translation-store.json` (version 2) is both:

- provenance/history store (all translation versions, review candidates)
- active selection store (active_version, active_review pointers)
- source snapshot store (per-record source text + sha256)
- review candidate store (reviews[] array)

This is correct for a compact project but has two problems:

1. **Agent confusion**: The store contains nested candidates, active pointers, source snapshots, review records, and version metadata. Agents inspecting it directly for quality review frequently misinterpret its structure (e.g., treating `versions` as a dict instead of a list).

2. **Merge conflict risk**: Every translation insert, review insert, and activation mutates the same file. In collaborative workflows using version control, this creates serialization bottlenecks.

3. **Inspection overhead**: Loading the entire store to find review gaps or search for a specific record's effective target is O(records) per operation.

## Proposed v3 split

```text
translations/<profile>/state/
  current.json                 # active pointers and current source hashes only
  translation-candidates/
    0001.json                  # candidate history for source chunk 0001
    0002.json
  review-candidates/
    0001.json
    0002.json
  ledgers/
    translation-version-ledger.json
    review-ledger.json
```

### `current.json`

Compact canonical current ledger. One record per source record:

```json
{
  "version": 3,
  "source_sha256": "...",
  "records": {
    "0011-000001": {
      "chunk_id": 11,
      "part_id": 1,
      "source_sha256": "...",
      "active_version": "1.3",
      "active_review": "R1.2",
      "selected_kind": "review",
      "selected_ref": "R1.2"
    }
  }
}
```

Fields:

- `chunk_id`, `part_id`: backreferences to candidate files.
- `source_sha256`: the source text hash at the time of last insert.
- `active_version`: latest accepted translation version ref.
- `active_review`: latest accepted review candidate ref (if any).
- `selected_kind`: "translation" or "review" - which candidate is the effective output.
- `selected_ref`: the ref of the selected candidate.

This file is the single source of truth for "what is the current effective target".

### `translation-candidates/<chunk>.json`

Immutable history of translation versions, sharded by chunk id. Each file contains an ordered array of `TranslationCandidate` records for one chunk:

```json
[
  {
    "id": "0011-000001",
    "record_id": "0011-000001",
    "chunk_id": 11,
    "version_ref": "1.1",
    "source": "...",
    "target": "...",
    "source_sha256": "...",
    "target_sha256": "...",
    "status": "accepted",
    "created_at": "2026-...",
    "context_sha256": "...",
    "baseline_sha256": "...",
    "reviewed": false
  }
]
```

Chunks are small (default 50 records), so individual candidate files stay under ~50KB. Build and validation read current pointers from `current.json`, then load only the referenced candidate files.

### `review-candidates/<chunk>.json`

Immutable history of review candidates, sharded by chunk id:

```json
[
  {
    "id": "0011-000001",
    "record_id": "0011-000001",
    "chunk_id": 11,
    "pass_number": 1,
    "run_number": 2,
    "review_ref": "R1.2",
    "base_kind": "review",
    "base_ref": "R1.1",
    "base_target_sha256": "...",
    "target": "...",
    "target_sha256": "...",
    "status": "accepted",
    "created_at": "2026-...",
    "review_task_id": "btr-..."
  }
]
```

### `translation-version-ledger.json`

Append-only ledger of all version refs in creation order. Used for audit and migration:

```json
[
  {
    "version_ref": "1.1",
    "record_id": "0011-000001",
    "chunk_id": 11,
    "created_at": "..."
  },
  {
    "version_ref": "1.2",
    "record_id": "0011-000001",
    "chunk_id": 11,
    "created_at": "..."
  }
]
```

### `review-ledger.json`

Append-only ledger of all review refs in creation order:

```json
[
  {
    "review_ref": "R1.1",
    "record_id": "0011-000001",
    "chunk_id": 11,
    "created_at": "..."
  },
  {
    "review_ref": "R1.2",
    "record_id": "0011-000001",
    "chunk_id": 11,
    "created_at": "..."
  }
]
```

## Benefits

1. **Agent safety**: Generated current-only indexes (`current-source.jsonl`, `current-target.jsonl`) are clear and unambiguous. The canonical files are never the primary search surface for agents.

2. **Reduced merge conflicts**: Only `current.json` changes on every accepted insert (one small file). Candidate history files are append-only and rarely conflict (different chunks, different records).

3. **Inspection performance**: `current.json` is ~200KB for a typical 7500-record project. Reading it is O(1) for most queries. Candidate files are only loaded when historical detail is needed.

4. **Atomic updates**: Use write-then-rename for `current.json` (already used for `translation-store.json` via `write_json_model_atomic`). Candidate files are append-only; two concurrent inserts to different chunks never conflict.

## Migration plan

### Phase 1: parallel write (v2 + v3)

1. Add v3 write path alongside existing v2 write path.
2. On every accepted insert, write both v2 `translation-store.json` and the v3 files.
3. Add a `booktx store migrate --to v3` command that converts v2 to v3.
4. Add a `booktx store check --v3` validation that compares v2 and v3 consistency.

### Phase 2: switch reads to v3

1. Update build, validation, review status, and all other read paths to use `current.json` + candidate files.
2. Generated indexes already read from `current.json`.
3. Run both v2 and v3 reads in parallel with consistency checks for one release cycle.

### Phase 3: drop v2

1. Stop writing `translation-store.json`.
2. Mark it as deprecated/legacy format.
3. Keep a read-only migration helper for one more release cycle, then remove.

## Prerequisites

Before migration:

- Generated current-only indexes are stable (implemented in P2).
- Review todo and QA scan commands are stable (implemented in P2).
- Full test coverage for v3 write/read paths.
- Migration command tested on real-world stores.

## Current status

This document describes the planned v3 split. Implementation is deferred until:

1. The current generated-artifact approach (current-only indexes, review queue indexes) is proven stable.
2. The review workflow (P0 + P1 + P2) is complete and tested.
3. A real-world store reaches a size where merge conflicts or inspection performance become a problem.
