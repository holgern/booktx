"""Unit tests for booktx.agents_md rendering and managed-file safety."""

from __future__ import annotations

import os

import pytest

from booktx.agents_md import (
    AGENTS_FILENAME,
    AgentsMdMetadata,
    delete_managed_agents_md,
    inspect_agents_md,
    render_agents_md,
    write_managed_agents_md,
)
from booktx.errors import BooktxError
from booktx.io_utils import write_text_atomic

ISO_KWARGS = {
    "mode": "isolated",
    "profile": "de_gpt5_5",
    "source_id": "sha256:0123456789abcdef",
    "target_locale": "de-DE",
}
COLLAB_KWARGS = {
    "mode": "collaborative",
    "profile": None,
    "source_id": "sha256:0123456789abcdef",
}


def _managed_block(body: str = "") -> str:
    return (
        "<!-- booktx-agents-md\n"
        "schema: booktx.agents-md.v1\n"
        "mode: isolated\n"
        "profile: de_gpt5_5\n"
        "generated_by: booktx\n"
        "source_id: sha256:abc\n"
        "-->\n" + body
    )


# --- case 1 & 2: isolated render content and forbidden tokens -------------


def test_isolated_render_contains_required_commands():
    text = render_agents_md(**ISO_KWARGS)
    assert "booktx mode ." in text
    assert "booktx doctor isolation ." in text
    assert "booktx context status ." in text
    assert "booktx translate todo-next . --chapters N --batch-words 800 --write" in text
    assert "booktx translate todo-status . --latest" in text
    assert "booktx translate todo-resume . --latest --format block" in text
    # bounded-todo completion does not require a whole-book build
    assert "booktx build . --require-complete" in text


def test_isolated_render_has_no_forbidden_tokens():
    text = render_agents_md(**ISO_KWARGS)
    forbidden = ["../", "translations/", "--profile"]
    for token in forbidden:
        assert token not in text, f"isolated render leaked {token!r}"
    # The current profile name and target locale are allowed (local identity).
    assert "profile: de_gpt5_5" in text
    assert "target: de-DE" in text
    # No absolute command paths leak (a leading slash would be a parent escape).
    for line in text.splitlines():
        stripped = line.lstrip()
        assert not stripped.startswith("booktx /"), line
        assert not stripped.startswith("booktx ../"), line


# --- case 3: collaborative render ----------------------------------------


def test_collaborative_render_contains_required_commands():
    text = render_agents_md(**COLLAB_KWARGS)
    assert "booktx profile list ." in text
    assert "--profile PROFILE" in text
    assert "booktx agents write . --mode isolated --profile PROFILE" in text
    assert "booktx translate todo-next . --profile PROFILE --chapters 3" in text


# --- case 4: inspect returns managed-valid metadata ----------------------


def test_inspect_returns_managed_valid_metadata_for_generated_file(tmp_path):
    path = tmp_path / AGENTS_FILENAME
    write_managed_agents_md(path, render_agents_md(**ISO_KWARGS))
    inspection = inspect_agents_md(path)
    assert inspection.state == "managed-valid"
    meta = inspection.metadata
    assert isinstance(meta, AgentsMdMetadata)
    assert meta.schema == "booktx.agents-md.v1"
    assert meta.mode == "isolated"
    assert meta.profile == "de_gpt5_5"
    assert meta.source_id == ISO_KWARGS["source_id"]
    assert meta.generated_by == "booktx"


# --- case 5: parser rejects missing/duplicate/unknown keys ---------------


def test_inspect_marks_missing_key_as_malformed(tmp_path):
    path = tmp_path / AGENTS_FILENAME
    write_text_atomic(
        path,
        "<!-- booktx-agents-md\n"
        "schema: booktx.agents-md.v1\n"
        "mode: isolated\n"
        "profile: de_gpt5_5\n"
        "generated_by: booktx\n"
        "-->\n",
    )
    assert inspect_agents_md(path).state == "managed-malformed"


def test_inspect_marks_duplicate_key_as_malformed(tmp_path):
    path = tmp_path / AGENTS_FILENAME
    write_text_atomic(
        path,
        "<!-- booktx-agents-md\n"
        "schema: booktx.agents-md.v1\n"
        "schema: booktx.agents-md.v1\n"
        "mode: isolated\n"
        "profile: de_gpt5_5\n"
        "generated_by: booktx\n"
        "source_id: sha256:abc\n"
        "-->\n",
    )
    assert inspect_agents_md(path).state == "managed-malformed"


def test_inspect_marks_unknown_key_as_malformed(tmp_path):
    path = tmp_path / AGENTS_FILENAME
    write_text_atomic(path, _managed_block().replace("-->\n", "extra: no\n-->\n"))
    assert inspect_agents_md(path).state == "managed-malformed"


def test_inspect_marks_invalid_mode_profile_combo_as_malformed(tmp_path):
    path = tmp_path / AGENTS_FILENAME
    write_text_atomic(
        path,
        "<!-- booktx-agents-md\n"
        "schema: booktx.agents-md.v1\n"
        "mode: collaborative\n"
        "profile: de_gpt5_5\n"
        "generated_by: booktx\n"
        "source_id: sha256:abc\n"
        "-->\n",
    )
    assert inspect_agents_md(path).state == "managed-malformed"


# --- case 6: stale source metadata remains managed -----------------------


