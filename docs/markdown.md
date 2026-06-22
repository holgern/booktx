# Markdown handling

Markdown support is implemented in `booktx.markdown_io`.

## What gets translated

booktx extracts inline prose from these contexts:

- paragraphs
- headings
- list items
- blockquotes
- table cells
- emphasis and strong text when represented as inline content

The extracted prose is segmented into records by `phrasplit`.

## What is preserved

These are not translated directly:

- fenced code blocks
- indented code blocks
- inline code
- link URLs
- raw inline HTML
- raw HTML blocks
- YAML front matter keys

Inline non-translatable content is hidden behind `__TAG_NNN__` placeholders. Protected names are hidden behind `__NAME_NNN__` placeholders.

## Front matter

Leading YAML front matter is preserved. The extractor splits it from the body before Markdown parsing.

Example:

```markdown
---
title: Example
author: Alice
---

# Chapter One

Hello world.
```

The front matter block remains in the template. Body prose is extracted normally.

## Links

For Markdown links, the visible link text can be translated, but the URL is hidden and restored.

Source:

```markdown
Read [the guide](https://example.com).
```

Extractor behavior:

```text
Read [the guide](__TAG_001__).
```

The target must preserve `__TAG_001__`.

## Inline code

Inline code is hidden:

```markdown
Run `booktx validate .` before building.
```

Extractor behavior:

```text
Run __TAG_001__ before building.
```

The target must preserve `__TAG_001__`.

## Template rebuild

The extractor creates a Markdown template with internal `__SPANTX_NNNN__` markers. These internal markers must never appear in translated chunks. Build replaces each marker with the translated span text.

## Known limitations

Markdown extraction relies on literal inline token content being findable in the original Markdown body. Very unusual Markdown that parses to inline content not present as a direct substring may leave a span unreplaced in the template.

Attributes, image alt text, and complex raw HTML are not translated in v1.
