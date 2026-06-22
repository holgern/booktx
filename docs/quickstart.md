# Quickstart

This page shows the shortest complete local workflow.

## Install from a checkout

```bash
python -m pip install -e .
```

For development:

```bash
python -m pip install -e ".[dev]"
```

If you want to build the Sphinx documentation locally, install the docs tools as well. See [Development](development.md).

## Create a project

```bash
booktx init ./demo --target de --source-file ./book.md --source-lang en
```

This creates:

```text
demo/
  source/
  .booktx/
  output/
```

The source document is copied to `demo/source/`.

## Inspect the source

```bash
booktx inspect ./demo
```

`inspect` reports the detected format, language settings, estimated record count, and protected terms.

## Extract chunks

```bash
booktx extract ./demo
```

This writes source chunks to:

```text
demo/.booktx/chunks/0001.json
demo/.booktx/chunks/0002.json
...
```

Extraction is idempotent: rerunning it rebuilds `chunks/` but leaves `translated/` intact.

## Build translation context

```bash
booktx context init ./demo --non-interactive
booktx context questions ./demo
```

Answer the required questions:

```bash
booktx context answer ./demo Q001 --text de-DE
booktx context answer ./demo Q002 --text "fluent literary German"
booktx context answer ./demo Q003 --text "neutral to elevated"
booktx context answer ./demo Q004 --text "natural dialogue; preserve character voice"
booktx context answer ./demo Q005 --text "Keep named people, places, and titles unchanged unless glossary says otherwise."
booktx context answer ./demo Q006 --text "Translate transparent invented terms only after glossary approval."
booktx context answer ./demo Q007 --text "Use a consistent approved rendering for kinden terms."
booktx context answer ./demo Q008 --text "Keep Sieur unless the user approves an equivalent."
booktx context answer ./demo Q009 --text "Do not use Netherlands/Dutch meanings for Lowlands/Lowlander."
booktx context answer ./demo Q012 --text "Forbidden glossary targets are errors."
booktx context mark-ready ./demo
```

The JSON context is authoritative. The Markdown context is a rendered agent-readable view:

```text
demo/.booktx/context.json
demo/.booktx/context.md
```

## Translate one chunk

Ask for the next chunk:

```bash
booktx next ./demo
```

The command prints the context path and the next source chunk path. Create a matching file in `.booktx/translated/`.

Source chunk:

```json
{
  "chunk_id": "0001",
  "source_language": "en",
  "target_language": "de",
  "records": [
    {
      "id": "0001-000001",
      "source": "__NAME_001__ looked at the city.",
      "protected_terms": ["Alice"],
      "placeholders": [
        { "token": "__NAME_001__", "original": "Alice", "kind": "name" }
      ]
    }
  ]
}
```

Translated chunk:

```json
{
  "chunk_id": "0001",
  "records": [
    {
      "id": "0001-000001",
      "target": "__NAME_001__ sah die Stadt an."
    }
  ]
}
```

Do not translate the placeholder token. The build step restores the original protected term.

## Validate

```bash
booktx validate ./demo
```

Validation writes:

```text
demo/.booktx/reports/validation-report.json
```

Fix every error before building. Warnings should be reviewed and resolved where possible.

## Build

```bash
booktx build ./demo
```

Markdown output:

```text
demo/output/book.de.md
```

EPUB output:

```text
demo/output/book.de.epub
```
