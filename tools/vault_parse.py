"""Shared vault-walking helpers used by the CLI importer and the MCP tool.

Both `tools/vault_import.py` (CLI) and `mcp/tools.py::import_vault` (MCP) reuse
`collect_md_files` + `build_payloads` + `resolve_exclude_patterns` so the
file-walking / exclude / payload-building logic lives in exactly one place.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

# Directories that are always excluded regardless of user-provided patterns.
# `.obsidian/` is the Obsidian config/plugin directory, `.trash/` is the
# soft-delete folder. Both contain metadata/garbage that should never land
# in a user's KB.
DEFAULT_EXCLUDES: tuple[str, ...] = (".obsidian/**", ".trash/**")


def resolve_exclude_patterns(user_excludes: list[str] | None) -> list[str]:
    """Merge user-provided exclude globs with the always-on defaults.

    Returns a new list; preserves user ordering. Defaults are appended only
    if not already present.
    """
    patterns: list[str] = list(user_excludes) if user_excludes else []
    for default in DEFAULT_EXCLUDES:
        if default not in patterns:
            patterns.append(default)
    return patterns


def collect_md_files(vault_path: Path, exclude_patterns: list[str]) -> list[Path]:
    """Walk vault directory and collect .md files, skipping excluded patterns.

    Returns a sorted list of paths relative to `vault_path`. Directories that
    match an exclude pattern are pruned from the walk (we never descend into
    them); individual files are filtered by the full relative path.
    """
    md_files: list[Path] = []
    for root, dirs, files in os.walk(vault_path):
        rel_root = Path(root).relative_to(vault_path)

        # Check if this directory should be excluded
        skip_dir = False
        for pattern in exclude_patterns:
            dir_str = str(rel_root)
            if fnmatch.fnmatch(dir_str, pattern.rstrip("/*").rstrip("/**")):
                skip_dir = True
                break
            if fnmatch.fnmatch(dir_str + "/", pattern):
                skip_dir = True
                break

        if skip_dir:
            dirs.clear()  # Don't descend into excluded directories
            continue

        for filename in sorted(files):
            if not filename.endswith(".md"):
                continue

            rel_path = rel_root / filename
            rel_path_str = str(rel_path)

            # Check file-level excludes
            excluded = False
            for pattern in exclude_patterns:
                if fnmatch.fnmatch(rel_path_str, pattern):
                    excluded = True
                    break
            if excluded:
                continue

            md_files.append(rel_path)

    return sorted(md_files)


def build_payloads(
    vault_path: Path, md_files: list[Path]
) -> tuple[list[dict], list[str]]:
    """Read file contents and build payload objects. Returns (payloads, errors).

    Each payload is `{"filename": "<rel path>", "content": "<utf-8 text>"}` —
    the exact shape POST /import expects. Unreadable files surface as string
    errors rather than raising.
    """
    payloads: list[dict] = []
    errors: list[str] = []

    for rel_path in md_files:
        full_path = vault_path / rel_path
        try:
            content = full_path.read_text(encoding="utf-8")
            payloads.append(
                {
                    "filename": str(rel_path),
                    "content": content,
                }
            )
        except (OSError, UnicodeDecodeError) as e:
            errors.append(f"Failed to read {rel_path}: {e}")

    return payloads, errors
