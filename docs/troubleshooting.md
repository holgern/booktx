# Troubleshooting

## `multiple translation profiles exist`

Pass `--profile` or select one first:

```bash
booktx profile select ./book de_gpt5_5
```

## `no translation profile exists`

Create one:

```bash
booktx profile create ./book de_gpt5_5 --target de
```

## `task profile mismatch`

The task was created for another profile. Request a fresh task in the selected
profile.

## `submission profile mismatch`

The durable submission file or JSON payload declares a different profile than
the selected one. Use the matching `translations/<profile>/ingest/` file.

## `output filename ... does not match target language ...`

Choose an output filename that matches the profile target, for example
`book.de.epub`.

## `legacy path used after migration`

After migrating, do not use:

- `.booktx/context.json`
- `.booktx/context.md`
- `.booktx/tasks/`
- `.booktx/ingest/`
- `.booktx/translated/`
- `.booktx/translation-store.json`

Use the selected profile paths under `translations/<profile>/` instead.
