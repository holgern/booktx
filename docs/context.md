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
5. Context is not shared across languages or model experiments.
6. If `context.md` already contains manual chapter notes, run `booktx context import-md ./book --profile PROFILE --write` before validating or rendering again.
7. Chapter-note appends change the next task's effective context, but they do not create a new dotted version by themselves.
8. Each new translation task snapshots its composed effective context view under `context-history/views/<sha>/` and accepted candidates preserve that task-time evidence.

## Typical workflow

```bash
booktx context init ./book --profile de_gpt5_5 --non-interactive
booktx context questions ./book --profile de_gpt5_5
booktx context answer ./book --profile de_gpt5_5 Q001 --text de-DE
booktx context mark-ready ./book --profile de_gpt5_5
booktx context render ./book --profile de_gpt5_5 --write
```

When multiple profiles exist, always pass `--profile` unless the intended
profile is already selected.
