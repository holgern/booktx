# Documentation index

Start here:

1. Read [quickstart](quickstart.md) for the profile-first workflow.
2. Read [project layout](project-layout.md) for shared vs profile-local state.
3. Read [profiles](profiles.md) for the isolation model.
4. Read [commands](commands.md) for CLI usage.
5. Read [context](context.md) before working on translations.
6. Read [agent workflow](agent-workflow.md) for coding-agent operating rules.

High-level workflow:

1. Initialize a source project.
2. Extract source chunks into `.booktx/chunks/`.
3. Create or select a translation profile.
4. Build or approve `translations/<profile>/context.json` and `context.md`.
5. Translate via `translations/<profile>/ingest/`.
6. Validate the selected profile.
7. Build the final document into `translations/<profile>/output/`.