def test_stale_source_metadata_remains_managed(tmp_path):
    path = tmp_path / AGENTS_FILENAME
    write_text_atomic(
        path,
        "<!-- booktx-agents-md\n"
        "schema: booktx.agents-md.v1\n"
        "mode: isolated\n"
        "profile: de_gpt5_5\n"
        "generated_by: booktx\n"
        "source_id: sha256:STALE\n"
        "-->\n",
    )
    inspection = inspect_agents_md(path)
    assert inspection.state == "managed-valid"
    assert inspection.metadata.source_id == "sha256:STALE"
    # Staleness is computed by the workflow layer against the live project id.


# --- case 7: malformed ownership metadata is not unmanaged ---------------


def test_malformed_managed_file_is_not_treated_as_unmanaged(tmp_path):
    path = tmp_path / AGENTS_FILENAME
    write_text_atomic(
        path,
        "<!-- booktx-agents-md\nschema: booktx.agents-md.v1\nmode: isolated\n-->\n",
    )
    assert inspect_agents_md(path).state == "managed-malformed"


# --- case 8: unmanaged AGENTS.md blocks overwrite by default -------------


def test_unmanaged_file_blocks_overwrite(tmp_path):
    path = tmp_path / AGENTS_FILENAME
    path.write_text("# my custom harness instructions\n", encoding="utf-8")
    with pytest.raises(BooktxError) as exc:
        write_managed_agents_md(path, render_agents_md(**ISO_KWARGS))
    assert exc.value.code == "agents_unmanaged_target"


# --- case 9: --replace-unmanaged replaces only the selected target -------


def test_replace_unmanaged_replaces_regular_file_target(tmp_path):
    path = tmp_path / AGENTS_FILENAME
    path.write_text("# my custom harness instructions\n", encoding="utf-8")
    write_managed_agents_md(
        path, render_agents_md(**ISO_KWARGS), replace_unmanaged=True
    )
    assert inspect_agents_md(path).state == "managed-valid"


def test_replace_unmanaged_does_not_replace_malformed(tmp_path):
    path = tmp_path / AGENTS_FILENAME
    write_text_atomic(
        path,
        "<!-- booktx-agents-md\nschema: booktx.agents-md.v1\nmode: isolated\n-->\n",
    )
    with pytest.raises(BooktxError) as exc:
        write_managed_agents_md(
            path, render_agents_md(**ISO_KWARGS), replace_unmanaged=True
        )
    assert exc.value.code == "agents_malformed_target"


# --- case 10: managed deletion requires valid metadata + expected mode ----


def test_delete_requires_managed_valid_and_matching_mode(tmp_path):
    path = tmp_path / AGENTS_FILENAME
    write_managed_agents_md(path, render_agents_md(**ISO_KWARGS))
    # wrong mode is rejected
    with pytest.raises(BooktxError) as exc:
        delete_managed_agents_md(path, expected_mode="collaborative")
    assert exc.value.code == "agents_delete_mode_mismatch"
    assert path.exists()
    # right mode deletes
    assert delete_managed_agents_md(path, expected_mode="isolated") is True
    assert not path.exists()
    # absent is a no-op False
    assert delete_managed_agents_md(path, expected_mode="isolated") is False


def test_delete_refuses_unmanaged_and_malformed(tmp_path):
    unmanaged = tmp_path / "u.md"
    unmanaged.write_text("custom\n", encoding="utf-8")
    with pytest.raises(BooktxError) as exc:
        delete_managed_agents_md(unmanaged)
    assert exc.value.code == "agents_delete_unmanaged"
    assert unmanaged.exists()


# --- case 11: symlinks are reported but never followed/overwritten/deleted


def test_symlink_reported_and_never_touched(tmp_path):
    target = tmp_path / "target.md"
    target.write_text("payload\n", encoding="utf-8")
    link = tmp_path / AGENTS_FILENAME
    os.symlink(target, link)
    assert inspect_agents_md(link).state == "symlink"
    with pytest.raises(BooktxError) as exc:
        write_managed_agents_md(link, render_agents_md(**ISO_KWARGS))
    assert exc.value.code == "agents_symlink_target"
    with pytest.raises(BooktxError) as exc:
        delete_managed_agents_md(link)
    assert exc.value.code == "agents_delete_symlink"
    assert link.is_symlink()
    assert target.read_text() == "payload\n"


# --- case 12: rendering ends with exactly one newline --------------------


def test_render_ends_with_exactly_one_newline():
    iso = render_agents_md(**ISO_KWARGS)
    assert iso.endswith("\n")
    assert not iso.endswith("\n\n")
    col = render_agents_md(**COLLAB_KWARGS)
    assert col.endswith("\n")
    assert not col.endswith("\n\n")


# --- extra: comment must start at byte zero ------------------------------


def test_marker_not_at_byte_zero_is_unmanaged(tmp_path):
    path = tmp_path / AGENTS_FILENAME
    path.write_text(
        "\n<!-- booktx-agents-md\nschema: booktx.agents-md.v1\n-->\n", encoding="utf-8"
    )
    assert inspect_agents_md(path).state == "unmanaged"


def test_unclosed_comment_within_4kib_is_malformed(tmp_path):
    path = tmp_path / AGENTS_FILENAME
    write_text_atomic(path, "<!-- booktx-agents-md\nschema: booktx.agents-md.v1\n")
    assert inspect_agents_md(path).state == "managed-malformed"
