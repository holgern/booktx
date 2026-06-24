"""Identity payload assembly for the ``whoami`` / identity commands.

Extracted from the CLI so the pure payload logic (no Rich console) is testable
and reusable. The human rendering stays in the CLI layer where the console
lives.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from booktx.config import (
    Project,
    load_translation_store,
    load_translation_version_ledger,
    project_source_sha256,
    translation_store_path,
)
from booktx.context import context_path, load_context
from booktx.versioning import canonical_json_sha256, resolve_identity

__all__ = [
    "identity_payload",
    "context_identity_payload",
    "store_identity_payload",
]


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def context_identity_payload(proj: Project) -> dict[str, Any]:
    path = context_path(proj)
    rel_path = _relative(path, proj.root)
    if not path.is_file():
        return {
            "path": rel_path,
            "exists": False,
            "ready": None,
            "sha256": None,
            "status": "missing",
        }
    try:
        context = load_context(proj)
    except Exception:
        return {
            "path": rel_path,
            "exists": True,
            "ready": None,
            "sha256": None,
            "status": "invalid",
        }
    if context is None:  # pragma: no cover - guarded by is_file() above
        return {
            "path": rel_path,
            "exists": False,
            "ready": None,
            "sha256": None,
            "status": "missing",
        }
    return {
        "path": rel_path,
        "exists": True,
        "ready": context.ready,
        "sha256": canonical_json_sha256(context.model_dump(mode="json", by_alias=True)),
        "status": "ready" if context.ready else "not_ready",
    }


def store_identity_payload(proj: Project) -> dict[str, Any]:
    path = translation_store_path(proj)
    if not path.is_file():
        return {
            "exists": False,
            "version": None,
            "record_count": None,
            "status": "missing",
        }
    try:
        store = load_translation_store(proj)
    except Exception:
        version = None
        try:
            raw = json.loads(path.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            raw = {}
        if isinstance(raw, dict) and isinstance(raw.get("version"), int):
            version = raw["version"]
        return {
            "exists": True,
            "version": version,
            "record_count": None,
            "status": "invalid",
        }
    return {
        "exists": True,
        "version": store.version,
        "record_count": len(store.records),
        "status": "ok",
    }


def identity_payload(proj: Project) -> dict[str, Any]:
    identity = resolve_identity(proj)
    active_version = None
    try:
        active_version = load_translation_version_ledger(proj).active_version
    except Exception:  # noqa: BLE001
        active_version = None

    try:
        source_sha256 = project_source_sha256(proj)
    except Exception:  # noqa: BLE001
        source_sha256 = None

    return {
        "project_dir": str(proj.root),
        "actor": identity.actor,
        "harness": identity.harness,
        "model": identity.model,
        "active_version": active_version,
        "context": context_identity_payload(proj),
        "source_sha256": source_sha256,
        "store": store_identity_payload(proj),
    }
