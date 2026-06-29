"""Path-safety tests for artifact-id path constructors (Phase 1).

Ensures task/todo/review ids cannot escape their profile-local directory via
path separators, absolute paths, traversal, or empty values.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from booktx.config import (
    init_project,
    review_todo_json_path,
    review_todo_markdown_path,
    translation_ingest_block_path,
    translation_ingest_path,
    translation_review_ingest_block_path,
    translation_review_source_block_path,
    translation_review_task_path,
    translation_task_path,
    translation_task_source_block_path,
    translation_todo_json_path,
    translation_todo_markdown_path,
)
from booktx.errors import BooktxError
from booktx.path_ids import safe_artifact_id


@pytest.fixture()
def project(tmp_path: Path) -> object:
    return init_project(tmp_path / "book", target_language="de")


CONSTRUCTORS = [
    ("task", translation_task_path),
    ("task", translation_task_source_block_path),
    ("task", translation_ingest_path),
    ("task", translation_ingest_block_path),
    ("todo", translation_todo_json_path),
    ("todo", translation_todo_markdown_path),
    ("review_todo", review_todo_json_path),
    ("review_todo", review_todo_markdown_path),
    ("review_task", translation_review_task_path),
    ("review_task", translation_review_source_block_path),
    ("review_task", translation_review_ingest_block_path),
]


@pytest.mark.parametrize(("kind", "ctor"), CONSTRUCTORS)
def test_valid_id_is_accepted(project, kind: str, ctor) -> None:
    # Should not raise; the id survives unchanged in the basename.
    path = ctor(project, f"{kind}-001")
    assert f"{kind}-001" in path.name


@pytest.mark.parametrize(
    "bad",
    [
        "../evil",
        "..",
        "/abs/path",
        "a/b",
        "ok/../../evil",
        "dir\\file",  # backslash separator on platforms where it counts
        "",
    ],
    ids=[
        "dotdot",
        "dotdot-only",
        "absolute",
        "slash",
        "nested-traversal",
        "backslash",
        "empty",
    ],
)
@pytest.mark.parametrize(("kind", "ctor"), CONSTRUCTORS)
def test_traversal_ids_rejected(project, kind: str, ctor, bad: str) -> None:
    with pytest.raises(BooktxError) as exc:
        ctor(project, bad)
    assert exc.value.code == f"invalid_{kind}_id"


def test_safe_artifact_id_returns_value_unchanged() -> None:
    assert safe_artifact_id("task-007", kind="task") == "task-007"
    assert safe_artifact_id("brt-2026-abc", kind="review_todo") == "brt-2026-abc"


def test_safe_artifact_id_rejects_non_string() -> None:
    with pytest.raises(BooktxError) as exc:
        safe_artifact_id(123, kind="task")  # type: ignore[arg-type]
    assert exc.value.code == "invalid_task_id"


def test_valid_path_stays_within_profile_dir(project, tmp_path: Path) -> None:
    """A valid id must resolve inside the project tree, not above it."""
    path = translation_task_path(project, "task-001")
    assert tmp_path.resolve() in path.resolve().parents or (
        path.resolve() == tmp_path.resolve()
    )
    # Negative sanity: a traversal id, if it were not caught, would leave the tree.
    with pytest.raises(BooktxError):
        translation_task_path(project, "../../../../etc/passwd")
