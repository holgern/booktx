"""Durable review-todo artifacts for bounded multi-pass review runs.

Mirrors :mod:`booktx.agent_todo` for the review workflow: select passes and
chapters with review gaps, build a durable :class:`ReviewTodo`, and render
human-readable markdown loop instructions.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import PydanticUserError, ValidationError

from booktx.command_hints import (
    check_command,
)
from booktx.config import (
    Project,
    _err,
    review_todo_dir,
    review_todo_json_path,
    review_todo_markdown_path,
)
from booktx.io_utils import write_text_atomic
from booktx.models import (
    QualityReviewConfig,
    ReviewTodo,
    ReviewTodoChapter,
    ReviewTodoPass,
)
from booktx.review_status import compute_review_snapshot
from booktx.versioning import canonical_json_sha256

if TYPE_CHECKING:
    from booktx.runtime import RuntimeMode
    from booktx.status import StatusBundle

__all__ = [
    "make_review_todo_id",
    "build_review_todo",
    "write_review_todo",
    "load_review_todo",
    "list_review_todos",
    "ReviewTodoChapterStatus",
    "ReviewTodoStatus",
    "compute_review_todo_status",
    "resume_review_todo",
]


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------


def make_review_todo_id(
    profile: str, first_chapter_id: str, chapter_ids: list[str], pass_numbers: list[int]
) -> str:
    """Derive a deterministic, path-safe review todo id.

    Uses a ``blake2s`` digest (``digest_size=4``) of the joined chapter and
    pass identifiers, plus a seconds-precision UTC timestamp.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload = "|".join(chapter_ids) + "|" + "|".join(str(p) for p in pass_numbers)
    digest = hashlib.blake2s(payload.encode("utf-8"), digest_size=4).hexdigest()
    return f"brt-{stamp}-{profile}-{first_chapter_id}-{digest}"


# ---------------------------------------------------------------------------
# Chapter selection
# ---------------------------------------------------------------------------


def select_review_todo_chapters(
    bundle: StatusBundle,
    quality_cfg: QualityReviewConfig,
    *,
    chapters: int,
    start_chapter: str | None = None,
) -> list[tuple[str, str, int]]:
    """Select the next *chapters* with review gaps for the active passes.

    Returns ``(chapter_id, title, missing_review_count)`` in reading order.
    """
    if chapters < 1:
        raise ValueError("chapters must be >= 1")

    all_chapters = bundle.index.chapter_summaries

    eligible: list[tuple[str, str, int]] = []
    for ch in all_chapters:
        total_missing = 0
        for pass_number in quality_cfg.active_passes:
            pcfg = next(
                (p for p in quality_cfg.passes if p.pass_number == pass_number), None
            )
            if pcfg is not None and not pcfg.enabled:
                continue
            # Count records in this chapter that need review for this pass.
            missing = _count_missing_review(
                bundle, ch.chapter_id, pass_number, quality_cfg
            )
            total_missing += missing
        if total_missing > 0:
            eligible.append((ch.chapter_id, ch.title, total_missing))

    if start_chapter is not None:
        start_idx = next(
            (i for i, (cid, _, _) in enumerate(eligible) if cid == start_chapter),
            None,
        )
        if start_idx is None:
            raise ValueError(
                f"start chapter {start_chapter!r} not found or has no review gaps"
            )
        eligible = eligible[start_idx:]

    return eligible[:chapters]


def _count_missing_review(
    bundle: StatusBundle,
    chapter_id: str,
    pass_number: int,
    quality_cfg: QualityReviewConfig,
) -> int:
    """Count records in a chapter that need review for a pass."""
    # Use the review snapshot for fast per-pass counts.
    record_order: list[tuple[str, str]] = []
    for cid, rids in bundle.index.record_ids_by_chapter.items():
        record_order.extend((rid, cid) for rid in rids)
    from booktx.config import load_translation_store

    store = (
        load_translation_store(bundle.project) if hasattr(bundle, "project") else None
    )
    if store is None:
        return 0
    snapshot = compute_review_snapshot(store, quality_cfg, record_order=record_order)
    for p in snapshot.passes:
        if p.pass_number == pass_number:
            # Count records in this chapter that need review.
            count = 0
            rids = bundle.index.record_ids_by_chapter.get(chapter_id, [])

            for rid in rids:
                stored = store.records.get(rid)
                if stored is None:
                    continue
                from booktx.review_status import (
                    _accepted_review_for_pass as accepted_for_pass,
                )
                from booktx.review_status import (
                    _eligible_for_pass,
                )

                if not _eligible_for_pass(
                    stored, quality_cfg.passes_by_number.get(pass_number)
                ):
                    continue
                if not accepted_for_pass(stored, pass_number):
                    count += 1
            return count
    return 0


