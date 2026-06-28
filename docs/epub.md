# EPUB handling

EPUB support uses two external libraries through booktx adapters:

- `epub2text` for structured extraction
- `text2epub` for rebuild

The adapter code lives mainly in:

- `booktx.epub_io`
- `booktx.epub_manifest`
- `booktx.build`

## Extraction flow

`booktx extract` calls `extract_epub()`.

The extraction policy requests:

- raw documents
- character offsets
- inline runs
- no duplicate-title removal
- no nav document text as translatable prose
- no pre-segmentation by `epub2text`

booktx then:

1. maps structured blocks back to raw XHTML offsets
2. protects configured names
3. stores ordered span references
4. converts structured extraction data into a `text2epub` manifest
5. writes `.booktx/source-manifest.json`

## Fresh EPUB chunk rule

New EPUB chunks should contain clean block text and `__NAME_NNN__` placeholders only.

Fresh EPUB extraction must not emit:

```text
__TAG_NNN__
__SPANTX_NNNN__
```

If these appear in fresh EPUB chunks, extraction is considered defective. Legacy EPUB projects should be re-extracted after upgrading.

## Manifest v2

EPUB rebuild uses `.booktx/source-manifest.json` version 2.

The manifest stores:

- source filename
- source format
- source and target languages
- source SHA256
- chunk count
- record count
- EPUB template data
- `text2epub` extraction manifest
- span references
- navigation references

Build rejects legacy EPUB manifests and asks the user to rerun extraction.

## Source checksum

EPUB build verifies that the current source EPUB matches the checksum recorded at extraction time.

If the source changed, rebuild fails. Re-run extraction after intentional source changes.

## Identity build

The intended gold standard is that pass-through EPUB builds preserve the
extracted source EPUB bytes by default. Pass-through profiles resolve to a
fully preserving EPUB output policy and so remain byte-identical.

Translation profiles, by contrast, apply an EPUB output policy that rewrites
publication/content language and injects a deterministic hyphenation style
sheet, so a translated build is **not** expected to be byte-identical to the
source. See _EPUB output-language and hyphenation policy_ below.

## EPUB output-language and hyphenation policy

A translated EPUB build writes the resolved target language to the primary
OPF `dc:language` and to the `lang`/`xml:lang` of targeted XHTML root
elements, and injects one deterministic best-effort hyphenation/word-break
style sheet into eligible reflowable documents. Descendant language
annotations (for example a quoted passage in another language) are preserved.

This is a **metadata and author-style correctness** contract, not a promise
of identical rendering across reading systems. Automatic hyphenation depends
on the reader and its installed dictionaries; booktx cannot control computed
style. CSS cascade conflicts (source `!important`, higher-specificity rules,
or reader styles) are reported as warnings because they can override the
injected policy.

Defaults depend on profile kind:

- translation and legacy translation projects default to `target` language
  and `auto` hyphenation;
- pass-through profiles default to `preserve`/`preserve` (byte-identical
  output).

Override the policy explicitly under `[epub_output]` in the profile (or
legacy) config:

```toml
[epub_output]
language_policy = "target"
language = "de-DE"        # required only when language_policy = "explicit"
hyphenation = "auto"      # auto | manual | none | preserve
inject_css = true
patch_body_language = false
```

`hyphenation = "none"` is the compatibility escape hatch: it disables the
generated automatic hyphenation when a reader keeps producing bad breaks.

The build is transactional: a failed policy resolution, rebuild, or audit
leaves the last successful output untouched. The build report keeps the
existing top-level keys (`changed_entries`, `replacement_count`,
`unresolved_token_count`) and adds an `epub_output_policy` object with the
resolved language, hyphenation mode, patched XHTML count, and warning count.

## Reconstruction validation

To verify that extraction and reconstruction include all content, create a
pass-through profile that rebuilds the EPUB from source-as-target chunks:

```bash
booktx extract ./book
booktx pass-through ./book --profile passthrough_en --create
```

Then compare the source and rebuilt output byte-for-byte (for the fixture) or
with an EPUB diff viewer (for real books):

```text
source/book.epub
translations/passthrough_en/output/book.en.epub
```

## Changed block tradeoff

The current EPUB rebuild path replaces changed blocks with escaped translated text for the whole block body.

This preserves identity builds, but changed blocks can lose inner inline markup such as `<strong>` or `<em>` until a future text-run-preserving replacement mode exists.

## Chapter detection

EPUB chapter detection combines several signals rather than trusting a single
source:

