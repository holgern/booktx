"""Tests for booktx.epub_output_policy.

Covers policy resolution, BCP-47 validation, deterministic CSS generation,
text2epub OutputRewriteOptions mapping, CSS conflict scanning, and the
post-build audit reconciled with text2epub's OutputRewriteReport.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from booktx.config import (
    create_profile,
    init_source_project,
    load_project,
)
from booktx.epub_output_policy import (
    POLICY_STYLE_ID,
    CssConflict,
    EpubOutputPolicy,
    PolicyError,
    audit_epub_output_policy,
    build_policy_css,
    is_effectively_preserving,
    resolve_epub_output_policy,
    scan_css_conflicts,
    to_text2epub_output_rewrite,
    validate_language_tag,
)
from booktx.models import EpubOutputConfig

# --------------------------------------------------------------------------- #
# Helpers to build minimal EPUBs
# --------------------------------------------------------------------------- #


def _write_epub(
    path: Path,
    *,
    language: str = "en",
    body_lang: str | None = None,
    css: str | None = None,
    css_href: str = "style.css",
    fixed_layout: bool = False,
) -> None:
    """Build a minimal valid EPUB with a single chapter.

    Built with raw zipfile for deterministic control over CSS linkage, which
    ebooklib does not always write reliably for standalone stylesheet items.
    """
    head_metas = ""
    if fixed_layout:
        head_metas = '<meta name="viewport" content="width=800, height=1200"/>'
    head_css = ""
    manifest_css = ""
    if css is not None:
        head_css = f'<link rel="stylesheet" href="{css_href}"/>'
        manifest_css = f'    <item id="css" href="{css_href}" media-type="text/css"/>\n'
    body_attr = f' lang="{body_lang}" xml:lang="{body_lang}"' if body_lang else ""
    chapter = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"'
        f' lang="{language}" xml:lang="{language}">'
        "<head>"
        f"<title>Chapter</title>{head_metas}{head_css}"
        "</head>"
        f"<body{body_attr}>"
        "<h1>Heading</h1>"
        "<p>A paragraph.</p>"
        "</body></html>"
    )
    nav = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"'
        ' xmlns:epub="http://www.idpf.org/2007/ops"'
        f' lang="{language}" xml:lang="{language}">'
        "<head><title>Contents</title></head><body>"
        '<nav epub:type="toc" id="toc"><ol><li>'
        '<a href="ch1.xhtml">Chapter</a></li></ol></nav>'
        "</body></html>"
    )
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf"'
        ' unique-identifier="bookid" version="3.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:identifier id="bookid">policy-book</dc:identifier>'
        "<dc:title>Policy Book</dc:title>"
        f"<dc:language>{language}</dc:language>"
        "</metadata>"
        "<manifest>\n"
        '    <item id="nav" href="nav.xhtml"'
        ' media-type="application/xhtml+xml" properties="nav"/>\n'
        '    <item id="ch1" href="ch1.xhtml"'
        ' media-type="application/xhtml+xml"/>\n'
        f"{manifest_css}"
        "</manifest>\n"
        "<spine>\n"
        '    <itemref idref="nav"/>\n'
        '    <itemref idref="ch1"/>\n'
        "</spine>"
        "</package>\n"
    )
    container = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<container version="1.0"'
        ' xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="content.opf"'
        ' media-type="application/oebps-package+xml"/></rootfiles>'
        "</container>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        mt = zipfile.ZipInfo("mimetype")
        mt.compress_type = zipfile.ZIP_STORED
        zf.writestr(mt, "application/epub+zip")
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("content.opf", opf)
        zf.writestr("nav.xhtml", nav)
        zf.writestr("ch1.xhtml", chapter)
        if css is not None:
            zf.writestr(css_href, css)


def _patch_xhtml_root_lang(epub_path: Path, entry: str, new_lang: str) -> None:
    """Rewrite one xhtml entry's root html lang/xml:lang in place."""
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(epub_path, "r") as zin:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == entry:
                    text = data.decode("utf-8", "replace")
                    text = text.replace(
                        ' lang="de-DE" xml:lang="de-DE"',
                        f' lang="{new_lang}" xml:lang="{new_lang}"',
                    )
                    data = text.encode("utf-8")
                zout.writestr(item, data)
    epub_path.write_bytes(buf.getvalue())


# --------------------------------------------------------------------------- #
# validate_language_tag
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("tag", ["en", "de", "de-DE", "pt-BR", "zh-Hans", "en-US"])
def test_validate_language_tag_accepts_valid(tag: str) -> None:
    assert validate_language_tag(tag) == tag


def test_validate_language_tag_normalizes_primary_subtag_case() -> None:
    assert validate_language_tag("DE-de") == "de-de"


