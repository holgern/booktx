"""booktx EPUB output-language and hyphenation policy.

This module owns booktx-side policy resolution, deterministic CSS construction,
CSS-conflict scanning, and post-build audit. It does **not** rewrite ZIP or XML
content itself: that is delegated to text2epub's generic output-rewrite engine
via :class:`text2epub.OutputRewriteOptions`.

See ``booktx_epub_hyphenation_fix.md`` for the full specification and contract.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Literal
from zipfile import BadZipFile, ZipFile

from booktx.config import Project
from booktx.models import EpubOutputConfig

__all__ = [
    "POLICY_STYLE_ID",
    "PolicyError",
    "EpubOutputPolicy",
    "EpubOutputPolicyReport",
    "CssConflict",
    "resolve_epub_output_policy",
    "resolve_language_tag",
    "validate_language_tag",
    "build_policy_css",
    "scan_css_conflicts",
    "to_text2epub_output_rewrite",
    "reconcile_css_injection",
    "audit_epub_output_policy",
]

# The author policy is injected under this stable id so it can be replaced
# idempotently across rebuilds. text2epub guarantees at most one element with
# this id per targeted document.
POLICY_STYLE_ID = "booktx-output-policy"

LanguagePolicy = Literal["target", "source", "preserve", "explicit"]
HyphenationMode = Literal["auto", "manual", "none", "preserve"]


class PolicyError(Exception):
    """User-facing policy resolution or audit error."""


@dataclass(frozen=True, slots=True)
class EpubOutputPolicy:
    """Fully resolved, effective booktx EPUB output policy.

    ``language`` is the resolved BCP-47-style tag when policy is not
    ``preserve``; it is ``None`` only for a fully preserving policy.
    """

    language_policy: LanguagePolicy
    language: str | None
    hyphenation: HyphenationMode
    inject_css: bool
    patch_body_language: bool


@dataclass(frozen=True, slots=True)
class CssConflict:
    """One CSS declaration that may defeat the generated policy.

    Cascade conflicts are warnings, not proof of rendered behaviour: determining
    computed style requires a browser engine.
    """

    entry: str
    declaration: str


@dataclass(slots=True)
class EpubOutputPolicyReport:
    """Post-build audit/reconciliation report produced by booktx.

    ``changed_entries`` mirrors the text2epub archive-order union of replacement
    and output-rewrite changes. The remaining lists are reconciled from the
    upstream :class:`text2epub.OutputRewriteReport` plus booktx's own audits.
    """

    applied: bool = False
    opf_path: str | None = None
    language_policy: LanguagePolicy = "preserve"
    language: str | None = None
    hyphenation: HyphenationMode = "preserve"
    changed_entries: list[str] = field(default_factory=list)
    targeted_xhtml_entries: list[str] = field(default_factory=list)
    patched_xhtml_entries: list[str] = field(default_factory=list)
    css_injected_entries: list[str] = field(default_factory=list)
    fixed_layout_skipped_entries: list[str] = field(default_factory=list)
    old_primary_language: str | None = None
    new_primary_language: str | None = None
    warnings: list[dict[str, str]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Language tag handling
# --------------------------------------------------------------------------- #

# Conservative BCP-47-style validator. Booktx does not ship a full registry;
# it rejects obviously malformed or underscore-locale inputs with a corrective
# message rather than silently rewriting them.
#
# A tag is a hyphen-separated sequence of alphanumeric subtags. The primary
# subtag for language tags is 2-3 alpha; region may be 2 alpha or 3 numeric.
_BCP47_RE = re.compile(r"^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})*$")


def validate_language_tag(tag: str) -> str:
    """Validate and normalize a BCP-47-style language tag.

    Raises :class:`PolicyError` with a corrective message for underscore
    locale forms (e.g. ``de_DE``) and for syntactically invalid tags.
    """
    if not tag or not tag.strip():
        raise PolicyError("language tag must not be empty")
    raw = tag.strip()
    if "_" in raw:
        raise PolicyError(
            f"language tag {raw!r} uses an underscore locale form; "
            f"use a hyphen BCP-47-style tag such as {raw.replace('_', '-')!r}"
        )
    if not _BCP47_RE.fullmatch(raw):
        raise PolicyError(f"language tag {raw!r} is not a valid BCP-47-style tag")
    # Language subtags are case-insensitive; normalize the canonical lower-case
    # primary subtag while leaving the rest as given.
    parts = raw.split("-")
    parts[0] = parts[0].lower()
    return "-".join(parts)


def resolve_language_tag(
    project: Project,
    policy: LanguagePolicy,
    explicit: str | None,
) -> str | None:
    """Resolve the output language tag for a non-preserve policy.

    Returns ``None`` for ``preserve``. Raises :class:`PolicyError` for an
    ``explicit`` policy without a language or for any unresolvable/invalid tag.
    """
    cfg = project.config
    if policy == "preserve":
        return None
    if policy == "target":
        tag = cfg.target_locale or cfg.target_language
    elif policy == "source":
        tag = cfg.source_language
    elif policy == "explicit":
        if not explicit or not explicit.strip():
            raise PolicyError(
                "language_policy='explicit' requires a non-empty epub_output.language"
            )
        tag = explicit
    else:  # pragma: no cover - exhaustive enum
        raise PolicyError(f"unknown language policy {policy!r}")

    if not tag or not tag.strip():
        raise PolicyError(f"could not resolve output language for policy {policy!r}")
    return validate_language_tag(tag)


# --------------------------------------------------------------------------- #
# Policy resolution
# --------------------------------------------------------------------------- #


def resolve_epub_output_policy(project: Project) -> EpubOutputPolicy:
    """Resolve the effective EPUB output policy for a project.

    Defaults depend on profile kind:

    * translation profiles and legacy translation projects default to
      ``target`` language / ``auto`` hyphenation;
    * pass-through profiles default to ``preserve`` / ``preserve``
      (byte-identical output).

    An explicit ``epub_output`` block overrides the defaults field-by-field.
    """
    stored = _stored_policy(project)
    is_pass_through = _is_pass_through(project)

    if stored is not None:
        return EpubOutputPolicy(
            language_policy=stored.language_policy,
            language=resolve_language_tag(
                project, stored.language_policy, stored.language
            ),
            hyphenation=stored.hyphenation,
            inject_css=stored.inject_css,
            patch_body_language=stored.patch_body_language,
        )

    if is_pass_through:
        return EpubOutputPolicy(
            language_policy="preserve",
            language=None,
            hyphenation="preserve",
            inject_css=False,
            patch_body_language=False,
        )
    return EpubOutputPolicy(
        language_policy="target",
        language=resolve_language_tag(project, "target", None),
        hyphenation="auto",
        inject_css=True,
        patch_body_language=False,
    )


def _stored_policy(project: Project) -> EpubOutputConfig | None:
    """Return the explicit epub_output config from profile or legacy config."""
    if (
        project.profile_config is not None
        and project.profile_config.epub_output is not None
    ):
        return project.profile_config.epub_output
    return project.config.epub_output


def _is_pass_through(project: Project) -> bool:
    if project.profile_config is not None:
        return project.profile_config.kind == "pass-through"
    return False


# --------------------------------------------------------------------------- #
# Deterministic CSS generation
# --------------------------------------------------------------------------- #

_AUTO_CSS = """\
/* generated by booktx */
html, body {
  -epub-hyphens: auto;
  hyphens: auto;
}

