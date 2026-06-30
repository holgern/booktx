---
schema_version: 2
object_type: release_entry
versioning:
  schema_version: 1
  revision: 1
entry_id: entry-0004
release_version: v0.3.1
kind: added
summary:
  Added booktx chapters detection with --audit comparing the EPUB table of
  contents against extracted chapter spans and the chapter map
status: accepted
audience: null
scopes: []
source_refs:
  - git:9676d56d78c0a5aba33a19781984f015eb18a2a8
  - git:872394a1dfa66d84f88c2e0cc650044d42af4481
paths:
  - booktx/epub_toc_audit.py
  - booktx/chapters.py
  - booktx/status.py
  - docs/commands.md
  - docs/epub.md
issues: []
prs: []
sources: []
breaking: false
internal: false
order: 4
---