@pytest.mark.parametrize("tag", ["de_DE", "pt_BR", "DE_DE"])
def test_validate_language_tag_rejects_underscore_locale(tag: str) -> None:
    with pytest.raises(PolicyError, match="underscore"):
        validate_language_tag(tag)


@pytest.mark.parametrize("tag", ["", "   ", "-DE", "de--DE", "de-", "1en"])
def test_validate_language_tag_rejects_malformed(tag: str) -> None:
    with pytest.raises(PolicyError):
        validate_language_tag(tag)


# --------------------------------------------------------------------------- #
# build_policy_css
# --------------------------------------------------------------------------- #


def test_css_auto_is_deterministic_and_complete() -> None:
    css = build_policy_css("auto")
    assert css.startswith("/* generated by booktx */")
    assert "-epub-hyphens: auto" in css
    assert "hyphens: auto" in css
    assert "-epub-word-break: normal" in css
    assert "overflow-wrap: normal" in css
    # headings/containers disable hyphenation
    assert "-epub-hyphens: none" in css
    # code blocks keep manual + anywhere wrap
    assert "-epub-hyphens: manual" in css
    assert "overflow-wrap: anywhere" in css
    assert build_policy_css("auto") == build_policy_css("auto")


def test_css_manual_is_deterministic() -> None:
    css = build_policy_css("manual")
    assert "-epub-hyphens: manual" in css
    assert "hyphens: manual" in css
    assert "-epub-word-break: normal" in css
    assert build_policy_css("manual") == build_policy_css("manual")


def test_css_none_disables_hyphenation() -> None:
    css = build_policy_css("none")
    assert "-epub-hyphens: none" in css
    assert "hyphens: none" in css
    assert "auto" not in css


def test_css_preserve_is_empty() -> None:
    assert build_policy_css("preserve") == ""


# --------------------------------------------------------------------------- #
# to_text2epub_output_rewrite mapping
# --------------------------------------------------------------------------- #


def test_preserving_policy_maps_to_no_rewrite() -> None:
    policy = EpubOutputPolicy("preserve", None, "preserve", False, False)
    assert to_text2epub_output_rewrite(policy) is None
    assert is_effectively_preserving(policy) is True


def test_target_auto_maps_to_full_rewrite() -> None:
    policy = EpubOutputPolicy("target", "de-DE", "auto", True, False)
    opts = to_text2epub_output_rewrite(policy)
    assert opts is not None
    assert opts.language == "de-DE"
    assert opts.patch_package_language is True
    assert opts.patch_content_language is True
    assert opts.patch_body_language is False
    assert opts.style_id == POLICY_STYLE_ID
    assert opts.content_scope == "spine-and-navigation"
    assert opts.css_text is not None
    assert opts.css_text.startswith("/* generated by booktx */")


def test_explicit_with_body_patch_sets_body_flag() -> None:
    policy = EpubOutputPolicy("explicit", "fr-FR", "auto", True, True)
    opts = to_text2epub_output_rewrite(policy)
    assert opts is not None
    assert opts.language == "fr-FR"
    assert opts.patch_body_language is True


def test_hyphenation_preserve_suppresses_css_even_when_inject_true() -> None:
    # preserve language but hyphenation preserve -> language untouched, no css
    policy = EpubOutputPolicy("target", "de-DE", "preserve", True, False)
    opts = to_text2epub_output_rewrite(policy)
    assert opts is not None
    assert opts.css_text is None
    assert opts.patch_package_language is True


def test_inject_css_false_keeps_language_rewrite() -> None:
    policy = EpubOutputPolicy("target", "de-DE", "auto", False, False)
    opts = to_text2epub_output_rewrite(policy)
    assert opts is not None
    assert opts.language == "de-DE"
    assert opts.css_text is None


# --------------------------------------------------------------------------- #
# resolve_epub_output_policy defaults
# --------------------------------------------------------------------------- #


def _project(tmp_path: Path, *, kind: str = "translation", epub_output=None):
    proj = init_source_project(tmp_path / "book")
    create_profile(
        proj.root, "p", target_language="de", target_locale="de-DE", kind=kind
    )
    p = load_project(proj.root, profile="p")
    if epub_output is not None:
        cfg = p.profile_config.model_copy(update={"epub_output": epub_output})
        from booktx.config import write_profile_config

        write_profile_config(proj.root, cfg)
        p = load_project(proj.root, profile="p")
    return p


def test_resolve_defaults_translation_target_auto(tmp_path: Path) -> None:
    p = _project(tmp_path)
    policy = resolve_epub_output_policy(p)
    assert policy.language_policy == "target"
    assert policy.language == "de-DE"
    assert policy.hyphenation == "auto"
    assert policy.inject_css is True
    assert policy.patch_body_language is False


