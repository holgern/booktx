"""Constrained inline XHTML fragment handling for EPUB records."""

from __future__ import annotations

from dataclasses import dataclass, field
from html import unescape
from typing import Literal
from xml.etree import ElementTree as ET

from booktx.models import Placeholder
from booktx.placeholders import protect_names

INLINE_XHTML_CODEC = "epub-inline-xhtml:v1"
PLAIN_CODEC = "plain:v1"

BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "body",
    "html",
    "table",
    "tr",
    "td",
    "ul",
    "ol",
    "li",
    "blockquote",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
}
OPAQUE_TAGS = {
    "code",
    "kbd",
    "samp",
    "var",
    "tt",
    "math",
    "svg",
    "img",
    "audio",
    "video",
    "object",
    "iframe",
}
FORBIDDEN_TAGS = BLOCK_TAGS | {"script", "style"}


@dataclass(slots=True, frozen=True)
class InlineSkeletonToken:
    kind: Literal["start", "end", "empty"]
    tag: str
    attrs: tuple[tuple[str, str], ...] = ()
    opaque: bool = False


@dataclass(slots=True)
class FragmentValidationIssue:
    rule: str
    message: str
    severity: Literal["warn", "error"] = "error"


@dataclass(slots=True)
class SanitizedFragment:
    xhtml: str
    visible_text: str
    skeleton: list[InlineSkeletonToken]
    issues: list[FragmentValidationIssue] = field(default_factory=list)


def _wrap(fragment: str) -> str:
    return f"<__booktx_root__>{fragment}</__booktx_root__>"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if tag.startswith("{") else tag


def _parse(fragment: str) -> tuple[ET.Element | None, list[FragmentValidationIssue]]:
    if "<!--" in fragment or "<?" in fragment:
        return None, [
            FragmentValidationIssue(
                "inline_xhtml_parseable",
                "comments and processing instructions are not allowed",
            )
        ]
    try:
        return ET.fromstring(_wrap(fragment)), []
    except ET.ParseError as exc:
        return None, [
            FragmentValidationIssue(
                "inline_xhtml_parseable", f"invalid XHTML fragment: {exc}"
            )
        ]


def _attrs(element: ET.Element) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(k), str(v)) for k, v in element.attrib.items()))


def _serialize_children(root: ET.Element) -> str:
    text = root.text or ""
    return text + "".join(
        ET.tostring(child, encoding="unicode", short_empty_elements=True)
        for child in list(root)
    )


def _visible_text(root: ET.Element) -> str:
    return "".join(root.itertext())


def _walk_skeleton(element: ET.Element, out: list[InlineSkeletonToken]) -> None:
    tag = _local(element.tag)
    attrs = _attrs(element)
    opaque = tag in OPAQUE_TAGS
    if len(element) == 0 and not (element.text or ""):
        out.append(InlineSkeletonToken("empty", tag, attrs, opaque))
        return
    out.append(InlineSkeletonToken("start", tag, attrs, opaque))
    for child in list(element):
        _walk_skeleton(child, out)
    out.append(InlineSkeletonToken("end", tag, (), opaque))


def inline_skeleton(fragment: str) -> list[InlineSkeletonToken]:
    root, issues = _parse(fragment)
    if root is None or issues:
        return []
    tokens: list[InlineSkeletonToken] = []
    for child in list(root):
        _walk_skeleton(child, tokens)
    return tokens


def strip_inline_xhtml(fragment: str) -> str:
    root, issues = _parse(fragment)
    if root is None or issues:
        return unescape(fragment)
    return _visible_text(root)


