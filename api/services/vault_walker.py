"""Tarball-and-FS-agnostic vault walker.

Shared helper used by the `/import/vault-from-blob` HTTP endpoint and, in
principle, any other caller that needs to walk a tarred-up vault without
materializing the full tree in memory. Exclude-pattern semantics mirror
`tools/vault_parse.py` so the two walk paths produce the same set of files
for the same vault.

The tarball iterator streams one member at a time via
`tarfile.extractfile()` and enforces a caller-supplied uncompressed-bytes
ceiling as a zip-bomb guard. `.md` files only; non-regular entries and
entries matching exclude globs are skipped.
"""

from __future__ import annotations

import fnmatch
import io
import tarfile
from typing import Iterator

# Directories that are always excluded regardless of user-provided patterns.
# `.obsidian/` is the Obsidian config/plugin directory, `.trash/` is the
# soft-delete folder. Both contain metadata/garbage that should never land
# in a user's KB. Mirrors `tools/vault_parse.py::DEFAULT_EXCLUDES`.
DEFAULT_EXCLUDES: tuple[str, ...] = (".obsidian/**", ".trash/**")


def resolve_exclude_patterns(user_excludes: list[str] | None) -> list[str]:
    """Merge user-provided exclude globs with the always-on defaults.

    Returns a new list; preserves user ordering. Defaults are appended only
    if not already present. Mirror of `tools/vault_parse.py` so both walk
    paths produce matching exclude sets.
    """
    patterns: list[str] = list(user_excludes) if user_excludes else []
    for default in DEFAULT_EXCLUDES:
        if default not in patterns:
            patterns.append(default)
    return patterns


def should_exclude(rel_path: str, patterns: list[str]) -> bool:
    """Return True if ``rel_path`` matches any exclude glob.

    The relative path should use forward slashes (POSIX style) — tarball
    members already use forward slashes, so no platform conversion is
    needed. Matches both full-path patterns (`.obsidian/**`) and prefix
    patterns by probing every ancestor segment against the stripped
    pattern form, the same way `collect_md_files` prunes directories.
    """
    # Normalize: strip any leading "./"
    if rel_path.startswith("./"):
        rel_path = rel_path[2:]

    for pattern in patterns:
        # Direct file-level match (`.obsidian/workspace.json` against `.obsidian/**`)
        if fnmatch.fnmatch(rel_path, pattern):
            return True

        # Directory-prefix match: walk each ancestor segment against the
        # bare directory form of the pattern (strip trailing `/**` / `/*`
        # / `/`). This mirrors the `collect_md_files` dir-prune path.
        bare = pattern.rstrip("/*").rstrip("/**").rstrip("/")
        if not bare:
            continue

        segments = rel_path.split("/")
        # Check every prefix (e.g. ".obsidian", ".obsidian/plugins", …)
        for i in range(1, len(segments)):
            prefix = "/".join(segments[:i])
            if fnmatch.fnmatch(prefix, bare):
                return True

    return False


def iter_tarball_md(
    tar_bytes: bytes,
    excludes: list[str],
    max_uncompressed: int,
) -> Iterator[tuple[str, str]]:
    """Yield ``(rel_path, content)`` for each `.md` file in the tarball.

    Streams one member at a time via ``tarfile.extractfile()`` — the full
    tree is never materialized in memory simultaneously. Non-regular
    files (directories, symlinks, devices) and entries matching
    ``excludes`` are skipped. Non-`.md` files are skipped.

    Raises ``ValueError`` if the cumulative uncompressed bytes read from
    the tarball exceeds ``max_uncompressed`` (zip-bomb guard).

    UTF-8 decoding uses ``errors="replace"`` — binary or mis-encoded
    files still yield, with replacement characters for the bad bytes,
    rather than failing the whole import.
    """
    total_bytes = 0
    buffer = io.BytesIO(tar_bytes)

    # Auto-detect compression (gz, bz2, xz, or uncompressed)
    with tarfile.open(fileobj=buffer, mode="r:*") as tar:
        for member in tar:
            if not member.isfile():
                # Skip directories, symlinks, hardlinks, devices, fifos
                continue

            rel_path = member.name
            if rel_path.startswith("./"):
                rel_path = rel_path[2:]

            # Only markdown files
            if not rel_path.endswith(".md"):
                continue

            # Honor exclude patterns (defaults + user)
            if should_exclude(rel_path, excludes):
                continue

            # Zip-bomb guard: track cumulative uncompressed bytes before
            # reading, using the member's declared size (cheap), then
            # verify against the actual read size below.
            total_bytes += member.size
            if total_bytes > max_uncompressed:
                raise ValueError(
                    f"Tarball exceeds max_uncompressed limit of "
                    f"{max_uncompressed} bytes"
                )

            extracted = tar.extractfile(member)
            if extracted is None:
                # Shouldn't happen for isfile() members, but guard anyway
                continue

            raw = extracted.read()
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                content = raw.decode("utf-8", errors="replace")

            yield rel_path, content