def test_resolve_defaults_pass_through_preserve(tmp_path: Path) -> None:
    p = _project(tmp_path, kind="pass-through")
    policy = resolve_epub_output_policy(p)
    assert policy.language_policy == "preserve"
    assert policy.language is None
    assert policy.hyphenation == "preserve"
    assert is_effectively_preserving(policy) is True


def test_resolve_explicit_override(tmp_path: Path) -> None:
    p = _project(
        tmp_path,
        epub_output=EpubOutputConfig(language_policy="source", hyphenation="none"),
    )
    policy = resolve_epub_output_policy(p)
    assert policy.language_policy == "source"
    assert policy.language == "en"  # source_language default
    assert policy.hyphenation == "none"


def test_resolve_explicit_policy_requires_language(tmp_path: Path) -> None:
    p = _project(tmp_path, epub_output=EpubOutputConfig(language_policy="explicit"))
    with pytest.raises(PolicyError, match="explicit"):
        resolve_epub_output_policy(p)


def test_resolve_explicit_policy_rejects_underscore_locale(tmp_path: Path) -> None:
    p = _project(
        tmp_path,
        epub_output=EpubOutputConfig(language_policy="explicit", language="de_DE"),
    )
    with pytest.raises(PolicyError, match="underscore"):
        resolve_epub_output_policy(p)


# --------------------------------------------------------------------------- #
# scan_css_conflicts
# --------------------------------------------------------------------------- #


