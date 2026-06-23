"""Project configuration and path resolution for legacy and profile layouts.

booktx now supports two project shapes:

Legacy single-layout projects:

    book/
      source/
      .booktx/
        config.toml
        manifest.json
        names.json
        chunks/
        context.json
        context.md
        identity.json
        translation-store.json
        translation-version-ledger.json
        tasks/
        ingest/
        translated/
        reports/
      output/

Profile-layout projects:

    book/
      source/
      .booktx/
        source-config.toml
        source-manifest.json
        names.json
        chapter-map.json
        profile-state.json
        chunks/
      translations/
        <profile>/
          config.toml
          identity.json
          context.json
          context.md
          translation-store.json
          translation-version-ledger.json
          tasks/
          ingest/
          translated/
          reports/
          output/

The shared ``.booktx/`` tree is source-derived state only. Mutable translation
state lives under the selected profile.
"""

# ruff: noqa: E501

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

import tomli_w

try:
    import tomllib  # Python 3.11+ stdlib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from booktx.epub_manifest import sha256_path
from booktx.models import (
    Manifest,
    NamesFile,
    ProfileConfig,
    ProfileIdentityConfig,
    ProfileState,
    ProjectConfig,
    SourceConfig,
    TranslationIdentity,
    TranslationStore,
    TranslationStoreV2,
    TranslationTask,
    TranslationVersionLedger,
)

__all__ = [
    "SUPPORTED_SOURCE_SUFFIXES",
    "BooktxError",
    "Project",
    "detect_format",
    "source_config_path",
    "profile_state_path",
    "translations_dir",
    "profile_dir",
    "profile_config_path",
    "list_profiles",
    "load_profile_state",
    "write_profile_state",
    "load_profile_config",
    "write_profile_config",
    "resolve_profile_name",
    "load_project",
    "load_source_project",
    "load_profile_project",
    "init_source_project",
    "init_project",
    "create_profile",
    "select_profile",
    "migrate_current_project",
    "write_manifest",
    "load_manifest",
    "write_names",
    "load_names",
    "protected_terms_sha256",
    "project_source_sha256",
    "current_source_sha256",
    "extracted_source_sha256",
    "translation_store_path",
    "translation_version_ledger_path",
    "identity_path",
    "load_translation_store",
    "load_translation_version_ledger",
    "load_identity",
    "write_translation_store",
    "write_translation_version_ledger",
    "write_identity",
    "translation_task_dir",
    "translation_task_path",
    "translation_task_source_block_path",
    "translation_ingest_dir",
    "translation_ingest_path",
    "translation_ingest_block_path",
    "load_translation_task",
    "write_translation_task",
    "find_source_file",
]

SUPPORTED_SOURCE_SUFFIXES: dict[str, str] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".epub": "epub",
}

