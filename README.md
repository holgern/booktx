# booktx

`booktx` is a deterministic command-line tool that prepares **Markdown** and
**EPUB** documents for translation by a coding agent (or a human translator).
It does the mechanical bookkeeping — extract translatable sentences, validate
the translation, rebuild the document — and leaves the actual translation to
you or your agent.

**booktx never translates text itself** and makes no LLM or network calls. All
translation text comes from JSON chunk files that you (or an agent) fill in.

## Legal notice

booktx is intended for DRM-free documents that you lawfully own or are allowed
to process. The license of booktx applies only to the software, not to input
books or generated translations. Do not redistribute translated books unless
you have the rights to do so.

booktx is licensed under **AGPL-3.0-or-later** (it uses
[`EbookLib`](https://github.com/aerkalov/ebooklib), which is AGPL).

---

## Install

```bash
pip install -e .          # editable install from a checkout
# or, once published:
pip install booktx
```

Requires Python 3.10+. The `booktx` console script is installed automatically.

## Project layout

`booktx init ./book --target de` creates this layout:

```text
book/
  source/
    book.md        # or book.epub — exactly one source document
  .booktx/
    config.toml    # source/target language, format, chunk size
    manifest.json  # source digest + per-document templates (epub)
    names.json     # manually protected verbatim terms (names, brands, places)
    context.json   # authoritative style/glossary/questions context
    context.md     # rendered context that agents must read before translating
    chapter-map.json # detected chapter -> chunk ranges (additive metadata)
    chunks/        # 0001.json, 0002.json ... (booktx writes these)
    translated/    # 0001.json, 0002.json ... (the agent writes these)
    reports/       # validation-report.json
  output/
    book.de.md     # or book.de.epub — the rebuilt translated document
```

## Commands

```bash
booktx init ./book --target de                 # create the project
booktx init ./book --target de --source book.md --source-lang en
booktx inspect ./book                          # summarise the source
booktx extract ./book                          # write .booktx/chunks/*.json
booktx context init ./book --non-interactive   # create open questions/context
booktx context questions ./book                # show required context questions
booktx context answer ./book Q001 --text de-DE # answer one context question
booktx context mark-ready ./book               # mark ready after required answers
booktx chapters ./book                         # list detected chapter ranges
booktx next ./book                             # print next chunk (requires context)
booktx next ./book --unit chapter              # print next incomplete chapter
booktx next-chapter ./book                     # chapter workflow shortcut
booktx validate ./book                         # enforce contract + context lint
booktx build ./book                            # rebuild output/book.<target>.<ext>
```

`booktx next` refuses to return translation work until `.booktx/context.json`
exists and has `ready: true`. When ready, it prints the rendered context path
before the chunk path. Use `--allow-missing-context` only for legacy workflows
and tests that deliberately bypass the context gate.

`booktx next --unit chapter` and `booktx next-chapter` print the next
incomplete chapter and all chunk files it covers. `booktx chapters` writes
`.booktx/chapter-map.json` and lists detected chapter ranges.

`booktx context init --non-interactive` creates a not-ready context with open
questions and a seed glossary. Required questions must be answered before
`booktx context mark-ready` succeeds. `context.md` is generated from
`context.json`; the JSON file is authoritative.

`booktx extract` is **idempotent**: it rebuilds `chunks/` on every run but
leaves `translated/` untouched, so re-extracting after editing the source never
destroys work in progress. Stale `translated/*.json` files whose chunk no longer
exists are kept and reported as warnings.

## The translation contract

`booktx extract` writes a chunk file like this:

```json
{
  "chunk_id": "0001",
  "source_language": "en",
  "target_language": "de",
  "records": [
    {
      "id": "0001-000001",
      "source": "Alice looked at Mr. Smith.",
      "protected_terms": ["Alice", "Mr. Smith"],
      "placeholders": []
    }
  ]
}
```

The agent writes the matching file to `.booktx/translated/0001.json`:

```json
{
  "chunk_id": "0001",
  "records": [
    {
      "id": "0001-000001",
      "target": "Alice sah Mr. Smith an."
    }
  ]
}
```

### Hard rules (enforced by `booktx validate`)

A translated chunk is rejected if any of the following is true:

- the JSON is invalid, or there is commentary outside the JSON object;
- the record count changed;
- any record id changed;
- any target is empty;
- a placeholder (`__NAME_NNN__` / `__TAG_NNN__`) was removed, changed, or added;
- a protected name was translated or removed.

The goal is **one source sentence to one translated sentence**. The validator
never merges or splits records.

## Placeholders and protected names

Before segmentation, booktx hides non-translatable spans behind stable tokens
so the translator never sees them, and restores them verbatim during build:

```text
Alice           -> __NAME_001__        (from names.json#protected_terms)
Mr. Smith       -> __NAME_002__
inline code     -> __TAG_001__         (markdown: `code`; epub: <code>...</code>)
link URL        -> __TAG_002__         (markdown: (url); epub: <a href="url">...</a>)
```

Edit `.booktx/names.json` to add names, brands, or places that must survive
translation untouched:

```json
{
  "protected_terms": ["Alice", "Mr. Smith", "Baker Street"]
}
```

The agent **must** preserve every `__NAME_NNN__` and `__TAG_NNN__` token
exactly. `booktx build` restores the originals after validation.

## Markdown handling

- Translate prose text only.
- Do not translate fenced code blocks, inline code, URLs, or YAML front-matter
  **keys** (front-matter values are prose).
- Preserve headings, lists, blockquotes, links, emphasis, and tables.

booktx replaces each extracted prose span with an internal placeholder and
reinserts the translated text during build.

## EPUB handling

- Read with `EbookLib`; process only XHTML spine documents.
- Preserve images, CSS, metadata, and reading order.
- Translate visible text nodes only.
- Do not translate scripts, styles, identifiers, filenames, CSS, or image alt
  text.
- Build writes a **new** EPUB; the original is never modified.

Inline markup such as `<strong>`, `<em>`, and `<a>` is preserved through
open/close `__TAG_NNN__` tokens, so the inner text is translated while the tags
and attributes (e.g. `href`) survive unchanged. Inline `<code>`, `<kbd>`,
`<samp>`, and `<var>` are opaque — their text is kept verbatim.

## End-to-end example (Markdown)

```bash
booktx init ./demo --target de
cp book.md ./demo/source/
booktx extract ./demo
booktx next ./demo              # -> 0001   .booktx/chunks/0001.json

# Fill in .booktx/translated/0001.json (see the contract above), then:

booktx validate ./demo
booktx build ./demo            # -> demo/output/book.de.md
```

## End-to-end example (EPUB)

```bash
booktx init ./demo --target de --source-file book.epub
booktx extract ./demo
booktx next ./demo

# Fill in every .booktx/translated/*.json, then:

booktx validate ./demo
booktx build ./demo            # -> demo/output/book.de.epub
```

## What v1 does NOT do

PDF, DOCX, AsciiDoc, a web UI, direct OpenAI/Anthropic/Ollama API calls, DRM
handling, automatic publishing, translation memory, or parallel agent
execution. The CLI itself performs no translation. v1 is intentionally small
and deterministic.

## Development

```bash
pip install -e '.[dev]'
pytest -q
ruff check .
```

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