# ---------------------------------------------------------------------------
# Todo construction
# ---------------------------------------------------------------------------


def build_review_todo(
    project: Project,
    bundle: StatusBundle,
    quality_cfg: QualityReviewConfig,
    *,
    chapters: int,
    batch_words: int,
    start_chapter: str | None = None,
) -> ReviewTodo:
    """Build a :class:`ReviewTodo` without writing it.

    Raises :class:`ValueError` when no chapters have review gaps.
    """
    if chapters < 1:
        raise ValueError("chapters must be >= 1")
    if batch_words < 1:
        raise ValueError("batch_words must be >= 1")

    selected = select_review_todo_chapters(
        bundle,
        quality_cfg,
        chapters=chapters,
        start_chapter=start_chapter,
    )
    if not selected:
        raise ValueError("no chapters have review gaps")

    # Build pass entries for each active pass.
    passes: list[ReviewTodoPass] = []
    for pass_number in quality_cfg.active_passes:
        pcfg = next(
            (p for p in quality_cfg.passes if p.pass_number == pass_number), None
        )
        if pcfg is not None and not pcfg.enabled:
            continue
        from booktx.review_tasks import default_base_mode

        base = default_base_mode(pcfg)
        passes.append(
            ReviewTodoPass(
                pass_number=pass_number,
                selection="missing",
                base=base,
            )
        )

    # Build chapter entries.
    todo_chapters: list[ReviewTodoChapter] = []
    for chapter_id, title, missing_count in selected:
        live = bundle.index.chapters_by_id.get(chapter_id)
        todo_chapters.append(
            ReviewTodoChapter(
                chapter_id=chapter_id,
                title=title,
                status=live.status if live is not None else "unknown",
                eligible_records_at_start=live.records_total if live is not None else 0,
                missing_review_at_start=missing_count,
                stale_review_at_start=0,
                pending_passes=[p.pass_number for p in passes],
            )
        )

    todo_id = make_review_todo_id(
        project.profile or "",
        selected[0][0],
        [c[0] for c in selected],
        [p.pass_number for p in passes],
    )

    return ReviewTodo(
        review_todo_id=todo_id,
        profile=project.profile or "",
        passes=passes,
        chapters_requested=chapters,
        batch_words=batch_words,
        created_at=datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        source_sha256=bundle.snapshot.source.source_sha256 or None,
        profile_config_sha256=(
            canonical_json_sha256(project.profile_config.model_dump(mode="json"))
            if project.profile_config is not None
            else None
        ),
        source_config_sha256=canonical_json_sha256(
            project.source_config.model_dump(mode="json")
        ),
        start_snapshot_sha256=None,
        chapters=todo_chapters,
    )


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def render_review_todo_markdown(
    todo: ReviewTodo, project: Project, *, mode: RuntimeMode | None = None
) -> str:
    """Render the human-readable review todo markdown."""
    from booktx.command_hints import (
        review_todo_resume_command,
        review_todo_status_command,
    )

    lines: list[str] = []
    lines.append(f"# booktx review todo: {todo.review_todo_id}")
    lines.append("")

    first_chapter = todo.chapters[0] if todo.chapters else None
    lines.append(
        f"Goal: review {todo.chapters_requested} chapter(s)"
        + (
            f" starting at {first_chapter.chapter_id} {first_chapter.title}".rstrip()
            if first_chapter
            else ""
        )
    )
    lines.append(f"Per-task budget: {todo.batch_words} source words")
    lines.append(f"Profile: {todo.profile}")
    pass_display = ", ".join(f"pass {p.pass_number}" for p in todo.passes)
    lines.append(f"Active passes: {pass_display}")
    lines.append("")

    # Stop conditions
    lines.append("## Stop immediately if")
    lines.append("")
    lines.append("- Quality review is disabled or pass config changed.")
    lines.append("- `booktx review insert` rejects the submission.")
    lines.append("- Source drift is detected.")
    lines.append(
        f"- You have completed all passes for {todo.chapters_requested} chapter(s)"
        " from this todo."
    )
    lines.append("")

    # Loop
    lines.append("## Loop")
    lines.append("")
    lines.append("1. Inspect live todo status:")
    lines.append("")
    lines.append("   ```bash")
    lines.append(
        "   "
        + review_todo_status_command(
            project,
            mode=mode,
            review_todo_id=todo.review_todo_id,
        )
    )
    lines.append("   ```")
    lines.append("")
    lines.append("2. If the todo goal is complete, stop and report progress.")
    lines.append("")
    lines.append("3. Request the next bounded review batch:")
    lines.append("")
    lines.append("   ```bash")
    next_cmd = review_todo_resume_command(
        project,
        mode=mode,
        review_todo_id=todo.review_todo_id,
    )
    lines.append(f"   {next_cmd}")
    lines.append("   ```")
    lines.append("")
    lines.append("4. Read the printed source file and edit the ingest block.")
    lines.append("")
    lines.append("5. Submit exactly the printed submit command.")
    lines.append("")
    lines.append("6. Validate the active chapter:")
    lines.append("")
    lines.append("   ```bash")
    scoped_check = check_command(
        project,
        mode=mode,
        chapter_id=first_chapter.chapter_id if first_chapter else None,
        fail_on_warnings=True,
    )
    lines.append(f"   {scoped_check}")
    lines.append("   ```")
    lines.append("")

    # Planned chapters table
    lines.append("## Planned chapters")
    lines.append("")
    lines.append(
        "| chapter | title | eligible records | missing review | pending passes |"
    )
    lines.append("|---|---:|---:|---|")
    for c in todo.chapters:
        passes_display = ", ".join(str(p) for p in c.pending_passes)
        lines.append(
            f"| {c.chapter_id} | {c.title} | {c.eligible_records_at_start} "
            f"| {c.missing_review_at_start} | {passes_display} |"
        )
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# File writing
# ---------------------------------------------------------------------------


