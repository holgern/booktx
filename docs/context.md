# Context

Context is profile-local.

```text
translations/<profile>/context.json
translations/<profile>/context.md
translations/<profile>/context-history/views/<sha>/{context.json,context.md,manifest.json}
```

## Rules

1. Build or answer the context before translating.
2. Read `translations/<profile>/context.md` before opening a new task.
3. Treat `context.json` as authoritative and `context.md` as rendered.
4. Persist chapter notes with `booktx context chapter-note`, never by hand-editing `context.md`.
5. Context is not shared across languages or model experiments. Series-wide consistency is achieved by importing an explicit context pack (`booktx context export-pack` / `import-pack`), not by sharing profile state.
6. If `context.md` already contains manual chapter notes, run `booktx context import-md ./book --profile PROFILE --write` before validating or rendering again.
7. Chapter-note appends change the next task's effective context, but they do not create a new dotted version by themselves.
8. Each new translation task snapshots its composed effective context view under `context-history/views/<sha>/` and accepted candidates preserve that task-time evidence.

## Source of truth and organization doctor

`context.json` is the source of truth. Structured `style`, `global_rules`, and
glossary entries are active policy. Setup questions and chapter notes are
provenance unless their decisions are promoted into structured policy with
context commands such as `context approve`, `context mandate-term`,
`context add-term`, or `context reset-term`.

Use the report-only organization doctor to find duplicated or hidden policy:

```bash
booktx context doctor ./book --profile PROFILE
booktx context doctor ./book --profile PROFILE --json
booktx context doctor ./book --compare-profiles --json
booktx context doctor ./book --profile PROFILE --write-report reports/context-organization-report.md
```

Single-profile doctor works in project-root and isolated profile-root mode.
`--compare-profiles` is a same-book cross-profile workflow and is rejected in
isolated profile-root mode. Cross-profile comparison skips non-translation
profiles and compares only profiles with compatible source language, target
language, and target locale. Doctor does not mutate context. It reports issues
such as Q006 arrow terms, chapter-note terminology candidates, advisory entries
that look binding, and sibling profile glossary drift. Report files must not be
written under `/tmp`. In isolated profile-root mode report paths must be
profile-local relative paths, for example `reports/context-organization-report.md`.

`context render` supports explicit views:

```bash
booktx context render ./book --profile PROFILE --view full
booktx context render ./book --profile PROFILE --view effective --stdout
booktx context render ./book --profile PROFILE --view provenance --stdout
```

The default remains `full` for compatibility and it is the only view that can
be written to `context.md`. Use `effective --stdout` to inspect a cleaner agent
prompt that omits answered setup questions and treats chapter decisions as
continuity memory, not current terminology policy. Use `provenance --stdout`
to audit setup answers and chapter decisions.

## Same-book multi-profile context sync

When several sibling profiles translate the same book and language pair, keep
their reusable policy aligned with an explicit sync from project-root
collaborative mode:

```bash
booktx context sync ./book \
  --from de_gpt5_5 \
  --all-compatible \
  --section glossary \
  --term "Empire"
```

- `context sync` is a **same-book cross-profile** workflow. It is rejected in
  isolated profile-root mode.
- It reuses the context-pack merge rules, but discovers sibling targets and
  renders one consolidated plan across them.
- It is a dry run by default. Re-run with `--write` only after reviewing the
  plan.
- It never shares mutable files. Each target profile still keeps its own
  `context.json` and `context.md`.
- By default, `--all-compatible` excludes pass-through profiles and selection
  profiles.

Use `context export-pack` / `import-pack` when you need to move reusable policy
between different books. Use `context sync` when the source and targets are
sibling profiles inside the same book project.

## Source-analysis review queue

`booktx source analyze` is a source-review command, not a glossary approval
command. The canonical JSON and generated Markdown are a translation-risk review
queue for names, world-building terms, titles, and rare terms that may need a
decision before translation starts.

```bash
booktx source analyze ./book --write
booktx source analyze ./book --write --sync-profiles
booktx source analysis ./book/translations/PROFILE
```

- The JSON report is authoritative; Markdown is a readable review queue.
- Profile-root `source analysis` reads only the current profile snapshot. If it
  says the snapshot is missing or stale, refresh from the project root with
  `booktx source analyze . --write --sync-profiles`.