p, li, blockquote, dd, dt {
  -epub-hyphens: auto;
  hyphens: auto;
  -epub-word-break: normal;
  word-break: normal;
  overflow-wrap: normal;
  word-wrap: normal;
}

h1, h2, h3, h4, h5, h6,
nav, .reader-toc, .title-page {
  -epub-hyphens: none;
  hyphens: none;
}

pre, code, kbd, samp {
  -epub-hyphens: manual;
  hyphens: manual;
  overflow-wrap: anywhere;
}
"""

_MANUAL_CSS = """\
/* generated by booktx */
html, body, p, li, blockquote, dd, dt {
  -epub-hyphens: manual;
  hyphens: manual;
  -epub-word-break: normal;
  word-break: normal;
  overflow-wrap: normal;
  word-wrap: normal;
}
"""

_DISABLED_CSS = """\
/* generated by booktx */
html, body, p, li, blockquote, dd, dt {
  -epub-hyphens: none;
  hyphens: none;
}
"""


def build_policy_css(hyphenation: HyphenationMode) -> str:
    """Return the deterministic CSS for a hyphenation mode.

    Returns an empty string for ``preserve`` (no CSS is injected).
    """
    if hyphenation == "auto":
        return _AUTO_CSS
    if hyphenation == "manual":
        return _MANUAL_CSS
    if hyphenation == "none":
        return _DISABLED_CSS
    return ""  # preserve


# --------------------------------------------------------------------------- #
# CSS conflict scanning
# --------------------------------------------------------------------------- #

# Declarations that may defeat the generated policy. The scan is intentionally
# a conservative regex pass over linked and inline CSS; it cannot determine
# computed style, so conflicts are reported as warnings.
_CONFLICT_PATTERNS = [
    re.compile(r"-epub-word-break\s*:\s*break-all", re.I),
    re.compile(r"(?<![-\w])word-break\s*:\s*break-all", re.I),
    re.compile(r"overflow-wrap\s*:\s*anywhere", re.I),
    re.compile(r"word-wrap\s*:\s*break-word", re.I),
    re.compile(r"-epub-hyphens\s*:\s*[^;}{]+", re.I),
    re.compile(r"(?<![-\w])hyphens\s*:\s*[^;}{]+", re.I),
]

# Any of the relevant declarations escalated with !important can override the
# author policy regardless of specificity.
_IMPORTANT_RE = re.compile(
    r"(?:-epub-hyphens|hyphens|-epub-word-break|word-break|overflow-wrap|word-wrap)"
    r"\s*:[^;}{]*!\s*important",
    re.I,
)

_CSS_EXTENSIONS = (".css",)


def scan_css_conflicts(
    archive_or_bytes: Path | str | bytes | IO[bytes] | ZipFile,
) -> list[CssConflict]:
    """Scan linked and inline CSS in an EPUB for policy-defeating declarations.

    Accepts a path, raw bytes, a file-like object, or an open
    :class:`zipfile.ZipFile`. Returns conflicts grouped per matched
    declaration with the owning entry name.

    This is a best-effort heuristic scan only; it is not a cascade resolver.
    """
    conflicts: list[CssConflict] = []
    entries = _iter_text_entries(archive_or_bytes)
    for name, text in entries:
        if not name.lower().endswith(_CSS_EXTENSIONS):
            continue
        conflicts.extend(_scan_one(name, text))
    return conflicts


def _scan_one(name: str, text: str) -> list[CssConflict]:
    found: list[CssConflict] = []
    seen: set[tuple[str, str]] = set()
    for pattern in _CONFLICT_PATTERNS:
        for match in pattern.finditer(text):
            declaration = " ".join(match.group(0).split())
            key = (name, declaration)
            if key in seen:
                continue
            seen.add(key)
            found.append(CssConflict(entry=name, declaration=declaration))
    for match in _IMPORTANT_RE.finditer(text):
        declaration = " ".join(match.group(0).split())
        key = (name, declaration)
        if key in seen:
            continue
        seen.add(key)
        found.append(CssConflict(entry=name, declaration=declaration))
    found.sort(key=lambda c: (c.entry, c.declaration))
    return found


def _iter_text_entries(
    archive_or_bytes: Path | str | bytes | IO[bytes] | ZipFile,
) -> list[tuple[str, str]]:
    """Open the EPUB if needed and return (entry_name, decoded_text) pairs."""
    if isinstance(archive_or_bytes, ZipFile):
        return _read_all(archive_or_bytes)
    if isinstance(archive_or_bytes, (bytes, bytearray)):
        import io

        with ZipFile(io.BytesIO(bytes(archive_or_bytes))) as zf:
            return _read_all(zf)
    if hasattr(archive_or_bytes, "read"):
        data = archive_or_bytes.read()
        import io

        with ZipFile(io.BytesIO(data)) as zf:
            return _read_all(zf)
    # Path or str
    with ZipFile(Path(archive_or_bytes)) as zf:
        return _read_all(zf)


def _read_all(zf: ZipFile) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for name in zf.namelist():
        if name.lower().endswith(_CSS_EXTENSIONS):
            out.append((name, zf.read(name).decode("utf-8", "replace")))
    return out


# --------------------------------------------------------------------------- #
# text2epub mapping
# --------------------------------------------------------------------------- #


def to_text2epub_output_rewrite(policy: EpubOutputPolicy):  # type: ignore[no-untyped-def]  # Phase 0 baseline: return type is a lazily-imported text2epub OutputRewriteOptions; see docs/mypy-baseline.md
    """Map a resolved policy to a text2epub ``OutputRewriteOptions`` or None.

    Returns ``None`` for a fully preserving policy so text2epub retains its
    existing replacement-only or byte-copy behaviour. Otherwise it supplies the
    resolved language flags, generated CSS, ``style_id`` and content scope.
    """
    is_preserve_language = policy.language_policy == "preserve"
    # hyphenation='preserve' suppresses CSS injection regardless of inject_css.
    css = (
        build_policy_css(policy.hyphenation) if policy.hyphenation != "preserve" else ""
    )
    inject_css = policy.inject_css and bool(css)

    if is_preserve_language and not inject_css and not policy.patch_body_language:
        return None

    from text2epub import OutputRewriteOptions  # type: ignore[import-not-found]

    return OutputRewriteOptions(
        language=policy.language,
        patch_package_language=not is_preserve_language,
        patch_content_language=not is_preserve_language,
        patch_body_language=policy.patch_body_language and not is_preserve_language,
        css_text=css if inject_css else None,
        style_id=POLICY_STYLE_ID,
        content_scope="spine-and-navigation",
    )


# --------------------------------------------------------------------------- #


def is_effectively_preserving(policy: EpubOutputPolicy) -> bool:
    """True when the policy maps to no upstream rewrite (byte identity kept)."""
    return to_text2epub_output_rewrite(policy) is None


# --------------------------------------------------------------------------- #
# Post-build audit
# --------------------------------------------------------------------------- #

_LANG_ATTR_RE = re.compile(
    r"<html\b[^>]*\b(?:xml:lang|lang)\s*=\s*\"([^\"]+)\"", re.I | re.S
)


def reconcile_css_injection(
    epub_path: Path,
    *,
    upstream_css_entries: Sequence[str],
) -> list[str]:
    """Verify every CSS entry text2epub reported as injected is present exactly once.

    ``build`` calls this with the ``css_injected_entries`` list from the upstream
    :class:`text2epub.OutputRewriteReport`. Each such entry must carry exactly
    one ``style#<POLICY_STYLE_ID>``; this is the post-build idempotency check
    that anchors the per-document contract to text2epub's own reporting rather
    than to booktx's guessing of which documents are eligible.

    Returns the validated entry list. Raises :class:`PolicyError` otherwise.
    """
    validated: list[str] = []
    with ZipFile(epub_path) as zf:
        for name in upstream_css_entries:
            text = zf.read(name).decode("utf-8", "replace")
            count = text.count(f'id="{POLICY_STYLE_ID}"')
            if count != 1:
                raise PolicyError(
                    f"{name}: expected exactly one style#{POLICY_STYLE_ID}, "
                    f"found {count}"
                )
            validated.append(name)
    return validated


def audit_epub_output_policy(
    epub_path: Path,
    *,
    extraction_hrefs: Sequence[str],
    policy: EpubOutputPolicy,
) -> EpubOutputPolicyReport:
    """Audit a built EPUB against the resolved policy contract.

    Verifies, for non-preserving policy:

    * the primary OPF ``dc:language`` equals the resolved language;
    * every targeted XHTML root has matching ``lang`` and ``xml:lang``;
    * no document carries more than one ``style#<POLICY_STYLE_ID>`` (idempotency);
    * body attributes match only when body patching is enabled.

    Per-document "exactly one style on every eligible document" is reconciled
    against text2epub's own ``css_injected_entries`` via
    :func:`reconcile_css_injection` in the build flow.

    For a fully preserving policy, verifies that no policy style was introduced.

    ``extraction_hrefs`` are the OPF-relative hrefs booktx knows about from the
    extraction manifest; the audit reports targeted/xhtml entries from the
    archive itself.

    Raises :class:`PolicyError` on a structural policy failure. CSS cascade
    conflicts are returned as warnings via :func:`scan_css_conflicts`, not as
    structural errors.
    """
    report = EpubOutputPolicyReport(
        applied=not is_effectively_preserving(policy),
        language_policy=policy.language_policy,
        language=policy.language,
        hyphenation=policy.hyphenation,
    )
    try:
        with ZipFile(epub_path) as zf:
            names = zf.namelist()
            opf_path = _find_opf_path(zf, names)
            report.opf_path = opf_path
            xhtml_entries = [
                n for n in names if n.lower().endswith((".xhtml", ".html"))
            ]
            report.targeted_xhtml_entries = xhtml_entries
            if opf_path is not None:
                primary = _opf_primary_language(
                    zf.read(opf_path).decode("utf-8", "replace")
                )
                report.old_primary_language = None
                report.new_primary_language = primary
            # CSS conflicts are warnings regardless of policy.
            report.warnings = [
                {"entry": c.entry, "declaration": c.declaration}
                for c in scan_css_conflicts(zf)
            ]
            if report.applied:
                _audit_non_preserving(zf, opf_path, xhtml_entries, policy, report)
            else:
                _audit_preserving(zf, xhtml_entries, report)
    except BadZipFile as exc:
        raise PolicyError(f"built EPUB is not a valid archive: {exc}") from exc
    return report


def _audit_non_preserving(
    zf: ZipFile,
    opf_path: str | None,
    xhtml_entries: list[str],
    policy: EpubOutputPolicy,
    report: EpubOutputPolicyReport,
) -> None:
    assert policy.language is not None
    if opf_path is not None:
        primary = _opf_primary_language(zf.read(opf_path).decode("utf-8", "replace"))
        if primary != policy.language:
            raise PolicyError(
                f"primary OPF dc:language is {primary!r}, expected {policy.language!r}"
            )
    css_enabled = policy.inject_css and policy.hyphenation != "preserve"
    patched: list[str] = []
    injected: list[str] = []
    fixed_layout: list[str] = []
    for name in xhtml_entries:
        text = zf.read(name).decode("utf-8", "replace")
        if _is_fixed_layout(text):
            fixed_layout.append(name)
            continue
        if not _root_lang_matches(text, policy.language):
            raise PolicyError(
                f"{name}: root html lang/xml:lang does not match {policy.language!r}"
            )
        patched.append(name)
        count = text.count(f'id="{POLICY_STYLE_ID}"')
        # Idempotency: no document may carry more than one policy style.
        if count > 1:
            raise PolicyError(
                f"{name}: found {count} style#{POLICY_STYLE_ID} entries "
                "(expected at most one)"
            )
        if css_enabled and count == 1:
            injected.append(name)
    if policy.patch_body_language:
        for name in xhtml_entries:
            text = zf.read(name).decode("utf-8", "replace")
            if _is_fixed_layout(text):
                continue
            if not _body_lang_matches(text, policy.language):
                raise PolicyError(
                    f"{name}: body lang/xml:lang does not match {policy.language!r}"
                )
    report.patched_xhtml_entries = patched
    report.css_injected_entries = injected
    report.fixed_layout_skipped_entries = fixed_layout


def _audit_preserving(
    zf: ZipFile, xhtml_entries: list[str], report: EpubOutputPolicyReport
) -> None:
    for name in xhtml_entries:
        text = zf.read(name).decode("utf-8", "replace")
        if f'id="{POLICY_STYLE_ID}"' in text:
            raise PolicyError(
                f"{name}: preserve policy must not introduce style#{POLICY_STYLE_ID}"
            )


def _find_opf_path(zf: ZipFile, names: list[str]) -> str | None:
    container = "META-INF/container.xml"
    if container in names:
        text = zf.read(container).decode("utf-8", "replace")
        match = re.search(r"full-path\s*=\s*\"([^\"]+)\"", text)
        if match and match.group(1) in names:
            return match.group(1)
    opfs = [n for n in names if n.lower().endswith(".opf")]
    return opfs[0] if opfs else None


def _opf_primary_language(opf_text: str) -> str | None:
    match = re.search(
        r"<dc:language[^>]*>\s*([^<]+?)\s*</dc:language>", opf_text, re.I | re.S
    )
    return match.group(1).strip() if match else None


def _root_lang_matches(text: str, language: str) -> bool:
    html_match = re.search(r"<html\b[^>]*>", text, re.I | re.S)
    if html_match is None:
        return False
    tag = html_match.group(0)
    lang = re.search(r'\blang\s*=\s*"([^"]+)"', tag, re.I)
    xml_lang = re.search(r'\bxml:lang\s*=\s*"([^"]+)"', tag, re.I)
    if lang is None or xml_lang is None:
        return False
    return lang.group(1) == language and xml_lang.group(1) == language


def _body_lang_matches(text: str, language: str) -> bool:
    body_match = re.search(r"<body\b[^>]*>", text, re.I | re.S)
    if body_match is None:
        return False
    tag = body_match.group(0)
    lang = re.search(r'\blang\s*=\s*"([^"]+)"', tag, re.I)
    xml_lang = re.search(r'\bxml:lang\s*=\s*"([^"]+)"', tag, re.I)
    if "lang" not in tag:
        # No lang attributes on body at all: treat as not matching when patching
        # is requested.
        return False
    lang_ok = lang is None or lang.group(1) == language
    xml_ok = xml_lang is None or xml_lang.group(1) == language
    return lang_ok and xml_ok


def _is_fixed_layout(text: str) -> bool:
    head_match = re.search(r"<head\b.*?</head>", text, re.I | re.S)
    if head_match is None:
        return False
    head = head_match.group(0)
    return bool(
        re.search(
            r"<meta[^>]+name\s*=\s*[\"']viewport[\"'][^>]+content\s*=\s*[\"']"
            r"[^\"']*(?:width\s*=\s*\d+|height\s*=\s*\d+)",
            head,
            re.I,
        )
    )