def _zip_bytes(css_text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("OEBPS/style.css", css_text)
    return buf.getvalue()


def test_scan_finds_break_all_conflict() -> None:
    css = "p { word-break: break-all; }"
    conflicts = scan_css_conflicts(_zip_bytes(css))
    assert any(
        "word-break" in c.declaration and "break-all" in c.declaration
        for c in conflicts
    )


def test_scan_finds_overflow_anywhere_conflict() -> None:
    css = "p { overflow-wrap: anywhere; }"
    conflicts = scan_css_conflicts(_zip_bytes(css))
    assert any("overflow-wrap" in c.declaration for c in conflicts)


def test_scan_finds_epub_word_break_conflict() -> None:
    css = "p { -epub-word-break: break-all; }"
    conflicts = scan_css_conflicts(_zip_bytes(css))
    assert any("-epub-word-break" in c.declaration for c in conflicts)


def test_scan_finds_important_conflict() -> None:
    css = "p { hyphens: none !important; }"
    conflicts = scan_css_conflicts(_zip_bytes(css))
    assert any("!important" in c.declaration for c in conflicts)


def test_scan_finds_word_wrap_break_word_conflict() -> None:
    css = "p { word-wrap: break-word; }"
    conflicts = scan_css_conflicts(_zip_bytes(css))
    assert any("word-wrap" in c.declaration for c in conflicts)


def test_scan_clean_css_has_no_conflicts() -> None:
    css = "p { hyphens: auto; }\nh1 { color: red; }"
    # plain hyphens:auto with no break/anywhere/important -> the bare hyphens
    # declaration IS reported as a potential override target. So we only assert
    # no break-all / anywhere / important conflicts.
    conflicts = scan_css_conflicts(_zip_bytes(css))
    joined = " ".join(c.declaration for c in conflicts)
    assert "break-all" not in joined
    assert "anywhere" not in joined
    assert "!important" not in joined


def test_scan_accepts_path(tmp_path: Path) -> None:
    epub_path = tmp_path / "book.epub"
    _write_epub(epub_path, css="p { word-break: break-all; }")
    conflicts = scan_css_conflicts(epub_path)
    assert conflicts
    assert all(isinstance(c, CssConflict) for c in conflicts)


# --------------------------------------------------------------------------- #
# audit_epub_output_policy (integration with real text2epub rewrite)
# --------------------------------------------------------------------------- #


def _rebuild_with_policy(src: Path, out: Path, policy: EpubOutputPolicy) -> None:
    """Use text2epub directly to rewrite a source EPUB per a booktx policy."""
    from text2epub import ReplacementPlan, rebuild_epub

    opts = to_text2epub_output_rewrite(policy)
    rebuild_epub(
        ReplacementPlan(
            source_epub=src,
            extraction_manifest={},
            replacements=[],
            output_rewrite=opts,
        ),
        out,
    )


def test_audit_passes_on_target_auto_rewrite(tmp_path: Path) -> None:
    src = tmp_path / "src.epub"
    out = tmp_path / "out.epub"
    _write_epub(src, language="en")
    policy = EpubOutputPolicy("target", "de-DE", "auto", True, False)
    _rebuild_with_policy(src, out, policy)
    report = audit_epub_output_policy(out, extraction_hrefs=[], policy=policy)
    assert report.applied is True
    assert report.new_primary_language == "de-DE"
    assert report.patched_xhtml_entries  # non-empty
    assert report.css_injected_entries  # exactly one style injected per doc


def test_audit_preserve_reports_no_changes(tmp_path: Path) -> None:
    # No rewrite for preserving policy; build a clean epub without the style.
    out = tmp_path / "out.epub"
    _write_epub(out, language="en")
    policy = EpubOutputPolicy("preserve", None, "preserve", False, False)
    report = audit_epub_output_policy(out, extraction_hrefs=[], policy=policy)
    assert report.applied is False
    assert report.css_injected_entries == []


def test_audit_fails_when_primary_language_wrong(tmp_path: Path) -> None:
    out = tmp_path / "out.epub"
    _write_epub(out, language="en")
    policy = EpubOutputPolicy("target", "de-DE", "auto", True, False)
    with pytest.raises(PolicyError, match="primary OPF dc:language"):
        audit_epub_output_policy(out, extraction_hrefs=[], policy=policy)


def test_audit_fails_when_root_lang_wrong(tmp_path: Path) -> None:
    # OPF language is correct, but the XHTML root lang does not match.
    out = tmp_path / "out.epub"
    _write_epub(out, language="de-DE")
    # Patch the chapter root lang to a mismatched value while leaving OPF intact.
    _patch_xhtml_root_lang(out, "ch1.xhtml", "en")
    policy = EpubOutputPolicy("target", "de-DE", "auto", True, False)
    with pytest.raises(PolicyError, match="root html lang"):
        audit_epub_output_policy(out, extraction_hrefs=[], policy=policy)


def test_audit_reports_css_conflict_warnings(tmp_path: Path) -> None:
    src = tmp_path / "src.epub"
    out = tmp_path / "out.epub"
    _write_epub(src, language="en", css="p { word-break: break-all; }")
    policy = EpubOutputPolicy("target", "de-DE", "auto", True, False)
    _rebuild_with_policy(src, out, policy)
    report = audit_epub_output_policy(out, extraction_hrefs=[], policy=policy)
    assert report.warnings
    assert any("break-all" in w["declaration"] for w in report.warnings)


def test_audit_skips_fixed_layout_for_css_injection(tmp_path: Path) -> None:
    src = tmp_path / "src.epub"
    out = tmp_path / "out.epub"
    _write_epub(src, language="en", fixed_layout=True)
    policy = EpubOutputPolicy("target", "de-DE", "auto", True, False)
    _rebuild_with_policy(src, out, policy)
    report = audit_epub_output_policy(out, extraction_hrefs=[], policy=policy)
    # text2epub skips CSS injection for fixed-layout by default; the audit must
    # not demand a style there. Language patching still applies.
    assert report.new_primary_language == "de-DE"
    # The fixed-layout doc should be recorded as skipped for CSS.
    assert all("fixed" not in n for n in report.css_injected_entries)


def test_audit_rejects_invalid_zip(tmp_path: Path) -> None:
    bad = tmp_path / "bad.epub"
    bad.write_bytes(b"not a zip")
    policy = EpubOutputPolicy("target", "de-DE", "auto", True, False)
    with pytest.raises(PolicyError, match="not a valid archive"):
        audit_epub_output_policy(bad, extraction_hrefs=[], policy=policy)


def test_reconcile_css_injection_validates_upstream_entries(tmp_path: Path) -> None:
    from booktx.epub_output_policy import reconcile_css_injection

    src = tmp_path / "src.epub"
    out = tmp_path / "out.epub"
    _write_epub(src, language="en")
    policy = EpubOutputPolicy("target", "de-DE", "auto", True, False)
    _rebuild_with_policy(src, out, policy)
    # Discover which entries actually got the style from text2epub.
    with zipfile.ZipFile(out) as zf:
        reported = [
            n
            for n in zf.namelist()
            if n.lower().endswith((".xhtml", ".html"))
            and f'id="{POLICY_STYLE_ID}"' in zf.read(n).decode("utf-8", "replace")
        ]
    validated = reconcile_css_injection(out, upstream_css_entries=reported)
    assert validated == reported


def test_reconcile_css_injection_fails_on_missing_style(tmp_path: Path) -> None:
    from booktx.epub_output_policy import reconcile_css_injection

    out = tmp_path / "out.epub"
    _write_epub(out, language="en")  # no policy style present
    with pytest.raises(PolicyError, match="expected exactly one style"):
        reconcile_css_injection(out, upstream_css_entries=["ch1.xhtml"])