1. **upstream `epub2text` block chapter annotations** — the authoritative
   source for manifests marked `chapter_mapping="epub2text-block-v1"`. New
   extractions persist `TextBlock.chapter_id` / `chapter_title` onto each
   span ref and use them even when the set is empty (an authoritative "no
   assignment" result).
2. **heading tags** (`h1` through `h6`) that extend a numbered sequence
3. **TOC-derived document starts**: when a boundary source is partial and a
   visible contents page links to an extracted XHTML document, the first span
   of that document becomes a chapter boundary
4. a single chapter covering the whole record stream (last resort)

Boundaries are resolved through canonical `Record.span_index` metadata, so a
multi-sentence block never shifts a chapter start. Old manifests without
block annotations (`chapter_mapping="legacy"`) fall back to a conservative
navigation mapper that ignores fallback/unresolved navigation entries and
offsets beyond extracted spans; re-extraction is required to gain upstream
annotations. Detection no longer trusts partial navigation blindly: when a
boundary source is a strict subset of a strongly chapter-like heading
sequence, headings complete the map. TOC-derived boundaries are only used
for documents that were actually extracted, so a truncated/preview EPUB
never produces empty chapter entries. Relative visible-TOC hrefs are resolved
against their containing XHTML document (no same-basename collapse).

### Chapter completeness audit

A visible contents page can promise more chapters than were extracted or
detected (for example a preview EPUB, a skipped spine document, or partial
navigation). The audit compares the visible TOC against extracted spans,
navigation, and the chapter map:

```bash
booktx chapters ./book --audit
booktx chapters ./book --audit --json
```

The audit writes `.booktx/reports/chapter-audit.json` and is also surfaced by
`booktx validate` and `booktx check` for unscoped EPUB runs. Deterministic
finding codes and severities:

- `error epub_toc_href_extracted_but_unmapped`: the TOC target has extracted
  spans but no chapter boundary covers it.
- `warning epub_toc_chapter_missing_from_map`: a numbered TOC entry is not in
  the chapter map.
- `warning epub_toc_href_missing_from_extracted_spans`: the TOC target has no
  extracted span (truncated/preview EPUB or extraction skip).
- `warning epub_navigation_partial`: navigation covers fewer numbered chapters
  than visible chapter signals.
- `warning epub_chapter_sequence_gap`: numbered TOC chapters have gaps.

The chapter map and audit are generated automatically during `booktx extract`:
`extract` writes `.booktx/chapter-map.json`, runs the audit, writes
`.booktx/reports/chapter-audit.json`, and prints a one-line warning with the
`booktx chapters . --audit` hint when findings exist. Extraction stays
successful for warning-only preview/truncation cases (it is a completeness
signal, not a policy gate). The chapter-map algorithm is versioned
(`ChapterMap.version`); a cached map whose version is older than the current
algorithm is regenerated even when the source SHA is unchanged.

`booktx status` recomputes the audit summary for the current source rather
than trusting the persisted report, and shows it when findings exist. New
chapter/task/todo selection (`next`, `next-chapter`, `translate next --chapter`, todo creation) refuses to create new work only on `error` audit
findings (for example `epub_toc_href_extracted_but_unmapped`); warning-only
findings remain visible but non-blocking. This keeps the three counts distinct:

- **visible-TOC count** — chapters promised by the contents page (audit only).
- **extracted-spine documents** — XHTML documents actually present in the spine.
- **chapter-map count** — chapters booktx will translate.

If the chapter-map count is lower than the visible-TOC count, inspect the
source rather than trusting the contents page:

```bash
booktx chapters . --audit
booktx epub inspect .
```

### Re-extraction for upstream annotations

Projects extracted before `chapter_mapping="epub2text-block-v1"` use the
conservative legacy navigation fallback and do not carry upstream block
annotations. Re-extract to gain them:

```bash
booktx extract .
```

## Common EPUB errors

### Legacy manifest

Message:

```text
This project uses the legacy EPUB extraction format. Re-run `booktx extract` after upgrading.
```

Fix:

```bash
booktx extract .
```

### Source checksum mismatch

The source EPUB bytes differ from the extraction manifest. Restore the original source or re-extract.

### Unresolved placeholder in output EPUB

A target likely omitted or changed a placeholder token. Run validation, repair the translated chunk, and rebuild.

## Inline XHTML semantics

EPUB extraction exposes inline XHTML fragments in record `source` values when the source block contains inline semantics. EPUB records use `source_markup="epub-inline-xhtml:v1"`. Legacy plain records continue to load as `plain:v1`.

During rebuild, changed EPUB targets are parsed and sanitized as constrained inline XHTML before `text2epub` receives `allow_inline_xhtml=True`. Identity/pass-through output uses the plain expected source so reconstruction checks remain useful.
