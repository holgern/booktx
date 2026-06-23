"""Submission payload parsing for the translate-accept commands.

Owns the three submission wire formats (JSON, TSV, block) and the input-source
selection that the ``translate insert`` command used to inline. Parsers return
:class:`booktx.acceptance.SubmittedRecord` lists directly so the acceptance
service never sees raw dicts.

Validation errors are raised as :class:`booktx.config.BooktxError`; the CLI
renders them via the shared ``_handle_booktx_error`` path, so messages stay
identical to the previous inline ``_die`` calls.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from booktx.acceptance import SubmittedRecord
from booktx.config import _err
from booktx.models import TranslatedRecord
from booktx.record_refs import parse_version_ref

if TYPE_CHECKING:
    pass

__all__ = [
    "ParsedSubmission",
    "parse_json_submission",
    "parse_tsv_submission",
    "parse_block_submission",
    "read_submission_file",
    "resolve_submission",
]


_BLOCK_HEADER_RE = re.compile(r"^>>>\s+(?P<id>\S+)\s*$")


class ParsedSubmission:
    """A parsed submission payload: optional task id + validated records."""

    __slots__ = ("task_id", "records", "translation_version", "profile")

    def __init__(
        self,
        records: list[SubmittedRecord],
        task_id: str | None = None,
        translation_version: str | None = None,
        profile: str | None = None,
    ) -> None:
        self.records = records
        self.task_id = task_id
        self.translation_version = translation_version
        self.profile = profile


def _trim_blank_edge_lines(lines: list[str]) -> str:
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[start:end])


def parse_json_submission(text: str) -> ParsedSubmission:
    """Parse a JSON ``{"task_id": ..., "records": [{"id","target"}, ...]}`` payload."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _err(
            "invalid_json_submission",
            f"invalid JSON submission: {exc.msg} (line {exc.lineno} col {exc.colno})",
        ) from None
    if not isinstance(payload, dict):
        raise _err("invalid_json_submission", "JSON submission must be an object")
    schema_version = payload.get("schema_version")
    if schema_version is not None and not isinstance(schema_version, int):
        raise _err(
            "invalid_json_submission",
            "JSON submission field 'schema_version' must be an integer",
        )
    legacy_version = payload.get("version")
    if legacy_version is not None and not isinstance(legacy_version, int):
        raise _err(
            "invalid_json_submission",
            "JSON submission field 'version' must be an integer",
        )
    translation_version_raw = payload.get("translation_version")
    profile_raw = payload.get("profile")
    translation_version = None
    profile = None
    if translation_version_raw is not None:
        if not isinstance(translation_version_raw, str):
            raise _err(
                "invalid_json_submission",
                "JSON submission field 'translation_version' must be a string",
            )
        try:
            translation_version = parse_version_ref(translation_version_raw).version_ref
        except ValueError as exc:
            raise _err("invalid_json_submission", str(exc)) from None
    if profile_raw is not None:
        if not isinstance(profile_raw, str):
            raise _err(
                "invalid_json_submission",
                "JSON submission field 'profile' must be a string",
            )
        profile = profile_raw.strip() or None
    records = payload.get("records")
    if not isinstance(records, list):
        raise _err(
            "invalid_json_submission", "JSON submission must contain a 'records' array"
        )
    parsed: list[SubmittedRecord] = []
    for item in records:
        if not isinstance(item, dict):
            raise _err(
                "invalid_json_submission", "each submitted record must be an object"
            )
        try:
            record = TranslatedRecord.model_validate(item)
        except Exception as exc:  # noqa: BLE001
            raise _err(
                "invalid_json_submission", f"invalid submitted record: {exc}"
            ) from None
        if not record.id or not isinstance(record.target, str):
            raise _err(
                "invalid_json_submission",
                "each submitted record must contain string fields 'id' and 'target'",
            )
        parsed.append(SubmittedRecord(id=record.id.strip(), target=record.target))
    task_id = payload.get("task_id")
    return ParsedSubmission(
        parsed,
        str(task_id).strip() if task_id else None,
        translation_version=translation_version,
        profile=profile,
    )


def parse_tsv_submission(text: str) -> ParsedSubmission:
    """Parse ``<record-id>\\t<target>`` lines (blank lines ignored)."""
    parsed: list[SubmittedRecord] = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip("\n")
        if not line.strip():
            continue
        if "\t" not in line:
            raise _err(
                "malformed_tsv",
                f"malformed TSV line {line_no}: expected '<record-id><TAB><target>'",
            )
        record_id, target = line.split("\t", 1)
        if not record_id.strip():
            raise _err(
                "malformed_tsv", f"malformed TSV line {line_no}: missing record id"
            )
        parsed.append(SubmittedRecord(id=record_id.strip(), target=target))
    return ParsedSubmission(parsed)