- Default review buckets suppress ordinary vocabulary and contraction fragments.

To turn reviewed candidates into durable context state, use explicit commands:

```bash
booktx context prefill ./book --profile PROFILE --from-source-analysis
booktx context prefill ./book --profile PROFILE --from-source-analysis --include-advisory --write
booktx context promote-candidate ./book CAND-... --profile PROFILE --as-question --write
booktx context promote-candidate ./book CAND-... --profile PROFILE --target "TARGET" --require-target --enforce error --write
booktx source ignore-candidate ./book CAND-... --reason "ordinary vocabulary" --write
booktx source review-candidate ./book CAND-... --reason "checked, no glossary decision needed" --write
```

Default `context prefill --from-source-analysis` creates **review questions**
for binding-glossary, name-policy, and invented/rare candidates. It does not
open advisory glossary entries by default. Use `--include-advisory` only when
you explicitly want low-priority phrase candidates to seed open glossary rows.

## Typical workflow

```bash
booktx context init ./book --profile de_gpt5_5 --non-interactive
booktx context questions ./book --profile de_gpt5_5
booktx context recommend ./book --profile de_gpt5_5 Q001 --text de-DE --reason "profile target locale"
booktx context questionnaire ./book --profile de_gpt5_5 --stdout
# Stop for user approval, then record the approved answer.
booktx context approve ./book --profile de_gpt5_5 Q001 --text de-DE --approved-by "user:<USER>"
booktx context mark-ready ./book --profile de_gpt5_5
booktx context render ./book --profile de_gpt5_5 --write
```

When multiple profiles exist, always pass `--profile` unless the intended
profile is already selected.

## Context question lifecycle

Questions start as `open`. Agents may store draft defaults with `context recommend`, which sets `recommended` but does not answer the question or change style policy. User-approved decisions are recorded with `context approve`, which stores `answer_source=user`, approval metadata, and applies style updates. Required dynamic questions can be added with `context add-question --required` after source review. Use `context questionnaire --stdout` to show a user-facing approval form. `context mark-ready --force --reason ...` is only for emergency or migration cases.

## Glossary commands

### add-term --forbid replacement semantics

`--forbid` replaces the full forbidden-target list. Use `--append-forbid` to add entries without removing existing ones. `--clear-forbidden` removes all forbidden targets. These options are mutually exclusive.

When the target changes, any forbidden term equal to the new target (respecting `case_sensitive`) is pruned automatically.

Updating an existing entry preserves `category`, `notes`, `enforce`, `case_sensitive`, `status`, and `examples` unless the command explicitly changes them.

### remove-term

```bash
booktx context remove-term . "empire"
booktx context remove-term . "empire" --missing-ok
```

Deletes exact glossary entries by source term. Without `--missing-ok`, exits non-zero when the term is absent.

### reset-term

```bash
booktx context reset-term . "empire" \
  --target "Imperium" \
  --forbid "Reich" --forbid "Empire" \
  --category "concept" \
  --enforce error
```

Replaces one glossary entry atomically. Refuses if the term does not exist unless `--create` is supplied. Preserves `case_sensitive` and `examples` unless explicitly changed.

## Chapter note commands

### chapter-note --replace-all

`--replace-all` sets the stored note exactly to the supplied values, allowing atomic reset of title, summaries, decisions, and open issues. Empty strings and empty lists are allowed. Conflicts with `--replace-decisions` and `--replace-open-issues`.

```bash
booktx context chapter-note . 0006 \
  --replace-all \
  --title "TWO" \
  --source-summary "..." \
  --translation-summary "..." \
  --decision "Keep Apt" \
  --open-issue "Check title rendering"
```

## Binding, advisory, and disabled glossary entries

Rendered context separates glossary entries into binding, advisory, and disabled sections. A glossary entry is binding only when `enforce != "off"` and it has `require_target = true` or at least one `forbidden_targets` value. `enforce` alone does not create an enforceable rule.

Source applicability uses longest-source-match spans across the whole glossary. Longer configured terms such as `Wasp-kinden` suppress contained shorter terms such as `wasp`; explicit plurals and hyphenated forms should be modeled with `source_variants`. When one record mixes a valid longer compound and a standalone shorter term, booktx may emit `glossary_alignment_ambiguous` because it cannot prove which target occurrence maps to which source occurrence.