def _validate_tree(
    root: ET.Element,
    allowed_tags: set[str],
    allowed_attrs: dict[str, set[tuple[tuple[str, str], ...]]],
) -> list[FragmentValidationIssue]:
    issues: list[FragmentValidationIssue] = []
    for element in root.iter():
        tag = _local(element.tag)
        if tag == "__booktx_root__":
            continue
        if tag in FORBIDDEN_TAGS:
            issues.append(
                FragmentValidationIssue(
                    "inline_xhtml_no_block_tags",
                    f"tag <{tag}> is not allowed in EPUB inline XHTML",
                )
            )
        if tag not in allowed_tags:
            issues.append(
                FragmentValidationIssue(
                    "inline_xhtml_preserved",
                    f"target added tag <{tag}> not present in source",
                )
            )
        attrs = _attrs(element)
        if attrs not in allowed_attrs.get(tag, set()):
            issues.append(
                FragmentValidationIssue(
                    "inline_xhtml_no_new_attributes",
                    f"attributes for <{tag}> do not match the source",
                )
            )
        for attr, _value in attrs:
            if attr.lower().startswith("on"):
                issues.append(
                    FragmentValidationIssue(
                        "inline_xhtml_no_new_attributes",
                        f"event handler attribute {attr!r} is not allowed",
                    )
                )
    return issues


def _opaque_serialized(fragment: str) -> list[str]:
    root, issues = _parse(fragment)
    if root is None or issues:
        return []
    return [
        ET.tostring(e, encoding="unicode", short_empty_elements=True)
        for e in root.iter()
        if _local(e.tag) in OPAQUE_TAGS
    ]


def sanitize_target_fragment(target: str, source_fragment: str) -> SanitizedFragment:
    source_root, source_issues = _parse(source_fragment)
    target_root, target_issues = _parse(target)
    issues = [*source_issues, *target_issues]
    if source_root is None or target_root is None:
        return SanitizedFragment(target, strip_inline_xhtml(target), [], issues)

    source_skeleton = inline_skeleton(source_fragment)
    target_skeleton = inline_skeleton(target)
    allowed_tags = {token.tag for token in source_skeleton}
    allowed_attrs: dict[str, set[tuple[tuple[str, str], ...]]] = {}
    for token in source_skeleton:
        if token.kind in {"start", "empty"}:
            allowed_attrs.setdefault(token.tag, set()).add(token.attrs)
    issues.extend(_validate_tree(target_root, allowed_tags, allowed_attrs))

    if source_skeleton != target_skeleton:
        issues.append(
            FragmentValidationIssue(
                "inline_xhtml_preserved",
                "target inline XHTML skeleton does not match the source",
            )
        )

    if _opaque_serialized(source_fragment) != _opaque_serialized(target):
        issues.append(
            FragmentValidationIssue(
                "inline_xhtml_opaque_preserved",
                "opaque inline elements must be preserved exactly",
            )
        )

    visible = _visible_text(target_root)
    if not visible.strip():
        issues.append(
            FragmentValidationIssue(
                "inline_xhtml_visible_text_not_empty", "target visible text is empty"
            )
        )
    if (
        ("–" in source_fragment or "—" in source_fragment)
        and "–" not in target
        and "—" not in target
    ):
        issues.append(
            FragmentValidationIssue(
                "dash_semantic_cue_missing",
                "source contains a dash cue but target does not",
                "warn",
            )
        )

    return SanitizedFragment(
        _serialize_children(target_root), visible, target_skeleton, issues
    )


def protect_names_in_xhtml_text_nodes(
    fragment: str, protected_terms: list[str]
) -> tuple[str, list[Placeholder]]:
    root, issues = _parse(fragment)
    if root is None or issues:
        protected = protect_names(fragment, protected_terms)
        return protected.text, protected.placeholders
    placeholders: list[Placeholder] = []
    counter = 1
    for node in root.iter():
        for attr in ("text", "tail"):
            text = getattr(node, attr)
            if not text:
                continue
            protected = protect_names(text, protected_terms, start_index=counter)
            setattr(node, attr, protected.text)
            placeholders.extend(protected.placeholders)
            counter += len(protected.placeholders)
    return _serialize_children(root), placeholders
