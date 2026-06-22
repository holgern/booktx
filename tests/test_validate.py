"""Tests for booktx.validate: every hard rule from the translation contract."""

from __future__ import annotations

import json
from pathlib import Path

from booktx.config import init_project, load_project
from booktx.models import Chunk, Placeholder, Record
from booktx.validate import (
    Severity,
    validate_chunk_pair,
    validate_project,
    write_report,
)


def _src_chunk(chunk_id: str = "0001") -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        source_language="en",
        target_language="de",
        records=[
            Record(
                id=f"{chunk_id}-000001",
                source="__NAME_001__ looked at __NAME_002__ here.",
                protected_terms=["Alice", "Mr. Smith"],
                placeholders=[
                    Placeholder(token="__NAME_001__", original="Alice", kind="name"),
                    Placeholder(
                        token="__NAME_002__", original="Mr. Smith", kind="name"
                    ),
                ],
            ),
            Record(
                id=f"{chunk_id}-000002",
                source="Run __TAG_001__ now.",
                protected_terms=[],
                placeholders=[
                    Placeholder(token="__TAG_001__", original="`code`", kind="tag")
                ],
            ),
        ],
    )


def _write_translation(tmp_path: Path, chunk_id: str, payload: object) -> Path:
    proj = init_project(tmp_path / "book", target_language="de")
    chunks_dir = proj.chunks_dir
    chunks_dir.mkdir(parents=True, exist_ok=True)
    (chunks_dir / f"{chunk_id}.json").write_text(
        _src_chunk(chunk_id).model_dump_json(), encoding="utf-8"
    )
    translated = proj.translated_dir / f"{chunk_id}.json"
    translated.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        translated.write_text(payload, encoding="utf-8")
    else:
        translated.write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path / "book"


def _valid_translation() -> dict:
    return {
        "chunk_id": "0001",
        "records": [
            {"id": "0001-000001", "target": "__NAME_001__ sah __NAME_002__ an."},
            {"id": "0001-000002", "target": "Führe __TAG_001__ aus."},
        ],
    }


def test_valid_translation_passes(tmp_path: Path):
    proj_path = _write_translation(tmp_path, "0001", _valid_translation())
    proj = load_project(proj_path)
    report = validate_project(proj)
    assert report.passed, [f.as_dict() for f in report.findings]
    assert report.errors == []


def test_rule_invalid_json(tmp_path: Path):
    proj_path = _write_translation(tmp_path, "0001", "{not valid json")
    report = validate_project(load_project(proj_path))
    rules = {f.rule for f in report.findings}
    assert "invalid_json_or_commentary" in rules
    assert not report.passed


def test_rule_record_count_changed(tmp_path: Path):
    payload = _valid_translation()
    payload["records"].append({"id": "0001-000003", "target": "Extra."})
    proj_path = _write_translation(tmp_path, "0001", payload)
    report = validate_project(load_project(proj_path))
    assert "record_count_changed" in {f.rule for f in report.findings}
    assert not report.passed


def test_rule_record_id_changed(tmp_path: Path):
    payload = _valid_translation()
    payload["records"][0]["id"] = "0001-999999"
    proj_path = _write_translation(tmp_path, "0001", payload)
    report = validate_project(load_project(proj_path))
    rules = {f.rule for f in report.findings}
    assert "record_id_removed" in rules or "record_id_added" in rules
    assert not report.passed


def test_rule_empty_target(tmp_path: Path):
    payload = _valid_translation()
    payload["records"][0]["target"] = "   "
    proj_path = _write_translation(tmp_path, "0001", payload)
    report = validate_project(load_project(proj_path))
    assert "empty_target" in {f.rule for f in report.findings}
    assert not report.passed


def test_rule_placeholder_removed(tmp_path: Path):
    payload = _valid_translation()
    payload["records"][1]["target"] = "Führe aus."  # dropped __TAG_001__
    proj_path = _write_translation(tmp_path, "0001", payload)
    report = validate_project(load_project(proj_path))
    assert "placeholder_removed_or_changed" in {f.rule for f in report.findings}
    assert not report.passed


def test_rule_placeholder_added(tmp_path: Path):
    payload = _valid_translation()
    payload["records"][0]["target"] = "__NAME_001__ sah __NAME_002__ __TAG_099__ an."
    proj_path = _write_translation(tmp_path, "0001", payload)
    report = validate_project(load_project(proj_path))
    assert "placeholder_added" in {f.rule for f in report.findings}
    assert not report.passed


