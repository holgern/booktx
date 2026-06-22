"""Project configuration and ``.booktx/`` directory management.

A *project* is a directory laid out like this (see
``booktx_coding_agent_start.md``)::

    book/
      source/
        book.md        # or book.epub; exactly one source file
      .booktx/
        config.toml
        manifest.json
        names.json
        chunks/
        translated/
        ingest/
        reports/
      output/

This module owns reading/writing those files and resolving the project root,
the single source file, and the active :class:`~booktx.models.ProjectConfig`.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomli_w

try:
    import tomllib  # Python 3.11+ stdlib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from booktx.epub_manifest import sha256_path
from booktx.models import (
    Manifest,
    NamesFile,
    ProjectConfig,
    TranslationStore,
    TranslationTask,
)

__all__ = [
    "SUPPORTED_SOURCE_SUFFIXES",
    "BooktxError",
    "Project",
    "init_project",
    "load_project",
    "detect_format",
    "write_manifest",
    "write_names",
    "load_names",
    "project_source_sha256",
    "translation_store_path",
    "load_translation_store",
    "write_translation_store",
    "translation_task_dir",
    "translation_task_path",
    "translation_ingest_dir",
    "translation_ingest_path",
    "translation_ingest_block_path",
    "load_translation_task",
    "write_translation_task",
]

#: Filename suffixes booktx understands in v1, mapped to format names.
SUPPORTED_SOURCE_SUFFIXES: dict[str, str] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".epub": "epub",
}

#: Default protected-terms file written by ``booktx init``.
DEFAULT_NAMES_JSON: dict[str, Any] = {"protected_terms": []}


class BooktxError(Exception):
    """User-facing error from booktx. Carries a stable ``code`` attribute."""


def _err(code: str, message: str) -> BooktxError:
    e = BooktxError(message)
    e.code = code  # type: ignore[attr-defined]
    return e


@dataclass(slots=True)
class Project:
    """Resolved paths for a booktx project."""

    root: Path
    source_dir: Path
    booktx_dir: Path
    config_path: Path
    manifest_path: Path
    names_path: Path
    chunks_dir: Path
    translated_dir: Path
    tasks_dir: Path
    ingest_dir: Path
    reports_dir: Path
    output_dir: Path
    config: ProjectConfig

    @property
    def source_path(self) -> Path:
        """Full path to the single source document."""
        return self.source_dir / self.config.source_file

    def chunks(self) -> list[Path]:
        return sorted(self.chunks_dir.glob("*.json"))

    def translated(self) -> list[Path]:
        return sorted(self.translated_dir.glob("*.json"))

    def chunk_ids(self) -> list[str]:
        return [p.stem for p in self.chunks()]

    def translated_ids(self) -> list[str]:
        return [p.stem for p in self.translated()]


def detect_format(filename: str | Path) -> str:
    """Return the booktx format name for a filename, or raise."""
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_SOURCE_SUFFIXES:
        raise _err(
            "unsupported_format",
            f"Unsupported source format '{suffix or '<none>'}'. "
            "booktx v1 supports only .md and .epub.",
        )
    return SUPPORTED_SOURCE_SUFFIXES[suffix]


def init_project(
    target: Path,
    *,
    target_language: str,
    source_language: str = "en",
    source_file: Path | str | None = None,
    chunk_size: int = 50,
) -> Project:
    """Create the project layout and a starter config at ``target``.

    If ``source_file`` is given, it is copied into ``target/source/`` and its
    format is detected from the suffix. If it is omitted, an empty
    ``source/`` directory is created and the user is expected to drop a file in.
    """
    root = Path(target).expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise _err("not_a_directory", f"{root} exists and is not a directory.")
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)

    source_dir = root / "source"
    booktx_dir = root / ".booktx"
    chunks_dir = booktx_dir / "chunks"
    translated_dir = booktx_dir / "translated"
    tasks_dir = booktx_dir / "tasks"
    ingest_dir = booktx_dir / "ingest"
    reports_dir = booktx_dir / "reports"
    output_dir = root / "output"
    for d in (
        source_dir,
        booktx_dir,
        chunks_dir,
        translated_dir,
        tasks_dir,
        ingest_dir,
        reports_dir,
        output_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)

    rel_source_name: str | None = None
    fmt: str | None = None
    if source_file is not None:
        src = Path(source_file).expanduser().resolve()
        if not src.is_file():
            raise _err("source_not_found", f"Source file not found: {src}")
        fmt = detect_format(src.name)
        dest = source_dir / src.name
        if src.resolve() != dest.resolve():
            dest.write_bytes(src.read_bytes())
        rel_source_name = src.name

    if rel_source_name is None:
        # No source supplied yet; defer until extract. We still need *some*
        # value for the config, but leave it empty and let load/extract fail
        # loudly if it stays empty.
        rel_source_name = ""
        fmt = "markdown"  # placeholder; re-detected at extract time

    cfg = ProjectConfig(
        source_language=source_language,
        target_language=target_language,
        source_file=rel_source_name,
        format=fmt,
        chunk_size=chunk_size,
    )
    _write_config(root / ".booktx" / "config.toml", cfg)
    # names.json: empty but present so the agent knows where to add terms.
    (booktx_dir / "names.json").write_text(
        json.dumps(DEFAULT_NAMES_JSON, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return load_project(root)


def load_project(root: Path | str) -> Project:
    """Load an existing project, validating its layout."""
    r = Path(root).expanduser().resolve()
    booktx_dir = r / ".booktx"
    config_path = booktx_dir / "config.toml"
    if not config_path.is_file():
        raise _err(
            "not_a_project",
            f"{r} is not a booktx project: missing {config_path}.",
        )
    cfg = _read_config(config_path)

    return Project(
        root=r,
        source_dir=r / "source",
        booktx_dir=booktx_dir,
        config_path=config_path,
        manifest_path=booktx_dir / "manifest.json",
        names_path=booktx_dir / "names.json",
        chunks_dir=booktx_dir / "chunks",
        translated_dir=booktx_dir / "translated",
        tasks_dir=booktx_dir / "tasks",
        ingest_dir=booktx_dir / "ingest",
        reports_dir=booktx_dir / "reports",
        output_dir=r / "output",
        config=cfg,
    )


def _read_config(path: Path) -> ProjectConfig:
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return ProjectConfig.model_validate(data)


def _write_config(path: Path, cfg: ProjectConfig) -> None:
    data = cfg.model_dump(mode="json")
    # tomli_w does not accept ints-as-strings; model_dump(mode="json") is fine.
    path.write_bytes(tomli_w.dumps(data).encode("utf-8"))


def write_manifest(project: Project, manifest: Manifest) -> None:
    project.manifest_path.write_text(
        manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )


def load_manifest(project: Project) -> Manifest | None:
    if not project.manifest_path.is_file():
        return None
    return Manifest.model_validate_json(project.manifest_path.read_text("utf-8"))


def write_names(project: Project, names: NamesFile) -> None:
    project.names_path.write_text(
        json.dumps(names.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def load_names(project: Project) -> NamesFile:
    if not project.names_path.is_file():
        return NamesFile()
    try:
        return NamesFile.model_validate_json(project.names_path.read_text("utf-8"))
    except Exception as exc:  # noqa: BLE001 - surface as BooktxError
        raise _err("bad_names_json", f"names.json is invalid: {exc}") from exc


def project_source_sha256(project: Project) -> str:
    """Return the current source SHA, preferring the manifest when present."""
    manifest = load_manifest(project)
    if manifest is not None and manifest.source.sha256:
        return manifest.source.sha256
    return sha256_path(find_source_file(project))


def translation_store_path(project: Project) -> Path:
    """Path to the primary record-level translation store."""
    return project.booktx_dir / "translation-store.json"


def load_translation_store(project: Project) -> TranslationStore:
    """Load the translation store, or return an empty store when absent."""
    path = translation_store_path(project)
    if not path.is_file():
        return TranslationStore()
    return TranslationStore.model_validate_json(path.read_text("utf-8"))


def write_translation_store(project: Project, store: TranslationStore) -> None:
    """Persist the translation store atomically."""
    _write_json_atomic(
        translation_store_path(project),
        store.model_dump_json(indent=2) + "\n",
    )


def translation_task_dir(project: Project) -> Path:
    """Directory holding persisted translation tasks."""
    return project.tasks_dir


def translation_task_path(project: Project, task_id: str) -> Path:
    """Path for one persisted translation task."""
    return translation_task_dir(project) / f"{task_id}.json"


def translation_ingest_dir(project: Project) -> Path:
    """Directory holding user/agent-authored translation submissions."""
    return project.ingest_dir


def translation_ingest_path(project: Project, task_id: str) -> Path:
    """Path for the durable JSON submission file for one translation task."""
    safe_task_id = Path(task_id).name
    if safe_task_id != task_id or not safe_task_id:
        raise _err("invalid_task_id", f"Invalid task id for ingest path: {task_id!r}")
    return translation_ingest_dir(project) / f"{safe_task_id}.json"


def translation_ingest_block_path(project: Project, task_id: str) -> Path:
    """Path for the durable block-text submission file for one translation task."""
    safe_task_id = Path(task_id).name
    if safe_task_id != task_id or not safe_task_id:
        raise _err("invalid_task_id", f"Invalid task id for ingest path: {task_id!r}")
    return translation_ingest_dir(project) / f"{safe_task_id}.block.txt"


def load_translation_task(project: Project, task_id: str) -> TranslationTask | None:
    """Load one translation task, or return ``None`` when missing."""
    path = translation_task_path(project, task_id)
    if not path.is_file():
        return None
    return TranslationTask.model_validate_json(path.read_text("utf-8"))


def write_translation_task(project: Project, task: TranslationTask) -> None:
    """Persist one translation task atomically."""
    _write_json_atomic(
        translation_task_path(project, task.task_id),
        task.model_dump_json(indent=2) + "\n",
    )


def find_source_file(project: Project) -> Path:
    """Resolve the single source document, or raise a clear error.

    Rules:
    - If config.source_file is set and the file exists, use it.
    - Otherwise scan ``source/`` for exactly one supported file.
    - Zero or >1 candidate is a hard error.
    """
    configured = project.config.source_file.strip()
    if configured:
        candidate = project.source_dir / configured
        if candidate.is_file():
            return candidate
        # Fall through to scan; the configured name may be stale.

    candidates = [
        p
        for p in sorted(project.source_dir.iterdir())
        if p.is_file() and p.suffix.lower() in SUPPORTED_SOURCE_SUFFIXES
    ]
    if not candidates:
        raise _err(
            "no_source",
            f"No source document found in {project.source_dir}. "
            "Drop a .md or .epub file into source/.",
        )
    if len(candidates) > 1:
        names = ", ".join(p.name for p in candidates)
        raise _err(
            "ambiguous_source",
            f"Found multiple source documents in {project.source_dir}: {names}. "
            "Keep exactly one.",
        )
    chosen = candidates[0]
    # Keep config in sync with the discovered file.
    if project.config.source_file != chosen.name or (
        project.config.format != detect_format(chosen.name)
    ):
        project.config.source_file = chosen.name
        project.config.format = detect_format(chosen.name)
        _write_config(project.config_path, project.config)
    return chosen


def _write_json_atomic(path: Path, text: str) -> None:
    """Write UTF-8 text atomically into ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as fh:
            fh.write(text)
            tmp_path = Path(fh.name)
        tmp_path.replace(path)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
