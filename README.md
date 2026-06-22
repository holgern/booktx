# booktx

`booktx` is a deterministic command-line tool that prepares **Markdown** and
**EPUB** documents for translation by a coding agent (or a human translator).
It does the mechanical bookkeeping — extract translatable sentences, report
progress, hand out the next safe work unit, validate submissions, rebuild the
document — and leaves the actual translation to you or your agent.

**booktx never translates text itself** and makes no LLM or network calls. All
translation text comes from local CLI submissions that `booktx` stores and validates.

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
    manifest.json  # source digest + EPUB extraction/rebuild metadata
    names.json     # manually protected verbatim terms (names, brands, places)
    context.json   # authoritative style/glossary/questions context
    context.md     # rendered context that agents must read before translating
    chapter-map.json # detected chapter -> chunk ranges (additive metadata)
    chunks/        # 0001.json, 0002.json ... (booktx writes these)
    translation-store.json # primary record-level translation state
    tasks/         # persisted work items created by booktx translate next
    translated/    # compatibility/export chunk JSON managed by booktx
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
booktx status ./book                           # report translation progress
booktx chapters ./book                         # list detected chapter ranges
booktx translate next ./book --unit batch --max-words 700 --format block
booktx translate insert ./book --stdin --format block
booktx translate import-legacy ./book          # import valid translated/*.json
booktx translate export ./book                 # export full translated chunks
booktx next ./book                             # legacy next-chunk summary
booktx next ./book --unit chapter              # legacy next-chapter summary
booktx next-chapter ./book                     # chapter workflow shortcut
booktx validate ./book                         # enforce contract + context lint
booktx build ./book                            # rebuild output/book.<target>.<ext>
booktx build ./book --require-complete         # fail on any missing/invalid record
```

`booktx translate next` refuses to return translation work until `.booktx/context.json`
exists and has `ready: true`. Use `--allow-missing-context` only for legacy
workflows and tests that deliberately bypass the context gate.

`booktx status` reports record-, chunk-, chapter-, and word-level progress.
`booktx translate next` returns the next paragraph, batch, chunk, or chapter
task together with a task id and submit hint. `booktx translate insert`
validates each submitted record before writing `translation-store.json`.

`booktx next --unit chapter` and `booktx next-chapter` remain available as
legacy summaries, but they now point agents at `booktx translate next` and
`booktx translate insert`. `booktx chapters` writes `.booktx/chapter-map.json`
and lists detected chapter ranges.

`booktx context init --non-interactive` creates a not-ready context with open
questions and a seed glossary. Required questions must be answered before
`booktx context mark-ready` succeeds. `context.md` is generated from
`context.json`; the JSON file is authoritative.

`booktx extract` is **idempotent**: it rebuilds `chunks/` on every run but
leaves both `translation-store.json` and compatibility `translated/` files
untouched, so re-extracting after editing the source never destroys work in
progress. Stale `translated/*.json` files whose chunk no longer exists are kept
and reported as warnings.

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

`booktx translate next` creates a durable `.booktx/ingest/TASK.block.txt` template for block-text submissions and keeps `.booktx/ingest/TASK.json` for compatibility tooling. Prefer `booktx translate insert --stdin --format block` for normal agent work, or submit the durable `.block.txt` file when you want the payload version-controlled. `booktx translate insert --json-file .booktx/ingest/TASK.json` remains available for JSON-based tooling. When you need compatibility chunk files,
`booktx translate export` materializes `.booktx/translated/NNNN.json` from the
accepted store entries:

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
- a placeholder (`__NAME_NNN__` and any visible legacy `__TAG_NNN__`) was removed, changed, or added;
- a protected name was translated or removed.

The goal is **one source sentence to one translated sentence**. The validator
never merges or splits records.

## Placeholders and protected names

Before segmentation, booktx hides protected spans behind stable tokens and
restores them during build:

```text
Alice           -> __NAME_001__        (from names.json#protected_terms)
Mr. Smith       -> __NAME_002__
inline code     -> __TAG_001__         (markdown / legacy EPUB chunks)
link URL        -> __TAG_002__         (markdown / legacy EPUB chunks)
```

Edit `.booktx/names.json` to add names, brands, or places that must survive
translation untouched:

```json
{
  "protected_terms": ["Alice", "Mr. Smith", "Baker Street"]
}
```

The agent **must** preserve every visible `__NAME_NNN__` token and every visible
legacy `__TAG_NNN__` token exactly. New EPUB extraction no longer emits TAG
tokens; if they appear in freshly extracted EPUB chunks, treat that as a
maintenance defect and re-run extraction after upgrading.

## Markdown handling

- Translate prose text only.
- Do not translate fenced code blocks, inline code, URLs, or YAML front-matter
  **keys** (front-matter values are prose).
- Preserve headings, lists, blockquotes, links, emphasis, and tables.

booktx replaces each extracted prose span with an internal placeholder and
reinserts the translated text during build.

## EPUB handling

- Extract with `epub2text`; rebuild with `text2epub`.
- New EPUB chunks contain clean block text plus `__NAME_NNN__` placeholders only.
- `booktx build` uses the stored `.booktx/manifest.json` extraction data as the
  source of truth and fails if the source EPUB SHA changed after extract.
- Identity/no-op EPUB builds are byte-identical to the extracted source EPUB.
- Changed EPUB blocks rebuild as valid EPUB with no leaked internal tokens.
- The original source EPUB is never modified.

### EPUB MVP inline-formatting tradeoff

The current EPUB rebuild path replaces changed blocks with escaped translated
text for the whole block body. That means identity/no-op builds preserve
everything byte-for-byte, but changed blocks may lose inner inline markup such
as `<strong>` or `<em>` until a future text-run-preserving replacement mode is
added.

### EPUB migration note

Existing EPUB projects extracted with the legacy TAG-placeholder pipeline should
be re-extracted after upgrading:

```bash
booktx extract ./project
```

Do not mix old EPUB chunks containing `__TAG_NNN__` with the new manifest v2
pipeline.

## End-to-end example (Markdown)

```bash
booktx init ./demo --target de
cp book.md ./demo/source/
booktx extract ./demo
booktx context init ./demo --non-interactive
booktx context mark-ready ./demo --force
booktx translate next ./demo --unit batch --max-words 700 --format block

# Submit the returned records through the CLI, then:

booktx validate ./demo
booktx build ./demo            # -> demo/output/book.de.md
```

## End-to-end example (EPUB)

```bash
booktx init ./demo --target de --source-file book.epub
booktx extract ./demo
booktx context init ./demo --non-interactive
booktx context mark-ready ./demo --force
booktx translate next ./demo --chapter 0001 --unit batch --max-words 700 --format block

# Submit translated records with booktx translate insert, then:

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
