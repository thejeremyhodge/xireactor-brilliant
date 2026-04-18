"""Unit tests for the shared vault-walking helpers (T-0209).

`tools/vault_parse.py` is used by both the CLI importer and the
`import_vault(path)` MCP tool — these tests pin the exclude/walk/read
behaviour without spinning up the API or DB.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add tools/ to sys.path so `vault_parse` resolves from the repo-root layout.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_TOOLS_DIR = _REPO_ROOT / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from vault_parse import (  # noqa: E402
    DEFAULT_EXCLUDES,
    build_payloads,
    collect_md_files,
    resolve_exclude_patterns,
)


FIXTURE_VAULT = Path(__file__).resolve().parent / "fixtures" / "vault"


def test_default_excludes_contain_obsidian_and_trash():
    assert ".obsidian/**" in DEFAULT_EXCLUDES
    assert ".trash/**" in DEFAULT_EXCLUDES


def test_resolve_exclude_patterns_always_appends_defaults():
    assert resolve_exclude_patterns(None) == list(DEFAULT_EXCLUDES)
    merged = resolve_exclude_patterns(["templates/**"])
    assert merged[0] == "templates/**"
    for default in DEFAULT_EXCLUDES:
        assert default in merged


def test_resolve_exclude_patterns_does_not_duplicate_defaults():
    merged = resolve_exclude_patterns([".obsidian/**"])
    # Only one copy of the default even though the user passed it explicitly.
    assert merged.count(".obsidian/**") == 1


def test_collect_md_files_skips_obsidian_dir_by_default():
    files = collect_md_files(FIXTURE_VAULT, resolve_exclude_patterns(None))
    # The sample vault has README.md, projects/alpha.md, people/gareth.md,
    # and .obsidian/workspace.json. The .obsidian/ dir must be pruned.
    rel_paths = {str(p) for p in files}
    assert "README.md" in rel_paths
    assert "projects/alpha.md" in rel_paths
    assert "people/gareth.md" in rel_paths
    # workspace.json isn't a .md file, but double-check no .obsidian/ files leak
    for p in files:
        assert ".obsidian" not in p.parts


def test_collect_md_files_respects_custom_excludes():
    files = collect_md_files(
        FIXTURE_VAULT, resolve_exclude_patterns(["projects/**"])
    )
    rel_paths = {str(p) for p in files}
    assert "projects/alpha.md" not in rel_paths
    assert "people/gareth.md" in rel_paths  # sibling dir still included


def test_build_payloads_reads_contents_and_uses_rel_paths():
    files = collect_md_files(FIXTURE_VAULT, resolve_exclude_patterns(None))
    payloads, errors = build_payloads(FIXTURE_VAULT, files)
    assert errors == []
    filenames = {p["filename"] for p in payloads}
    assert "projects/alpha.md" in filenames

    alpha = next(p for p in payloads if p["filename"] == "projects/alpha.md")
    assert alpha["content"].startswith("---\n")
    assert "Alpha Kickoff" in alpha["content"]