def parse_block_submission(text: str) -> ParsedSubmission:
    """Parse the durable ``>>> <record-id>`` block format."""
    parsed: list[SubmittedRecord] = []
    current_id: str | None = None
    current_lines: list[str] = []
    seen: set[str] = set()
    translation_version: str | None = None
    profile: str | None = None

    def flush() -> None:
        nonlocal current_id, current_lines
        if current_id is None:
            return
        # Strip trailing separator lines (blank or comment) that sit between
        # this record and the next header (or EOF). Internal and leading
        # comment lines are preserved as target text.
        lines = list(current_lines)
        while lines and (not lines[-1].strip() or lines[-1].lstrip().startswith("#")):
            lines.pop()
        target = _trim_blank_edge_lines(lines)
        if not target:
            raise _err("empty_block_target", f"empty target for record {current_id}")
        parsed.append(SubmittedRecord(id=current_id, target=target))
        current_id = None
        current_lines = []

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        header = _BLOCK_HEADER_RE.match(raw_line)
        if header:
            flush()
            record_id = header.group("id").strip()
            if record_id in seen:
                raise _err(
                    "duplicate_block_record",
                    f"duplicate record id in block submission: {record_id}",
                )
            seen.add(record_id)
            current_id = record_id
            current_lines = []
            continue
        if current_id is None:
            stripped = raw_line.strip()
            if stripped.startswith("#"):
                content = stripped[1:].strip()
                if ":" in content:
                    key, value = content.split(":", 1)
                    key = key.strip().lower()
                    value = value.strip()
                    if key == "translation_version" and value and value != "none":
                        try:
                            translation_version = parse_version_ref(value).version_ref
                        except ValueError as exc:
                            raise _err("malformed_block", str(exc)) from None
                    elif key == "profile" and value and value != "none":
                        profile = value
                continue
            if stripped:
                raise _err(
                    "malformed_block",
                    f"malformed block submission line {line_no}: "
                    "expected '>>> <record-id>' before target text",
                )
            continue
        current_lines.append(raw_line)

    flush()
    if not parsed:
        raise _err(
            "empty_block_submission",
            "block submission did not contain any records",
        )
    return ParsedSubmission(
        parsed,
        translation_version=translation_version,
        profile=profile,
    )


def read_submission_file(path: Path) -> str:
    """Read a submission file, raising BooktxError (never a traceback) on failure."""
    try:
        return path.read_text("utf-8")
    except FileNotFoundError as exc:
        raise _err(
            "submission_file_not_found",
            f"submission file not found: {path}",
        ) from exc
    except OSError as exc:
        raise _err(
            "submission_file_unreadable",
            f"could not read submission file {path}: {exc}",
        ) from exc


def resolve_submission(
    *,
    record_id: str | None,
    target: str | None,
    input_format: str,
    stdin: bool,
    json_file: Path | None,
    input_file: Path | None,
) -> ParsedSubmission:
    """Select the input source and parse it into a :class:`ParsedSubmission`.

    Mirrors the previous inline dispatch in ``translate_insert``: a single
    ``--record-id``/``--target`` pair wins, then ``--json-file``, then
    ``--file`` (with ``--format``), then ``--stdin``. Raises BooktxError for
    any ambiguous or missing combination.
    """
    if record_id is not None or target is not None:
        if not record_id or target is None:
            raise _err(
                "incomplete_record_pair",
                "--record-id and --target must be supplied together",
            )
        return ParsedSubmission([SubmittedRecord(id=record_id, target=target)])
    if json_file is not None:
        return parse_json_submission(read_submission_file(json_file))
    if input_file is not None:
        raw = read_submission_file(input_file)
        if input_format == "json":
            return parse_json_submission(raw)
        if input_format == "tsv":
            return parse_tsv_submission(raw)
        return parse_block_submission(raw)
    if stdin:
        raw = sys.stdin.read()
        if input_format == "json":
            return parse_json_submission(raw)
        if input_format == "tsv":
            return parse_tsv_submission(raw)
        return parse_block_submission(raw)
    raise _err(
        "no_submission_input",
        "provide one of --record-id/--target, --json-file, --file, or --stdin",
    )


# Re-exported so static type checkers see BooktxError as part of this module's
# public surface (it is raised by every parser).
__all__.append("BooktxError")
