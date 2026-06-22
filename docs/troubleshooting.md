# Troubleshooting

## `not a booktx project`

The directory is missing `.booktx/config.toml`.

Fix:

```bash
booktx init ./project --target de
```

or run the command from the correct project directory.

## `No source document found`

`source/` contains no supported file.

Fix: place exactly one `.md`, `.markdown`, or `.epub` file into `source/`.

## `Found multiple source documents`

`source/` contains more than one supported source file.

Fix: keep exactly one supported source file or update `.booktx/config.toml` to point at the intended `source_file`.

## `translation context is missing or not ready`

The context gate is working.

Fix:

```bash
booktx context init .
booktx context questions .
booktx context answer . Q001 --text de-DE
booktx context mark-ready .
```

Answer all required questions before marking ready.

## Validation: invalid JSON

The translated file is not a single JSON object.

Common causes:

- Markdown code fence around JSON
- explanatory prose before or after JSON
- trailing comments
- invalid escaping

Fix: write only the JSON object.

## Validation: record count changed

The translated chunk has more or fewer records than the source chunk.

Fix: restore the exact source record count and order.

## Validation: record id changed

A translated record id does not match the source record id at the same position.

Fix: copy record ids exactly from the source chunk.

## Validation: empty target

A target string is empty or whitespace.

Fix: provide a non-empty translation.

## Validation: placeholder removed or added

A target dropped a visible placeholder or introduced a token that does not exist in the source record.

Fix: compare the source record and target record, then preserve all visible `__NAME_NNN__` and `__TAG_NNN__` tokens exactly.

## Validation: protected name translated

A protected term appears translated or removed.

Fix: keep the corresponding `__NAME_NNN__` placeholder in target text. Do not write the original name manually unless it was not hidden in that record.

## Validation: forbidden target

The translation used a term listed under `forbidden_targets` in context.

Fix: replace the forbidden target with the approved glossary target or ask the user for a decision.

## EPUB: legacy manifest

The project was extracted with an old EPUB pipeline.

Fix:

```bash
booktx extract .
```

Then validate translated files against the new chunks.

## EPUB: source checksum mismatch

The source EPUB changed after extraction.

Fix one of these:

- restore the original source EPUB
- intentionally re-run `booktx extract .`

## EPUB: unresolved placeholder in built EPUB

A placeholder leaked into the rebuilt EPUB.

Fix:

```bash
booktx validate .
```

Repair the translated chunk that omitted or altered the placeholder, then rebuild.

## Test collection fails on missing dependencies

Install the package and dependencies:

```bash
python -m pip install -e ".[dev]"
```

If docs build fails, install Sphinx docs dependencies as described in [Development](development.md).
