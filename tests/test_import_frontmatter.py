"""Unit tests for the import-path frontmatter / link extraction helpers (T-0209).

Exercises the pure-python parse layer of `api/routes/import_files.py` without
requiring a live API or DB — we only test the parse/strip/governance helpers
so this file runs as part of the default `pytest tests/` suite.

Integration coverage (YAML frontmatter → entry fields after a real import,
wikilinks → `entry_links`) lives in `tests/test_entries_write.py` already and
is re-exercised by the end-to-end validation command in T-0209's spec.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make `api/` importable — same pattern as tests/test_storage.py.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_API_DIR = _REPO_ROOT / "api"
if str(_API_DIR) not in sys.path:
    sys.path.insert(0, str(_API_DIR))

from services.frontmatter import (  # noqa: E402
    build_domain_meta,
    extract_governance_fields,
    extract_title,
    parse_frontmatter,
)


# ---------------------------------------------------------------------------
# parse_frontmatter — YAML preamble extraction
# ---------------------------------------------------------------------------


def test_parse_frontmatter_strips_preamble_from_remaining_content():
    """The `---\\n...\\n---\\n` preamble must not survive into persisted content."""
    src = (
        "---\n"
        "title: Sprint Kickoff\n"
        "tags:\n"
        "  - sprint\n"
        "  - kickoff\n"
        "---\n"
        "# Sprint Kickoff\n"
        "\n"
        "The body of the note.\n"
    )
    meta, remaining = parse_frontmatter(src)
    assert meta["title"] == "Sprint Kickoff"
    assert meta["tags"] == ["sprint", "kickoff"]
    assert "---" not in remaining.split("\n")[0]
    assert remaining.startswith("# Sprint Kickoff")
    assert "tags:" not in remaining


def test_parse_frontmatter_inline_list_and_scalars():
    src = (
        "---\n"
        "tags: [alpha, beta, gamma]\n"
        "sensitivity: private\n"
        "content_type: meeting\n"
        "---\n"
        "Body\n"
    )
    meta, remaining = parse_frontmatter(src)
    assert meta["tags"] == ["alpha", "beta", "gamma"]
    assert meta["sensitivity"] == "private"
    assert meta["content_type"] == "meeting"
    assert remaining == "Body\n"


def test_parse_frontmatter_no_preamble_returns_empty_meta():
    src = "# Just a heading\n\nNo frontmatter here.\n"
    meta, remaining = parse_frontmatter(src)
    assert meta == {}
    assert remaining == src


def test_parse_frontmatter_malformed_falls_back_to_legacy_parser():
    """Malformed YAML should not raise — the legacy parser recovers partial data."""
    src = (
        "---\n"
        "tags: [unclosed, list\n"
        "title: Recoverable\n"
        "---\n"
        "Body\n"
    )
    meta, remaining = parse_frontmatter(src)
    assert "title" in meta  # at least one field must survive
    assert remaining == "Body\n"


# ---------------------------------------------------------------------------
# extract_title — frontmatter > heading > filename
# ---------------------------------------------------------------------------


def test_extract_title_prefers_frontmatter_over_heading():
    title = extract_title(
        "# Not The Title\n\nBody\n",
        "note.md",
        meta={"title": "Canonical Title"},
    )
    assert title == "Canonical Title"


def test_extract_title_falls_back_to_heading_then_filename():
    assert extract_title("# Heading Title\n", "note.md", meta={}) == "Heading Title"
    assert extract_title("no heading here", "my-note.md", meta=None) == "my-note"


# ---------------------------------------------------------------------------
# extract_governance_fields — known keys -> validated entry columns
# ---------------------------------------------------------------------------


def test_extract_governance_fields_known_keys_validated():
    meta = {
        "title": "x",  # ignored (not governance)
        "sensitivity": "private",
        "content_type": "meeting",
        "department": "engineering",
        "summary": "A one-line abstract.",
    }
    gov = extract_governance_fields(meta)
    assert gov["sensitivity"] == "private"
    assert gov["content_type"] == "meeting"
    assert gov["department"] == "engineering"
    assert gov["summary"] == "A one-line abstract."


def test_extract_governance_fields_rejects_unknown_sensitivity():
    gov = extract_governance_fields({"sensitivity": "top-secret"})
    assert "sensitivity" not in gov  # not in VALID_SENSITIVITIES


def test_extract_governance_fields_accepts_unknown_content_type():
    """`content_type_registry` is the sole authority (spec 0046 / T-0272.2).

    Unknown values are no longer dropped at parse time — they pass through
    so `_resolve_content_type` can auto-register them as `is_active=false`
    in the registry table.
    """
    gov = extract_governance_fields({"content_type": "invented"})
    assert gov["content_type"] == "invented"


def test_extract_governance_fields_accepts_moc():
    """`type: moc` is the motivating case for spec 0046 — a MOC (Map of
    Content) hub note. It must round-trip unchanged through
    `extract_governance_fields` so the subsequent registry lookup can
    auto-register it.
    """
    gov = extract_governance_fields({"content_type": "moc"})
    assert gov["content_type"] == "moc"


def test_extract_governance_fields_content_type_alias_accepted():
    """Legacy `type:` alias should still resolve a valid content_type."""
    gov = extract_governance_fields({"type": "meeting"})
    assert gov["content_type"] == "meeting"


# ---------------------------------------------------------------------------
# build_domain_meta — unknown keys survive, known keys are stripped
# ---------------------------------------------------------------------------


def test_build_domain_meta_strips_known_keys_keeps_unknown():
    meta = {
        "title": "x",
        "tags": ["a"],
        "sensitivity": "private",
        "content_type": "meeting",
        "type": "meeting",  # alias, also stripped
        "department": "eng",
        "summary": "abstract",
        # Unknown / org-specific keys — must survive into domain_meta.
        "client_id": "acme-123",
        "project_code": "ALPHA-42",
        "custom_flag": True,
    }
    dm = build_domain_meta(meta)
    assert dm == {
        "client_id": "acme-123",
        "project_code": "ALPHA-42",
        "custom_flag": True,
    }


# ---------------------------------------------------------------------------
# Wikilink regex — used by sync_entry_links (write-path link sync, #24).
# Re-implemented here because `api/routes/import_files.py` transitively pulls
# in the FastAPI/DB stack; the regex itself is trivially recreated.
# ---------------------------------------------------------------------------

import re  # noqa: E402

_WIKI_LINK_RE = re.compile(r"\[\[([^\]|#\\]+)")


@pytest.mark.parametrize(
    "content,expected",
    [
        ("See [[Alpha]] for details.", ["Alpha"]),
        ("Both [[Alpha]] and [[Beta]].", ["Alpha", "Beta"]),
        ("Pipe display: [[Alpha|the alpha project]].", ["Alpha"]),
        ("Heading anchor: [[Alpha#Section]].", ["Alpha"]),
        ("No wikilinks here.", []),
        ("Table: [[ent-0567\\|display]]", ["ent-0567"]),
        ("Mixed: [[Alpha]] and [[ent-0567\\|x]]", ["Alpha", "ent-0567"]),
        ("Trailing backslash no pipe: [[ent-0567\\]]", ["ent-0567"]),
    ],
)
def test_wiki_link_regex(content, expected):
    assert _WIKI_LINK_RE.findall(content) == expected