def test_placeholder_metadata_only_tokens_do_not_require_target_tokens(tmp_path: Path):
    chunk = Chunk(
        chunk_id="0001",
        source_language="en",
        target_language="de",
        records=[
            Record(
                id="0001-000001",
                source="No visible placeholder here.",
                protected_terms=[],
                placeholders=[
                    Placeholder(token="__TAG_001__", original="<i>", kind="tag"),
                    Placeholder(token="__TAG_002__", original="</i>", kind="tag"),
                ],
            )
        ],
    )
    translated = tmp_path / "0001.json"
    translated.write_text(
        json.dumps(
            {
                "chunk_id": "0001",
                "records": [
                    {
                        "id": "0001-000001",
                        "target": "Hier ist kein sichtbarer Platzhalter.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    findings = validate_chunk_pair(chunk, translated)

    assert [f.as_dict() for f in findings] == []


def test_placeholder_visible_source_tokens_still_required(tmp_path: Path):
    chunk = Chunk(
        chunk_id="0001",
        source_language="en",
        target_language="de",
        records=[
            Record(
                id="0001-000001",
                source="Run __TAG_001__ now.",
                protected_terms=[],
                placeholders=[],
            )
        ],
    )
    translated = tmp_path / "0001.json"
    translated.write_text(
        json.dumps(
            {
                "chunk_id": "0001",
                "records": [{"id": "0001-000001", "target": "Jetzt ausführen."}],
            }
        ),
        encoding="utf-8",
    )

    findings = validate_chunk_pair(chunk, translated)

    assert "placeholder_removed_or_changed" in {f.rule for f in findings}


def test_rule_protected_name_translated(tmp_path: Path):
    payload = _valid_translation()
    # Alice placeholder dropped and the name rendered in target language.
    payload["records"][0]["target"] = "Aliza sah __NAME_002__ an."
    proj_path = _write_translation(tmp_path, "0001", payload)
    report = validate_project(load_project(proj_path))
    rules = {f.rule for f in report.findings}
    assert "protected_name_translated_or_removed" in rules
    assert "placeholder_removed_or_changed" in rules
    assert not report.passed


def test_rule_commentary_outside_json(tmp_path: Path):
    raw = json.dumps(_valid_translation())
    proj_path = _write_translation(tmp_path, "0001", raw + "\n\n// hope this helps!")
    report = validate_project(load_project(proj_path))
    assert "invalid_json_or_commentary" in {f.rule for f in report.findings}
    assert not report.passed


def test_missing_translation_is_not_an_error(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    proj.chunks_dir.mkdir(parents=True, exist_ok=True)
    (proj.chunks_dir / "0001.json").write_text(
        _src_chunk().model_dump_json(), encoding="utf-8"
    )
    report = validate_project(proj)
    assert report.passed  # no translated file yet
    assert report.chunks_missing_translation == 1


def test_stale_translation_is_a_warning(tmp_path: Path):
    proj = init_project(tmp_path / "book", target_language="de")
    proj.chunks_dir.mkdir(parents=True, exist_ok=True)
    proj.translated_dir.mkdir(parents=True, exist_ok=True)
    # translated file with no matching chunk
    (proj.translated_dir / "9999.json").write_text(
        json.dumps({"chunk_id": "9999", "records": []}), encoding="utf-8"
    )
    report = validate_project(proj)
    assert report.passed  # warnings do not fail
    assert any(f.rule == "stale_translation" for f in report.warnings)


def test_write_report_creates_json(tmp_path: Path):
    proj_path = _write_translation(tmp_path, "0001", _valid_translation())
    proj = load_project(proj_path)
    report = validate_project(proj)
    out = write_report(proj, report)
    assert out.is_file()
    data = json.loads(out.read_text("utf-8"))
    assert data["passed"] is True
    assert data["chunks_checked"] == 1


def test_validate_chunk_pair_directly():
    # Pair-level API works without a project on disk.
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "0001.json"
        p.write_text(json.dumps(_valid_translation()), encoding="utf-8")
        findings = validate_chunk_pair(_src_chunk(), p)
        assert all(f.severity != Severity.ERROR for f in findings)