def write_review_todo(
    project: Project,
    todo: ReviewTodo,
    *,
    mode: RuntimeMode | None = None,
) -> tuple[Path, Path]:
    """Persist both the JSON and Markdown todo files atomically.

    Returns ``(json_path, md_path)``.
    """
    from booktx.io_utils import write_json_model_atomic

    todo_dir = review_todo_dir(project)
    todo_dir.mkdir(parents=True, exist_ok=True)

    json_path = review_todo_json_path(project, todo.review_todo_id)
    md_path = review_todo_markdown_path(project, todo.review_todo_id)

    write_json_model_atomic(json_path, todo)
    write_text_atomic(md_path, render_review_todo_markdown(todo, project, mode=mode))

    return json_path, md_path


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_review_todo(project: Project, review_todo_id: str) -> ReviewTodo | None:
    """Load one durable review todo by id."""
    path = review_todo_json_path(project, review_todo_id)
    if not path.is_file():
        return None
    try:
        return ReviewTodo.model_validate_json(path.read_text("utf-8"))
    except PydanticUserError as exc:
        raise _err(
            "review_todo_model_init_failed",
            f"internal review todo model init failed for {review_todo_id}: {exc}",
        ) from exc
    except ValidationError as exc:
        raise _err(
            "invalid_review_todo",
            f"review todo file {review_todo_id} is invalid: {exc}",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise _err(
            "invalid_review_todo", f"review todo {review_todo_id} is invalid: {exc}"
        ) from exc


def list_review_todos(project: Project) -> list[ReviewTodo]:
    """Return all durable review todos sorted by creation time."""
    todo_dir = review_todo_dir(project)
    if not todo_dir.exists():
        return []
    todos: list[ReviewTodo] = []
    for path in sorted(todo_dir.glob("*.json")):
        try:
            todos.append(ReviewTodo.model_validate_json(path.read_text("utf-8")))
        except PydanticUserError as exc:
            raise _err(
                "review_todo_model_init_failed",
                f"internal review todo model init failed for {path.name}: {exc}",
            ) from exc
        except ValidationError as exc:
            raise _err(
                "invalid_review_todo",
                f"review todo file {path.name} is invalid: {exc}",
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise _err(
                "invalid_review_todo",
                f"review todo file {path.name} is invalid: {exc}",
            ) from exc
    todos.sort(key=lambda todo: (todo.created_at, todo.review_todo_id))
    return todos


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ReviewTodoChapterStatus:
    chapter_id: str
    title: str
    eligible_records_now: int
    missing_review_now: int
    stale_review_now: int
    complete: bool
    pending_passes_now: list[int]

    def as_dict(self) -> dict[str, object]:
        return {
            "chapter_id": self.chapter_id,
            "title": self.title,
            "eligible_records_now": self.eligible_records_now,
            "missing_review_now": self.missing_review_now,
            "stale_review_now": self.stale_review_now,
            "complete": self.complete,
            "pending_passes_now": self.pending_passes_now,
        }


@dataclass(slots=True)
class ReviewTodoStatus:
    todo: ReviewTodo
    chapters: list[ReviewTodoChapterStatus]
    goal_complete: bool
    current_chapter: ReviewTodoChapterStatus | None
    source_drifted: bool
    state: str
    next_safe_command: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "review_todo_id": self.todo.review_todo_id,
            "profile": self.todo.profile,
            "goal_complete": self.goal_complete,
            "source_drifted": self.source_drifted,
            "state": self.state,
            "next_safe_command": self.next_safe_command,
            "chapters": [ch.as_dict() for ch in self.chapters],
        }


def compute_review_todo_status(
    todo: ReviewTodo,
    project: Project,
    bundle: StatusBundle,
    quality_cfg: QualityReviewConfig,
    *,
    mode: RuntimeMode | None = None,
) -> ReviewTodoStatus:
    """Build the live status snapshot for one review todo."""
    from booktx.config import load_translation_store

    store = load_translation_store(project)
    store_records = store.records

    record_order: list[tuple[str, str]] = []
    for cid, rids in bundle.index.record_ids_by_chapter.items():
        record_order.extend((rid, cid) for rid in rids)

    chapter_statuses: list[ReviewTodoChapterStatus] = []
    current: ReviewTodoChapterStatus | None = None

    for planned in todo.chapters:
        live = bundle.index.chapters_by_id.get(planned.chapter_id)
        if live is None:
            ch_status = ReviewTodoChapterStatus(
                chapter_id=planned.chapter_id,
                title=planned.title,
                eligible_records_now=0,
                missing_review_now=planned.missing_review_at_start,
                stale_review_now=0,
                complete=False,
                pending_passes_now=list(planned.pending_passes),
            )
        else:
            # Compute current review gaps for this chapter
            missing_now = 0
            for pass_number in planned.pending_passes:
                from booktx.review_status import (
                    _accepted_review_for_pass as accepted_for_pass,
                )
                from booktx.review_status import (
                    _eligible_for_pass,
                )

                pcfg = next(
                    (p for p in quality_cfg.passes if p.pass_number == pass_number),
                    None,
                )
                rids = bundle.index.record_ids_by_chapter.get(planned.chapter_id, [])
                for rid in rids:
                    stored = store_records.get(rid)
                    if stored is None:
                        continue
                    if not _eligible_for_pass(stored, pcfg):
                        continue
                    if not accepted_for_pass(stored, pass_number):
                        missing_now += 1

            ch_status = ReviewTodoChapterStatus(
                chapter_id=planned.chapter_id,
                title=planned.title,
                eligible_records_now=live.records_total,
                missing_review_now=missing_now,
                stale_review_now=0,
                complete=missing_now == 0,
                pending_passes_now=list(planned.pending_passes),
            )

        if current is None and not ch_status.complete:
            current = ch_status
        chapter_statuses.append(ch_status)

    goal_complete = all(ch.complete for ch in chapter_statuses)
    source_drifted = bundle.snapshot.source.source_drifted
    if todo.source_sha256 is not None:
        source_drifted = (
            source_drifted or bundle.snapshot.source.source_sha256 != todo.source_sha256
        )

    state = "ready"
    next_safe_command: str | None = None
    if goal_complete:
        state = "complete"
    elif source_drifted:
        state = "blocked"
        next_safe_command = "booktx extract ."
    elif current is not None:
        from booktx.command_hints import review_todo_resume_command

        next_safe_command = review_todo_resume_command(
            project, mode=mode, review_todo_id=todo.review_todo_id
        )

    return ReviewTodoStatus(
        todo=todo,
        chapters=chapter_statuses,
        goal_complete=goal_complete,
        current_chapter=current,
        source_drifted=source_drifted,
        state=state,
        next_safe_command=next_safe_command,
    )


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


def resume_review_todo(
    project: Project,
    bundle: StatusBundle,
    quality_cfg: QualityReviewConfig,
    *,
    mode: RuntimeMode | None = None,
    review_todo_id: str | None = None,
    latest: bool = False,
):
    """Create the next bounded review task from an open todo.

    Returns a :class:`TranslationReviewTask`.
    """
    if bool(review_todo_id) == bool(latest):
        raise _err(
            "review_todo_selector_required",
            "pass exactly one of --review-todo-id or --latest",
        )

    if review_todo_id is not None:
        todo = load_review_todo(project, review_todo_id)
        if todo is None:
            raise _err(
                "unknown_review_todo", f"unknown review todo id: {review_todo_id}"
            )
    else:
        todo = latest_incomplete_review_todo(project, bundle, quality_cfg)
        if todo is None:
            raise _err(
                "no_incomplete_review_todo",
                "no incomplete review todo was found",
            )

    status = compute_review_todo_status(todo, project, bundle, quality_cfg, mode=mode)
    if status.goal_complete:
        raise _err(
            "review_todo_complete",
            f"review todo {todo.review_todo_id} is already complete.",
        )
    if status.source_drifted:
        raise _err(
            "review_todo_source_drift",
            "source drifted since the review todo was created. Run `booktx extract .`.",
        )
    if status.current_chapter is None:
        raise _err(
            "review_todo_complete",
            f"review todo {todo.review_todo_id} is already complete.",
        )

    current = status.current_chapter
    # Find the first pending pass that has review gaps for this chapter.
    next_pass: int | None = None
    for pass_number in current.pending_passes_now:
        # Count missing records for this pass+chapter
        from booktx.config import load_translation_store

        store = load_translation_store(project)
        rids = bundle.index.record_ids_by_chapter.get(current.chapter_id, [])
        pcfg = next(
            (p for p in quality_cfg.passes if p.pass_number == pass_number), None
        )
        for rid in rids:
            stored = store.records.get(rid)
            if stored is None:
                continue
            from booktx.review_status import (
                _accepted_review_for_pass as accepted_for_pass,
            )
            from booktx.review_status import (
                _eligible_for_pass,
            )

            if not _eligible_for_pass(stored, pcfg):
                continue
            if not accepted_for_pass(stored, pass_number):
                next_pass = pass_number
                break
        if next_pass is not None:
            break

    if next_pass is None:
        raise _err(
            "review_todo_no_pending_passes",
            f"review todo {todo.review_todo_id} "
            "has no pending passes with review gaps.",
        )

    # Find the chapter progress object.
    chapter_progress = bundle.index.chapters_by_id.get(current.chapter_id)
    if chapter_progress is None:
        raise _err(
            "review_todo_chapter_missing",
            f"chapter {current.chapter_id} is no longer present.",
        )

    from booktx.review_tasks import (
        create_review_task,
        select_review_records,
    )

    selected = select_review_records(
        bundle,
        store.records,
        quality_cfg,
        pass_number=next_pass,
        chapter_id=current.chapter_id,
        max_words=todo.batch_words,
        selection="missing",
        base=None,
    )
    if not selected:
        raise _err(
            "review_todo_no_records",
            "no review records found for the current pass and chapter.",
        )

    return create_review_task(
        project,
        bundle,
        quality_cfg,
        selected,
        pass_number=next_pass,
        chapter=chapter_progress,
    )


# ---------------------------------------------------------------------------
# Latest incomplete
# ---------------------------------------------------------------------------


def latest_incomplete_review_todo(
    project: Project, bundle: StatusBundle, quality_cfg: QualityReviewConfig
) -> ReviewTodo | None:
    """Return the latest incomplete review todo when the choice is safe."""
    todos = list_review_todos(project)
    incomplete: list[ReviewTodo] = []
    chapter_sets: dict[str, set[str]] = {}
    for todo in todos:
        status = compute_review_todo_status(todo, project, bundle, quality_cfg)
        if not status.goal_complete:
            incomplete.append(todo)
            chapter_sets[todo.review_todo_id] = {ch.chapter_id for ch in todo.chapters}
    if not incomplete:
        return None
    latest = max(incomplete, key=lambda t: (t.created_at, t.review_todo_id))
    latest_chapters = chapter_sets[latest.review_todo_id]
    overlaps = [
        t.review_todo_id
        for t in incomplete
        if t.review_todo_id != latest.review_todo_id
        and latest_chapters.intersection(chapter_sets[t.review_todo_id])
    ]
    if overlaps:
        overlap_display = ", ".join(sorted(overlaps))
        raise _err(
            "ambiguous_latest_review_todo",
            f"latest incomplete review todo {latest.review_todo_id} overlaps "
            f"planned chapters with {overlap_display}. "
            "Use --review-todo-id to select the intended todo.",
        )
    return latest