DEFAULT_NAMES_JSON: dict[str, Any] = {"protected_terms": []}
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class BooktxError(Exception):
    """User-facing error from booktx. Carries a stable ``code`` attribute."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _err(code: str, message: str) -> BooktxError:
    return BooktxError(code, message)


@dataclass(slots=True)
class Project:
    """Resolved project paths for either the legacy or profile layout."""

    root: Path
    source_dir: Path
    booktx_dir: Path
    translations_dir: Path
    source_config_path: Path
    config_path: Path
    manifest_path: Path
    names_path: Path
    chunks_dir: Path
    chapter_map_path: Path
    profile_state_path: Path
    profile: str | None
    profile_dir: Path | None
    profile_config_path: Path | None
    context_json_path: Path | None
    context_md_path: Path | None
    identity_json_path: Path | None
    store_path: Path | None
    ledger_path: Path | None
    translated_dir: Path | None
    tasks_dir: Path | None
    ingest_dir: Path | None
    reports_dir: Path | None
    output_dir: Path | None
    source_config: SourceConfig
    profile_config: ProfileConfig | None
    config: ProjectConfig
    layout_version: Literal["legacy", "profiles"]

    @property
    def source_path(self) -> Path:
        return self.source_dir / self.config.source_file

    def chunks(self) -> list[Path]:
        return sorted(self.chunks_dir.glob("*.json"))

    def translated(self) -> list[Path]:
        if self.translated_dir is None:
            return []
        return sorted(self.translated_dir.glob("*.json"))

    def chunk_ids(self) -> list[str]:
        return [path.stem for path in self.chunks()]

    def translated_ids(self) -> list[str]:
        return [path.stem for path in self.translated()]


def _root_path(root_or_project: Project | Path | str) -> Path:
    if isinstance(root_or_project, Project):
        return root_or_project.root
    return Path(root_or_project).expanduser().resolve()


def _booktx_dir(root_or_project: Project | Path | str) -> Path:
    return _root_path(root_or_project) / ".booktx"


def _legacy_config_path(root_or_project: Project | Path | str) -> Path:
    return _booktx_dir(root_or_project) / "config.toml"


def source_config_path(root_or_project: Project | Path | str) -> Path:
    return _booktx_dir(root_or_project) / "source-config.toml"


def _legacy_manifest_path(root_or_project: Project | Path | str) -> Path:
    return _booktx_dir(root_or_project) / "manifest.json"


def _source_manifest_path(root_or_project: Project | Path | str) -> Path:
    return _booktx_dir(root_or_project) / "source-manifest.json"


def profile_state_path(root_or_project: Project | Path | str) -> Path:
    return _booktx_dir(root_or_project) / "profile-state.json"


def translations_dir(root_or_project: Project | Path | str) -> Path:
    return _root_path(root_or_project) / "translations"


def profile_dir(root_or_project: Project | Path | str, profile_name: str) -> Path:
    return translations_dir(root_or_project) / profile_name


def profile_config_path(
    root_or_project: Project | Path | str, profile_name: str
) -> Path:
    return profile_dir(root_or_project, profile_name) / "config.toml"


def _profile_context_json_path(
    root_or_project: Project | Path | str, profile_name: str
) -> Path:
    return profile_dir(root_or_project, profile_name) / "context.json"


def _profile_context_md_path(
    root_or_project: Project | Path | str, profile_name: str
) -> Path:
    return profile_dir(root_or_project, profile_name) / "context.md"


def _profile_identity_path(
    root_or_project: Project | Path | str, profile_name: str
) -> Path:
    return profile_dir(root_or_project, profile_name) / "identity.json"


def _profile_store_path(
    root_or_project: Project | Path | str, profile_name: str
) -> Path:
    return profile_dir(root_or_project, profile_name) / "translation-store.json"


def _profile_ledger_path(
    root_or_project: Project | Path | str, profile_name: str
) -> Path:
    return (
        profile_dir(root_or_project, profile_name) / "translation-version-ledger.json"
    )


def _profile_translated_dir(
    root_or_project: Project | Path | str, profile_name: str
) -> Path:
    return profile_dir(root_or_project, profile_name) / "translated"


def _profile_tasks_dir(
    root_or_project: Project | Path | str, profile_name: str
) -> Path:
    return profile_dir(root_or_project, profile_name) / "tasks"


def _profile_ingest_dir(
    root_or_project: Project | Path | str, profile_name: str
) -> Path:
    return profile_dir(root_or_project, profile_name) / "ingest"


def _profile_reports_dir(
    root_or_project: Project | Path | str, profile_name: str
) -> Path:
    return profile_dir(root_or_project, profile_name) / "reports"


def _profile_output_dir(
    root_or_project: Project | Path | str, profile_name: str
) -> Path:
    return profile_dir(root_or_project, profile_name) / "output"


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    if not isinstance(data, dict):
        raise _err("invalid_toml", f"{path} did not contain a TOML object")
    return data


def _write_toml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(tomli_w.dumps(payload).encode("utf-8"))


def _read_legacy_config(path: Path) -> ProjectConfig:
    return ProjectConfig.model_validate(_read_toml(path))


def _write_legacy_config(path: Path, cfg: ProjectConfig) -> None:
    _write_toml(path, cfg.model_dump(mode="json", exclude_none=True))


def _read_source_config(path: Path) -> SourceConfig:
    return SourceConfig.model_validate(_read_toml(path))


def _write_source_config(path: Path, cfg: SourceConfig) -> None:
    _write_toml(path, cfg.model_dump(mode="json", exclude_none=True))


def load_profile_config(
    project_or_root: Project | Path | str, profile_name: str
) -> ProfileConfig:
    validate_profile_name(profile_name)
    path = profile_config_path(project_or_root, profile_name)
    if not path.is_file():
        raise _err(
            "profile_not_found", f"translation profile not found: {profile_name}"
        )
    return ProfileConfig.model_validate(_read_toml(path))


def write_profile_config(
    project_or_root: Project | Path | str, cfg: ProfileConfig
) -> None:
    validate_profile_name(cfg.profile)
    _write_toml(
        profile_config_path(project_or_root, cfg.profile),
        cfg.model_dump(mode="json", exclude_none=True),
    )


def list_profiles(project_or_root: Project | Path | str) -> list[str]:
    root = translations_dir(project_or_root)
    if not root.is_dir():
        return []
    names: list[str] = []
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        if not (path / "config.toml").is_file():
            continue
        names.append(path.name)
    return names


def load_profile_state(project_or_root: Project | Path | str) -> ProfileState:
    path = profile_state_path(project_or_root)
    if not path.is_file():
        return ProfileState()
    return ProfileState.model_validate_json(path.read_text("utf-8"))


def write_profile_state(
    project_or_root: Project | Path | str, state: ProfileState
) -> None:
    from booktx.io_utils import write_json_model_atomic

    write_json_model_atomic(profile_state_path(project_or_root), state)


def detect_format(filename: str | Path) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_SOURCE_SUFFIXES:
        raise _err(
            "unsupported_format",
            f"Unsupported source format '{suffix or '<none>'}'. booktx supports only .md and .epub.",
        )
    return SUPPORTED_SOURCE_SUFFIXES[suffix]


def validate_profile_name(profile_name: str) -> str:
    if not profile_name:
        raise _err("invalid_profile_name", "translation profile name must not be empty")
    path = Path(profile_name)
    if (
        path.is_absolute()
        or path.name != profile_name
        or "/" in profile_name
        or "\\" in profile_name
    ):
        raise _err(
            "invalid_profile_name",
            f"invalid translation profile name: {profile_name!r}",
        )
    if not PROFILE_NAME_RE.fullmatch(profile_name):
        raise _err(
            "invalid_profile_name",
            "profile names may contain only letters, numbers, '.', '_' and '-'",
        )
    return profile_name


def _is_legacy_layout(root: Path) -> bool:
    return (
        _legacy_config_path(root).is_file() and not source_config_path(root).is_file()
    )


def _is_profile_layout(root: Path) -> bool:
    return source_config_path(root).is_file()


def _base_profile_project(root: Path, source_cfg: SourceConfig) -> Project:
    effective = ProjectConfig(
        source_language=source_cfg.source_language,
        source_file=source_cfg.source_file,
        format=source_cfg.format,
        chunk_size=source_cfg.chunk_size,
    )
    return Project(
        root=root,
        source_dir=root / "source",
        booktx_dir=_booktx_dir(root),
        translations_dir=translations_dir(root),
        source_config_path=source_config_path(root),
        config_path=source_config_path(root),
        manifest_path=_source_manifest_path(root),
        names_path=_booktx_dir(root) / "names.json",
        chunks_dir=_booktx_dir(root) / "chunks",
        chapter_map_path=_booktx_dir(root) / "chapter-map.json",
        profile_state_path=profile_state_path(root),
        profile=None,
        profile_dir=None,
        profile_config_path=None,
        context_json_path=None,
        context_md_path=None,
        identity_json_path=None,
        store_path=None,
        ledger_path=None,
        translated_dir=None,
        tasks_dir=None,
        ingest_dir=None,
        reports_dir=None,
        output_dir=None,
        source_config=source_cfg,
        profile_config=None,
        config=effective,
        layout_version="profiles",
    )


def _with_profile(
    base: Project, profile_name: str, profile_cfg: ProfileConfig
) -> Project:
    effective = ProjectConfig(
        source_language=base.source_config.source_language,
        target_language=profile_cfg.target_language,
        target_locale=profile_cfg.target_locale or profile_cfg.target_language,
        output_filename=profile_cfg.output_filename,
        source_file=base.source_config.source_file,
        format=base.source_config.format,
        chunk_size=base.source_config.chunk_size,
    )
    profile_root = profile_dir(base, profile_name)
    return Project(
        root=base.root,
        source_dir=base.source_dir,
        booktx_dir=base.booktx_dir,
        translations_dir=base.translations_dir,
        source_config_path=base.source_config_path,
        config_path=base.config_path,
        manifest_path=base.manifest_path,
        names_path=base.names_path,
        chunks_dir=base.chunks_dir,
        chapter_map_path=base.chapter_map_path,
        profile_state_path=base.profile_state_path,
        profile=profile_name,
        profile_dir=profile_root,
        profile_config_path=profile_config_path(base, profile_name),
        context_json_path=_profile_context_json_path(base, profile_name),
        context_md_path=_profile_context_md_path(base, profile_name),
        identity_json_path=_profile_identity_path(base, profile_name),
        store_path=_profile_store_path(base, profile_name),
        ledger_path=_profile_ledger_path(base, profile_name),
        translated_dir=_profile_translated_dir(base, profile_name),
        tasks_dir=_profile_tasks_dir(base, profile_name),
        ingest_dir=_profile_ingest_dir(base, profile_name),
        reports_dir=_profile_reports_dir(base, profile_name),
        output_dir=_profile_output_dir(base, profile_name),
        source_config=base.source_config,
        profile_config=profile_cfg,
        config=effective,
        layout_version="profiles",
    )


def resolve_profile_name(
    project: Project,
    explicit_profile: str | None,
    *,
    require_profile: bool,
    operation: str = "command",
) -> str | None:
    if project.layout_version == "legacy":
        if explicit_profile:
            raise _err(
                "profile_not_found",
                f"project does not use translation profiles; remove --profile {explicit_profile}",
            )
        return None

    profiles = list_profiles(project)
    if explicit_profile:
        validate_profile_name(explicit_profile)
        if explicit_profile not in profiles:
            raise _err(
                "profile_not_found",
                f"translation profile not found: {explicit_profile}",
            )
        return explicit_profile

    state = load_profile_state(project)
    if state.active_profile:
        if state.active_profile not in profiles:
            raise _err(
                "profile_not_found",
                f"active translation profile not found: {state.active_profile}",
            )
        return state.active_profile

    if len(profiles) == 1:
        return profiles[0]

    if require_profile and len(profiles) > 1:
        raise _err(
            "multiple_profiles_ambiguous",
            "multiple translation profiles exist; pass --profile or run "
            "`booktx profile select PROJECT_DIR PROFILE`",
        )
    if require_profile and not profiles:
        raise _err(
            "no_profiles",
            "no translation profile exists; run "
            "`booktx profile create PROJECT_DIR PROFILE --target LANG`",
        )
    _ = operation
    return None


def load_source_project(root: Path | str) -> Project:
    r = Path(root).expanduser().resolve()
    if _is_profile_layout(r):
        return _base_profile_project(r, _read_source_config(source_config_path(r)))
    if _is_legacy_layout(r):
        return load_project(r)
    raise _err(
        "not_a_project",
        f"{r} is not a booktx project: missing {_legacy_config_path(r)} or {source_config_path(r)}.",
    )


def load_profile_project(root: Path | str, profile: str) -> Project:
    return load_project(root, profile=profile, require_profile=True)


def load_project(
    root: Path | str,
    *,
    profile: str | None = None,
    require_profile: bool = False,
) -> Project:
    r = Path(root).expanduser().resolve()

    if _is_profile_layout(r):
        base = _base_profile_project(r, _read_source_config(source_config_path(r)))
        selected_profile = resolve_profile_name(
            base,
            profile,
            require_profile=require_profile,
        )
        if selected_profile is None:
            return base
        return _with_profile(
            base, selected_profile, load_profile_config(base, selected_profile)
        )

    if _is_legacy_layout(r):
        cfg = _read_legacy_config(_legacy_config_path(r))
        source_cfg = SourceConfig(
            source_language=cfg.source_language,
            source_file=cfg.source_file,
            format=cfg.format,
            chunk_size=cfg.chunk_size,
        )
        return Project(
            root=r,
            source_dir=r / "source",
            booktx_dir=_booktx_dir(r),
            translations_dir=translations_dir(r),
            source_config_path=_legacy_config_path(r),
            config_path=_legacy_config_path(r),
            manifest_path=_legacy_manifest_path(r),
            names_path=_booktx_dir(r) / "names.json",
            chunks_dir=_booktx_dir(r) / "chunks",
            chapter_map_path=_booktx_dir(r) / "chapter-map.json",
            profile_state_path=profile_state_path(r),
            profile=None,
            profile_dir=None,
            profile_config_path=None,
            context_json_path=_booktx_dir(r) / "context.json",
            context_md_path=_booktx_dir(r) / "context.md",
            identity_json_path=_booktx_dir(r) / "identity.json",
            store_path=_booktx_dir(r) / "translation-store.json",
            ledger_path=_booktx_dir(r) / "translation-version-ledger.json",
            translated_dir=_booktx_dir(r) / "translated",
            tasks_dir=_booktx_dir(r) / "tasks",
            ingest_dir=_booktx_dir(r) / "ingest",
            reports_dir=_booktx_dir(r) / "reports",
            output_dir=r / "output",
            source_config=source_cfg,
            profile_config=None,
            config=cfg,
            layout_version="legacy",
        )

    raise _err(
        "not_a_project",
        f"{r} is not a booktx project: missing {_legacy_config_path(r)} or {source_config_path(r)}.",
    )


def init_source_project(
    target: Path,
    *,
    source_language: str = "en",
    source_file: Path | str | None = None,
    chunk_size: int = 50,
) -> Project:
    root = Path(target).expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise _err("not_a_directory", f"{root} exists and is not a directory.")
    root.mkdir(parents=True, exist_ok=True)

    source_dir = root / "source"
    booktx_dir = _booktx_dir(root)
    chunks_dir = booktx_dir / "chunks"
    translations_root = translations_dir(root)
    for path in (source_dir, booktx_dir, chunks_dir, translations_root):
        path.mkdir(parents=True, exist_ok=True)

    rel_source_name = ""
    fmt = "markdown"
    if source_file is not None:
        src = Path(source_file).expanduser().resolve()
        if not src.is_file():
            raise _err("source_not_found", f"Source file not found: {src}")
        fmt = detect_format(src.name)
        dest = source_dir / src.name
        if src.resolve() != dest.resolve():
            dest.write_bytes(src.read_bytes())
        rel_source_name = src.name

    cfg = SourceConfig(
        source_language=source_language,
        source_file=rel_source_name,
        format=fmt,
        chunk_size=chunk_size,
    )
    _write_source_config(source_config_path(root), cfg)

    from booktx.io_utils import write_json_text_atomic

    write_json_text_atomic(
        booktx_dir / "names.json",
        json.dumps(DEFAULT_NAMES_JSON, indent=2, ensure_ascii=False),
    )
    return load_source_project(root)


def _default_profile_name(target_language: str) -> str:
    return f"{target_language}_default"


def _default_output_filename(source_cfg: SourceConfig, target_language: str) -> str:
    source_name = source_cfg.source_file or "book"
    stem = Path(source_name).stem or "book"
    suffix = ".epub" if source_cfg.format == "epub" else ".md"
    return f"{stem}.{target_language}{suffix}"


def _validate_output_filename(
    *,
    output_filename: str,
    source_cfg: SourceConfig,
    target_language: str,
) -> None:
    suffix = ".epub" if source_cfg.format == "epub" else ".md"
    expected_fragment = f".{target_language}{suffix}"
    if not output_filename.endswith(suffix) or expected_fragment not in output_filename:
        raise _err(
            "output_target_mismatch",
            f"output filename {output_filename} does not match target language {target_language}; "
            f"expected a filename like {_default_output_filename(source_cfg, target_language)}",
        )


def create_profile(
    root: Path | str,
    profile_name: str,
    *,
    target_language: str,
    target_locale: str | None = None,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    output_filename: str | None = None,
    select: bool = False,
) -> Project:
    validate_profile_name(profile_name)
    source_project = load_source_project(root)
    if source_project.layout_version == "legacy":
        raise _err(
            "legacy_project_required",
            "project uses the legacy single-layout format; run `booktx profile migrate-current` first",
        )
    existing_dir = profile_dir(source_project, profile_name)
    if existing_dir.exists() and any(existing_dir.iterdir()):
        raise _err(
            "profile_already_exists",
            f"translation profile already exists: {profile_name}",
        )
    existing_cfg = profile_config_path(source_project, profile_name)
    if existing_cfg.is_file():
        raise _err(
            "profile_already_exists",
            f"translation profile already exists: {profile_name}",
        )

    final_output_filename = output_filename or _default_output_filename(
        source_project.source_config, target_language
    )
    _validate_output_filename(
        output_filename=final_output_filename,
        source_cfg=source_project.source_config,
        target_language=target_language,
    )
    identity_cfg = ProfileIdentityConfig(
        actor=actor or "user:unknown",
        harness=harness or "booktx",
        model=model or "human",
    )
    cfg = ProfileConfig(
        profile=profile_name,
        source_language=source_project.source_config.source_language,
        target_language=target_language,
        target_locale=target_locale or target_language,
        output_filename=final_output_filename,
        identity=identity_cfg,
    )
    profile_root = profile_dir(source_project, profile_name)
    for path in (
        profile_root,
        _profile_tasks_dir(source_project, profile_name),
        _profile_ingest_dir(source_project, profile_name),
        _profile_translated_dir(source_project, profile_name),
        _profile_reports_dir(source_project, profile_name),
        _profile_output_dir(source_project, profile_name),
    ):
        path.mkdir(parents=True, exist_ok=True)
    write_profile_config(source_project, cfg)
    write_identity(
        _with_profile(source_project, profile_name, cfg),
        TranslationIdentity(
            actor=identity_cfg.actor,
            harness=identity_cfg.harness,
            model=identity_cfg.model,
        ),
    )
    if select:
        write_profile_state(source_project, ProfileState(active_profile=profile_name))
    return load_profile_project(source_project.root, profile_name)


def select_profile(root: Path | str, profile_name: str) -> Project:
    project = load_source_project(root)
    if project.layout_version == "legacy":
        raise _err(
            "legacy_project_required",
            "project uses the legacy single-layout format; it has no selectable translation profiles",
        )
    validate_profile_name(profile_name)
    if profile_name not in list_profiles(project):
        raise _err(
            "profile_not_found", f"translation profile not found: {profile_name}"
        )
    write_profile_state(project, ProfileState(active_profile=profile_name))
    return load_profile_project(project.root, profile_name)


def _migration_manifest_entries(
    paths: list[tuple[Path, Path]],
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for src, dst in paths:
        entry: dict[str, object] = {
            "source": str(src),
            "destination": str(dst),
            "exists": src.exists(),
        }
        if src.is_file():
            entry["sha256"] = sha256_path(src)
        elif src.is_dir():
            entry["entry_count"] = sum(1 for _ in src.rglob("*"))
        entries.append(entry)
    return entries


def _write_migration_manifest(root: Path, paths: list[tuple[Path, Path]]) -> Path:
    from booktx.io_utils import write_json_text_atomic

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest_path = _booktx_dir(root) / "migrations" / f"{stamp}-profile-migration.json"
    payload = {
        "version": 1,
        "timestamp": stamp,
        "moves": _migration_manifest_entries(paths),
    }
    write_json_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, ensure_ascii=False),
    )
    return manifest_path


def migrate_current_project(
    root: Path | str,
    profile_name: str,
    *,
    target_language: str | None = None,
    target_locale: str | None = None,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    select: bool = False,
    dry_run: bool = False,
) -> dict[str, object]:
    validate_profile_name(profile_name)
    project_root = Path(root).expanduser().resolve()
    if _is_profile_layout(project_root):
        raise _err(
            "already_profile_project",
            "project already uses translation profiles; use `booktx profile create` instead",
        )
    if not _is_legacy_layout(project_root):
        raise _err(
            "legacy_project_required",
            "project does not use the legacy single-layout format",
        )

    legacy_project = load_project(project_root)
    legacy_target = target_language or legacy_project.config.target_language
    if not legacy_target:
        raise _err(
            "profile_config_invalid",
            "legacy project has no target language; pass --target to migrate-current",
        )
    profile_root = profile_dir(project_root, profile_name)
    if profile_root.exists() and any(profile_root.iterdir()):
        raise _err(
            "migration_target_exists",
            f"migration target already exists and is not empty: {profile_root}",
        )

    legacy_identity = load_identity(legacy_project)
    identity_cfg = ProfileIdentityConfig(
        actor=actor
        or (legacy_identity.actor if legacy_identity is not None else "user:unknown"),
        harness=harness
        or (legacy_identity.harness if legacy_identity is not None else "booktx"),
        model=model
        or (legacy_identity.model if legacy_identity is not None else "human"),
    )
    source_cfg = SourceConfig(
        source_language=legacy_project.config.source_language,
        source_file=legacy_project.config.source_file,
        format=legacy_project.config.format,
        chunk_size=legacy_project.config.chunk_size,
    )
    profile_cfg = ProfileConfig(
        profile=profile_name,
        source_language=source_cfg.source_language,
        target_language=legacy_target,
        target_locale=target_locale or legacy_target,
        output_filename=_default_output_filename(source_cfg, legacy_target),
        identity=identity_cfg,
    )

    moves: list[tuple[Path, Path]] = [
        (_legacy_manifest_path(project_root), _source_manifest_path(project_root)),
        (
            _booktx_dir(project_root) / "context.json",
            _profile_context_json_path(project_root, profile_name),
        ),
        (
            _booktx_dir(project_root) / "context.md",
            _profile_context_md_path(project_root, profile_name),
        ),
        (
            _booktx_dir(project_root) / "identity.json",
            _profile_identity_path(project_root, profile_name),
        ),
        (
            _booktx_dir(project_root) / "translation-store.json",
            _profile_store_path(project_root, profile_name),
        ),
        (
            _booktx_dir(project_root) / "translation-version-ledger.json",
            _profile_ledger_path(project_root, profile_name),
        ),
        (
            _booktx_dir(project_root) / "tasks",
            _profile_tasks_dir(project_root, profile_name),
        ),
        (
            _booktx_dir(project_root) / "ingest",
            _profile_ingest_dir(project_root, profile_name),
        ),
        (
            _booktx_dir(project_root) / "translated",
            _profile_translated_dir(project_root, profile_name),
        ),
        (
            _booktx_dir(project_root) / "reports",
            _profile_reports_dir(project_root, profile_name),
        ),
        (project_root / "output", _profile_output_dir(project_root, profile_name)),
    ]
    summary = {
        "project": str(project_root),
        "profile": profile_name,
        "target_language": legacy_target,
        "target_locale": profile_cfg.target_locale or legacy_target,
        "dry_run": dry_run,
        "moves": [
            {
                "source": str(src.relative_to(project_root)),
                "destination": str(dst.relative_to(project_root)),
                "exists": src.exists(),
            }
            for src, dst in moves
        ],
    }
    if dry_run:
        return summary

    profile_root.mkdir(parents=True, exist_ok=True)
    for path in (
        _profile_tasks_dir(project_root, profile_name),
        _profile_ingest_dir(project_root, profile_name),
        _profile_translated_dir(project_root, profile_name),
        _profile_reports_dir(project_root, profile_name),
        _profile_output_dir(project_root, profile_name),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)

    migration_manifest = _write_migration_manifest(project_root, moves)
    _write_source_config(source_config_path(project_root), source_cfg)
    write_profile_config(project_root, profile_cfg)
    from booktx.io_utils import write_json_model_atomic

    write_json_model_atomic(
        _profile_identity_path(project_root, profile_name),
        TranslationIdentity(
            actor=identity_cfg.actor,
            harness=identity_cfg.harness,
            model=identity_cfg.model,
        ),
    )
    for src, dst in moves:
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        shutil.move(str(src), str(dst))
    legacy_config = _legacy_config_path(project_root)
    if legacy_config.exists():
        legacy_config.unlink()
    if select:
        write_profile_state(project_root, ProfileState(active_profile=profile_name))

    tasks_root = _profile_tasks_dir(project_root, profile_name)
    if tasks_root.is_dir():
        for task_path in tasks_root.glob("*.json"):
            task = TranslationTask.model_validate_json(task_path.read_text("utf-8"))
            if not task.profile:
                task.profile = profile_name
            if not task.target_locale:
                task.target_locale = profile_cfg.target_locale or legacy_target
            write_json_model_atomic(task_path, task)

    summary["migration_manifest"] = str(migration_manifest.relative_to(project_root))
    return summary


def init_project(
    target: Path,
    *,
    target_language: str = "",
    profile_name: str | None = None,
    source_language: str = "en",
    source_file: Path | str | None = None,
    chunk_size: int = 50,
) -> Project:
    project = init_source_project(
        target,
        source_language=source_language,
        source_file=source_file,
        chunk_size=chunk_size,
    )
    if not target_language:
        return project
    return create_profile(
        project.root,
        profile_name or _default_profile_name(target_language),
        target_language=target_language,
        target_locale=target_language,
        select=True,
    )


def write_manifest(project: Project, manifest: Manifest) -> None:
    from booktx.io_utils import write_json_model_atomic

    write_json_model_atomic(project.manifest_path, manifest)


def load_manifest(project: Project) -> Manifest | None:
    primary = project.manifest_path
    legacy = _legacy_manifest_path(project)
    source_manifest = _source_manifest_path(project)
    if primary.is_file():
        path = primary
    elif project.layout_version == "profiles" and legacy.is_file():
        path = legacy
    elif project.layout_version == "legacy" and source_manifest.is_file():
        path = source_manifest
    else:
        path = primary
    if not path.is_file():
        return None
    return Manifest.model_validate_json(path.read_text("utf-8"))


def write_names(project: Project, names: NamesFile) -> None:
    from booktx.io_utils import write_json_text_atomic

    write_json_text_atomic(
        project.names_path,
        json.dumps(names.model_dump(mode="json"), indent=2, ensure_ascii=False),
    )


def load_names(project: Project) -> NamesFile:
    if not project.names_path.is_file():
        return NamesFile()
    try:
        return NamesFile.model_validate_json(project.names_path.read_text("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise _err("bad_names_json", f"names.json is invalid: {exc}") from exc


def protected_terms_sha256(protected_terms: list[str]) -> str:
    payload = json.dumps(
        {"protected_terms": list(protected_terms)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def project_source_sha256(project: Project) -> str:
    manifest = load_manifest(project)
    if manifest is not None and manifest.source.sha256:
        return manifest.source.sha256
    return sha256_path(find_source_file(project))


def current_source_sha256(project: Project) -> str:
    return sha256_path(find_source_file(project))


def extracted_source_sha256(project: Project) -> str:
    manifest = load_manifest(project)
    if manifest is not None and manifest.source.sha256:
        return manifest.source.sha256
    return ""


def _require_profile_paths(project: Project, thing: str) -> None:
    if project.layout_version == "profiles" and (
        project.profile is None or project.profile_dir is None
    ):
        raise _err(
            "profile_required",
            f"{thing} requires a translation profile; pass --profile or run `booktx profile select`.",
        )


def translation_store_path(project: Project) -> Path:
    if project.store_path is not None:
        return project.store_path
    _require_profile_paths(project, "translation store access")
    return project.booktx_dir / "translation-store.json"


def translation_version_ledger_path(project: Project) -> Path:
    if project.ledger_path is not None:
        return project.ledger_path
    _require_profile_paths(project, "translation version access")
    return project.booktx_dir / "translation-version-ledger.json"


def identity_path(project: Project) -> Path:
    if project.identity_json_path is not None:
        return project.identity_json_path
    _require_profile_paths(project, "translation identity access")
    return project.booktx_dir / "identity.json"


def load_translation_store(project: Project) -> TranslationStoreV2:
    path = translation_store_path(project)
    if not path.is_file():
        return TranslationStoreV2()
    raw = json.loads(path.read_text("utf-8"))
    if raw.get("version") == 2:
        return TranslationStoreV2.model_validate(raw)
    legacy = TranslationStore.model_validate(raw)
    from booktx.progress import load_source_records
    from booktx.translation_store import legacy_store_to_v2

    source_records = {
        record.record_id: record for record in load_source_records(project)
    }
    return legacy_store_to_v2(legacy, source_records=source_records)


def load_translation_version_ledger(project: Project) -> TranslationVersionLedger:
    path = translation_version_ledger_path(project)
    if not path.is_file():
        return TranslationVersionLedger()
    return TranslationVersionLedger.model_validate_json(path.read_text("utf-8"))


def load_identity(project: Project) -> TranslationIdentity | None:
    path = identity_path(project)
    if not path.is_file():
        return None
    return TranslationIdentity.model_validate_json(path.read_text("utf-8"))


def write_translation_store(project: Project, store: TranslationStoreV2) -> None:
    from booktx.io_utils import write_json_model_atomic

    write_json_model_atomic(translation_store_path(project), store)


def write_translation_version_ledger(
    project: Project, ledger: TranslationVersionLedger
) -> None:
    from booktx.io_utils import write_json_model_atomic

    write_json_model_atomic(translation_version_ledger_path(project), ledger)


def write_identity(project: Project, identity: TranslationIdentity) -> None:
    from booktx.io_utils import write_json_model_atomic

    write_json_model_atomic(identity_path(project), identity)


def translation_task_dir(project: Project) -> Path:
    if project.tasks_dir is not None:
        return project.tasks_dir
    _require_profile_paths(project, "translation task access")
    return project.booktx_dir / "tasks"


def translation_task_path(project: Project, task_id: str) -> Path:
    return translation_task_dir(project) / f"{task_id}.json"


def translation_task_source_block_path(project: Project, task_id: str) -> Path:
    safe_task_id = Path(task_id).name
    if safe_task_id != task_id or not safe_task_id:
        raise _err("invalid_task_id", f"Invalid task id for source path: {task_id!r}")
    return translation_task_dir(project) / f"{safe_task_id}.source.block.txt"


def translation_ingest_dir(project: Project) -> Path:
    if project.ingest_dir is not None:
        return project.ingest_dir
    _require_profile_paths(project, "translation ingest access")
    return project.booktx_dir / "ingest"


def translation_ingest_path(project: Project, task_id: str) -> Path:
    safe_task_id = Path(task_id).name
    if safe_task_id != task_id or not safe_task_id:
        raise _err("invalid_task_id", f"Invalid task id for ingest path: {task_id!r}")
    return translation_ingest_dir(project) / f"{safe_task_id}.json"


def translation_ingest_block_path(project: Project, task_id: str) -> Path:
    safe_task_id = Path(task_id).name
    if safe_task_id != task_id or not safe_task_id:
        raise _err("invalid_task_id", f"Invalid task id for ingest path: {task_id!r}")
    return translation_ingest_dir(project) / f"{safe_task_id}.block.txt"


def load_translation_task(project: Project, task_id: str) -> TranslationTask | None:
    path = translation_task_path(project, task_id)
    if not path.is_file():
        return None
    return TranslationTask.model_validate_json(path.read_text("utf-8"))


def write_translation_task(project: Project, task: TranslationTask) -> None:
    from booktx.io_utils import write_json_model_atomic

    write_json_model_atomic(translation_task_path(project, task.task_id), task)


def _persist_source_config(project: Project) -> None:
    if project.layout_version == "legacy":
        legacy_cfg = ProjectConfig(
            source_language=project.config.source_language,
            target_language=project.config.target_language,
            source_file=project.config.source_file,
            format=project.config.format,
            chunk_size=project.config.chunk_size,
        )
        _write_legacy_config(project.config_path, legacy_cfg)
    else:
        _write_source_config(project.source_config_path, project.source_config)


def find_source_file(project: Project) -> Path:
    configured = project.config.source_file.strip()
    if configured:
        candidate = project.source_dir / configured
        if candidate.is_file():
            return candidate

    candidates = [
        path
        for path in sorted(project.source_dir.iterdir())
        if path.is_file() and path.suffix.lower() in SUPPORTED_SOURCE_SUFFIXES
    ]
    if not candidates:
        raise _err(
            "no_source",
            f"No source document found in {project.source_dir}. Drop a .md or .epub file into source/.",
        )
    if len(candidates) > 1:
        names = ", ".join(path.name for path in candidates)
        raise _err(
            "ambiguous_source",
            f"Found multiple source documents in {project.source_dir}: {names}. Keep exactly one.",
        )
    chosen = candidates[0]
    chosen_format = detect_format(chosen.name)
    if (
        project.config.source_file != chosen.name
        or project.config.format != chosen_format
    ):
        project.config.source_file = chosen.name
        project.config.format = chosen_format
        project.source_config.source_file = chosen.name
        project.source_config.format = chosen_format
        _persist_source_config(project)
    return chosen
