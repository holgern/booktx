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

The intended gold standard is that identity/no-op EPUB builds preserve the extracted source EPUB bytes. Tests cover no-translation and identity-translation paths.

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

1. **navigation entries** from `epub2text` (preferred when complete)
2. **heading tags** (`h1` through `h6`) that extend a numbered sequence
3. **TOC-derived document starts**: when navigation is partial and a visible
   contents page links to an extracted XHTML document, the first span of that
   document becomes a chapter boundary
4. a single chapter covering the whole record stream (last resort)

Detection no longer trusts partial navigation blindly. When navigation is a
strict subset of a strongly chapter-like heading sequence, headings complete
the map. TOC-derived boundaries are only used for documents that were
actually extracted, so a truncated/preview EPUB never produces empty chapter
entries.

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
