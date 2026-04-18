"""Pure-python helpers for parsing markdown frontmatter + extracting entry
fields during import (T-0209).

Lives under `api/services/` (no DB deps) so unit tests can import these
directly without booting the full api package. `api/routes/import_files.py`
re-exports the public names for backwards compatibility.

Responsibilities:
  - Strip a `---\\n...\\n---\\n` YAML frontmatter preamble from markdown
    content. Uses PyYAML when available, falls back to a forgiving
    hand-rolled key:value parser otherwise so malformed YAML doesn't kill
    an import.
  - Split parsed frontmatter into `governance` (first-class entry columns —
    sensitivity, content_type, department, summary) and `domain_meta`
    (everything else, stored as JSON).
  - Resolve the entry `title` from frontmatter / heading / filename.
"""

from __future__ import annotations

try:
    import yaml as _yaml  # type: ignore
    _HAS_YAML = True
except ImportError:  # pragma: no cover - PyYAML is in api/requirements.txt
    _HAS_YAML = False


# Valid governance values — duplicated here (in a pure-python module with no
# pydantic/FastAPI deps) so this file can be imported and unit-tested without
# booting the api package. Keep in sync with `api/models.py`.
VALID_SENSITIVITIES: frozenset = frozenset(
    {
        "system",
        "strategic",
        "operational",
        "private",
        "project",
        "meeting",
        "shared",
    }
)
VALID_CONTENT_TYPES: frozenset = frozenset(
    {
        "context",
        "project",
        "meeting",
        "decision",
        "intelligence",
        "daily",
        "resource",
        "department",
        "team",
        "system",
        "onboarding",
    }
)


# Frontmatter keys that map to first-class `entries` columns. Everything else
# is considered org-specific metadata and routes to `domain_meta`.
# `type` is the legacy alias for `content_type` (still consumed by
# `_resolve_content_type` in routes/import_files.py).
_ENTRY_FIELD_KEYS: frozenset[str] = frozenset(
    {"title", "tags", "sensitivity", "content_type", "type", "department", "summary"}
)


def _legacy_parse_frontmatter_body(body: str) -> dict:
    """Fallback hand-rolled YAML parser — forgiving enough that malformed
    frontmatter still yields recoverable key/value pairs.
    """
    meta: dict = {}
    lines = body.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            # Check for inline list: [a, b, c]
            if val.startswith("[") and val.endswith("]"):
                inner = val[1:-1]
                meta[key] = [
                    item.strip().strip('"').strip("'")
                    for item in inner.split(",")
                    if item.strip()
                ]
            elif val == "":
                # Possibly a multi-line list; collect subsequent "  - item" lines
                items = []
                while i + 1 < len(lines):
                    next_line = lines[i + 1]
                    stripped = next_line.strip()
                    if stripped.startswith("- "):
                        items.append(stripped[2:].strip().strip('"').strip("'"))
                        i += 1
                    else:
                        break
                if items:
                    meta[key] = items
                else:
                    meta[key] = val
            else:
                meta[key] = val
        i += 1
    return meta


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Extract YAML frontmatter and return (meta, remaining_content).

    Uses PyYAML (`yaml.safe_load`) when available so nested mappings, quoted
    strings, booleans, and numeric types round-trip correctly. Falls back to
    the legacy hand-rolled parser if PyYAML is unavailable or the body does
    not produce a dict (malformed YAML, single scalar).

    `remaining_content` has the `---\\n...\\n---\\n` preamble stripped so
    the persisted entry content no longer includes the frontmatter block
    (mirrors migration 020's cleanup of legacy seeds).
    """
    if not content.startswith("---\n"):
        return {}, content
    end = content.find("\n---", 3)
    if end == -1:
        return {}, content

    body = content[4:end]
    remaining = content[end + 4:].lstrip("\n")

    meta: dict = {}
    if _HAS_YAML:
        try:
            parsed = _yaml.safe_load(body)
            if isinstance(parsed, dict):
                meta = parsed
            else:
                # PyYAML returned a non-dict (scalar, list, None) — fall back
                # to the legacy parser which always yields a dict.
                meta = _legacy_parse_frontmatter_body(body)
        except Exception:
            # Malformed YAML — fall back to the forgiving hand-rolled parser
            # so partial metadata is still recoverable.
            meta = _legacy_parse_frontmatter_body(body)
    else:
        meta = _legacy_parse_frontmatter_body(body)

    return meta, remaining


def extract_title(content: str, filename: str, meta: dict | None = None) -> str:
    """Resolve the entry title from frontmatter, first # heading, or filename.

    Precedence: `meta['title']` > first `# ` heading > filename without `.md`.
    """
    if meta:
        frontmatter_title = meta.get("title")
        if isinstance(frontmatter_title, str) and frontmatter_title.strip():
            return frontmatter_title.strip()
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            return stripped[2:].strip()
    if filename.lower().endswith(".md"):
        return filename[:-3]
    return filename


def extract_governance_fields(meta: dict) -> dict:
    """Pull known governance / structural fields off frontmatter.

    Returns a dict with any of `sensitivity`, `content_type`, `department`,
    `summary` (each only present when the frontmatter supplied a usable
    value). Values are validated against `VALID_SENSITIVITIES` /
    `VALID_CONTENT_TYPES`; invalid values are dropped so the caller falls
    back to the existing inference path.
    """
    out: dict = {}

    sens = meta.get("sensitivity")
    if isinstance(sens, str) and sens.strip() and sens.strip() in VALID_SENSITIVITIES:
        out["sensitivity"] = sens.strip()

    # `content_type` wins over the legacy `type` alias.
    ct_raw = meta.get("content_type")
    if ct_raw is None:
        ct_raw = meta.get("type")
    if isinstance(ct_raw, list):
        ct_raw = ct_raw[0] if ct_raw else None
    if isinstance(ct_raw, str) and ct_raw.strip() in VALID_CONTENT_TYPES:
        out["content_type"] = ct_raw.strip()

    dept = meta.get("department")
    if isinstance(dept, str) and dept.strip():
        out["department"] = dept.strip()

    summary = meta.get("summary")
    if isinstance(summary, str) and summary.strip():
        out["summary"] = summary.strip()

    return out


def build_domain_meta(meta: dict) -> dict:
    """Strip frontmatter keys that have first-class entry columns.

    Everything not consumed by `title`, `tags`, `sensitivity`, `content_type`
    (or its `type` alias), `department`, or `summary` becomes org-specific
    metadata stored as JSON on the entry (#25).
    """
    return {k: v for k, v in meta.items() if k not in _ENTRY_FIELD_KEYS}
