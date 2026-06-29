# mypy per-code baseline (Phase 0)

This document records every per-code `# type: ignore[...]` baseline added in
Phase 0 of the booktx refactor (`booktx_refactor_review_recreated.md`).

Policy (user-approved, Q2=a): fix mypy errors that flag real defects; baseline
the remainder per-code with a targeted `# type: ignore[code]` and a short
justification. **Global mypy strict config is unchanged.** The goal of this
file is to make each remaining ignore auditable and removable once the
underlying annotation/env gap is closed.

`python -m mypy booktx` exits 0 against this baseline.

## Real-defect fixes (no ignore needed)

These were genuine type/logic bugs, fixed in Phase 0:

| Location                                                    | Was                                                                                              | Fix                                                              |
| ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `booktx/command_hints.py`                                   | `GLOSSARY_WORKFLOW_RULES: list[str]` holding `tuple[int, str]` items                             | Annotated `list[tuple[int, str]]`                                |
| `booktx/epub_output_policy.py` `_scan_one`                  | `seen: set[str]` with `(name, declaration)` tuple keys → dedup never matched                     | `set[tuple[str, str]]`                                           |
| `booktx/editor_indexes.py` `_write_jsonl_index`             | Iterated a `dict` (yields keys) → JSONL alias files were empty                                   | Accept `list                                                     | Mapping`, iterate `.values()`                                              |
| `booktx/cli.py` `translation_search_cmd`                    | `effective_target_candidate(...).target` dereferenced a possibly-`None` candidate twice          | Extracted `_neighbor_target` helper binding the local            |
| `booktx/cli.py` `translation_search_cmd`                    | `source_view.chapter_id` — `SourceRecordView` has no such field                                  | Use `bundle.index.record_to_chapter.get(...)`                    |
| `booktx/epub_toc_audit.py`                                  | `sorted(set[int                                                                                  | None])` after a separate filter comprehension mypy cannot narrow | Single comprehension with inline `if o is not None` (heading/nav ordinals) |
| `booktx/cli.py` `review_configure`                          | `write_profile_config` 3-arg call (signature takes 2)                                            | `write_profile_config(proj.root, cfg)`                           |
| `booktx/cli.py` `review_revise_record`                      | `validate_record_pair` imported from wrong module (`translation_store`)                          | Removed; uses module-level import from `booktx.validate`         |
| `booktx/review_todo.py`                                     | `quality_cfg.passes_by_number` (nonexistent attr); store never loaded (`bundle.project` missing) | Real `passes` lookup + thread `Project` through selection        |
| `booktx/cli.py` epub cmds                                   | `proj.paths.output_dir` (`Project` has no `.paths`)                                              | `proj.output_dir`                                                |
| `booktx/build.py`, `booktx/epub_io.py`, `booktx/html_io.py` | Stale `# type: ignore` comments (underlying types now available)                                 | Removed                                                          |

## Per-code baselines

Each entry below is a deliberate, justified per-code ignore. They should be
removed when the listed follow-up is done.

| Location                                                         | Code               | Reason / follow-up                                                                                                                            |
| ---------------------------------------------------------------- | ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------- |
| `booktx/config.py:70` `import tomli as tomllib`                  | `import-not-found` | `tomli` ships no type stubs; used only as the Python 3.10 fallback for stdlib `tomllib`. Remove once minimum Python is 3.11+.                 |
| `booktx/html_io.py:25` `from bs4 import ... NavigableString ...` | `attr-defined`     | `bs4` stubs do not re-export `NavigableString` explicitly. Remove when bs4 stubs improve.                                                     |
| `booktx/build.py:384` `_policy_report_dict`                      | `no-untyped-def`   | Internal build-report serializer; params span text2epub/booktx boundary objects. Add precise param types when the build module is decomposed. |
| `booktx/epub_output_policy.py:406` `to_text2epub_output_rewrite` | `no-untyped-def`   | Return type is a lazily-imported `text2epub.OutputRewriteOptions`. Annotate when text2epub types are imported at module level.                |
| `booktx/epub_toc_audit.py:374` `_load_template`                  | `no-untyped-def`   | Returns `EpubTemplateData                                                                                                                     | None` from a lazy manifest loader. |
| `booktx/epub_toc_audit.py:387` `_collect_toc_entries`            | `no-untyped-def`   | Returns `list[EpubTocEntry]`.                                                                                                                 |
| `booktx/epub_toc_audit.py:466` `audit_epub_chapter_map`          | `no-untyped-def`   | `chapter_map: ChapterMap                                                                                                                      | None` (lazy import).               |
| `booktx/review_todo.py:628` `resume_review_todo`                 | `no-untyped-def`   | Returns a review task/workflow object; annotate return type in Phase 2 (review-todo repair).                                                  |
| `booktx/validate.py:1608` `_soft_hyphen_findings`                | `no-untyped-def`   | `effective` is an `EffectiveTranslatedChunks` bundle; annotate in Phase 4 (effective-translation extraction).                                 |

## Remaining `no-untyped-def` strategy

The `no-untyped-def` ignores above are intentionally concentrated in modules
scheduled for deeper refactoring in later phases (`validate.py` → Phase 4,
`review_todo.py` → Phase 2). They will be replaced with precise annotations as
those modules are decomposed and their internal types are extracted into
public names, rather than bolted on now against unstable internals.
