#!/usr/bin/env python3
"""
Obsidian Vault Import CLI for xiReactor Brilliant.

Walks an Obsidian vault directory, collects .md files, and sends them
to the Brilliant /import API endpoint. Supports preview (dry-run) mode
to check for collisions before committing.

Dependencies: Python 3.8+ stdlib + requests
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    print("Error: 'requests' library is required. Install with: pip install requests", file=sys.stderr)
    sys.exit(1)

# Re-export the shared helpers so existing imports of
# `from vault_import import collect_md_files, build_payloads` still work.
# The logic lives in `vault_parse` so the MCP tool (`mcp/tools.py`) can use
# the same walker without shelling out to this CLI module.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from vault_parse import (  # noqa: E402
    DEFAULT_EXCLUDES,
    build_payloads,
    collect_md_files,
    resolve_exclude_patterns,
)


def preview_import(api_url: str, api_key: str, files: list[dict], base_path: str) -> None:
    """POST to /import/preview and display the collision report."""
    url = f"{api_url.rstrip('/')}/import/preview"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "files": files,
        "base_path": base_path,
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
    except requests.RequestException as e:
        print(f"Error: API request failed: {e}", file=sys.stderr)
        sys.exit(1)

    if resp.status_code != 200:
        print(f"Error: API returned {resp.status_code}", file=sys.stderr)
        try:
            detail = resp.json()
            print(json.dumps(detail, indent=2), file=sys.stderr)
        except (ValueError, requests.exceptions.JSONDecodeError):
            print(resp.text, file=sys.stderr)
        sys.exit(1)

    data = resp.json()
    print("=== Import Preview ===")
    print(f"  Files analyzed:  {data.get('files_analyzed', '?')}")
    print(f"  Would create:    {data.get('would_create', '?')}")
    print(f"  Would stage:     {data.get('would_stage', '?')}")
    print(f"  Would link:      {data.get('would_link', '?')}")

    collisions = data.get("collisions", [])
    if collisions:
        print(f"\n  Collisions ({len(collisions)}):")
        for c in collisions:
            if isinstance(c, dict):
                print(f"    - {c.get('filename', c.get('path', str(c)))}: {c.get('reason', '')}")
            else:
                print(f"    - {c}")
    else:
        print("\n  No collisions detected.")

    api_errors = data.get("errors", [])
    if api_errors:
        print(f"\n  Errors ({len(api_errors)}):")
        for e in api_errors:
            print(f"    - {e}")

    print("\nPreview complete. No changes were made.")


def execute_import(
    api_url: str,
    api_key: str,
    files: list[dict],
    base_path: str,
    source_vault: str,
) -> None:
    """POST to /import and display results."""
    url = f"{api_url.rstrip('/')}/import"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "files": files,
        "base_path": base_path,
        "source_vault": source_vault,
        "collisions": [],
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=300)
    except requests.RequestException as e:
        print(f"Error: API request failed: {e}", file=sys.stderr)
        sys.exit(1)

    if resp.status_code not in (200, 201):
        print(f"Error: API returned {resp.status_code}", file=sys.stderr)
        try:
            detail = resp.json()
            print(json.dumps(detail, indent=2), file=sys.stderr)
        except (ValueError, requests.exceptions.JSONDecodeError):
            print(resp.text, file=sys.stderr)
        sys.exit(1)

    data = resp.json()
    batch_id = data.get("batch_id", "unknown")

    print("=== Import Complete ===")
    print(f"  Batch ID:  {batch_id}")
    print(f"  Created:   {data.get('created', '?')}")
    print(f"  Staged:    {data.get('staged', '?')}")
    print(f"  Linked:    {data.get('linked', '?')}")

    api_errors = data.get("errors", [])
    if api_errors:
        print(f"\n  Errors ({len(api_errors)}):")
        for e in api_errors:
            print(f"    - {e}")

    print(f"\nImport finished. Use batch_id '{batch_id}' to rollback if needed.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import an Obsidian vault into xiReactor Brilliant",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # Preview what would happen (dry run):
  python vault_import.py --vault-path ~/vaults/my-vault --api-key sk-xxx --preview

  # Execute import:
  python vault_import.py --vault-path ~/vaults/my-vault --api-key sk-xxx

  # With custom excludes and file limit:
  python vault_import.py --vault-path ~/vaults/my-vault --api-key sk-xxx \\
      --exclude "templates/**" --exclude "archive/**" --max-files 1000
""",
    )
    parser.add_argument(
        "--vault-path",
        required=True,
        help="Path to Obsidian vault directory",
    )
    parser.add_argument(
        "--api-url",
        default="http://localhost:8010",
        help="Brilliant API base URL (default: http://localhost:8010)",
    )
    parser.add_argument(
        "--api-key",
        required=True,
        help="API key for authentication",
    )
    parser.add_argument(
        "--base-path",
        default=None,
        help="Logical path prefix (default: vault directory name)",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Dry-run mode: show collision report without importing",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=None,
        help="Glob patterns to skip (repeatable; default: .obsidian/** .trash/**)",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=500,
        help="Safety limit on number of files to import (default: 500)",
    )
    parser.add_argument(
        "--source-vault",
        default=None,
        help="Vault identifier for provenance tracking (default: vault directory name)",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    vault_path = Path(args.vault_path).resolve()
    if not vault_path.is_dir():
        print(f"Error: Vault path does not exist or is not a directory: {vault_path}", file=sys.stderr)
        sys.exit(1)

    vault_name = vault_path.name
    base_path = args.base_path or vault_name
    source_vault = args.source_vault or vault_name

    # Build exclude patterns with defaults
    exclude_patterns = resolve_exclude_patterns(args.exclude)

    print(f"Scanning vault: {vault_path}")
    print(f"Base path:      {base_path}")
    print(f"Excludes:       {', '.join(exclude_patterns)}")
    print(f"Max files:      {args.max_files}")
    print()

    # Collect files
    md_files = collect_md_files(vault_path, exclude_patterns)
    print(f"Found {len(md_files)} .md files")

    if not md_files:
        print("No files to import.")
        return

    # Check max-files safety limit
    if len(md_files) > args.max_files:
        print(
            f"Error: Found {len(md_files)} files, which exceeds --max-files limit of {args.max_files}. "
            f"Increase the limit with --max-files or add --exclude patterns to reduce the file count.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Read file contents
    payloads, read_errors = build_payloads(vault_path, md_files)

    if read_errors:
        print(f"\nWarnings ({len(read_errors)} files could not be read):")
        for err in read_errors:
            print(f"  - {err}")
        print()

    if not payloads:
        print("No readable files to import.")
        return

    print(f"Prepared {len(payloads)} files for {'preview' if args.preview else 'import'}")
    print()

    if args.preview:
        preview_import(args.api_url, args.api_key, payloads, base_path)
    else:
        execute_import(args.api_url, args.api_key, payloads, base_path, source_vault)


if __name__ == "__main__":
    main()
