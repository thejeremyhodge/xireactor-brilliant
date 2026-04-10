"""AI reviewer for Tier 3 governance staging items.

Uses the Anthropic API to evaluate staging items against project context
and governance rules. Returns approve / reject / escalate + reasoning.
"""

import json
import logging
import os
from typing import Any

from models import AIReviewResult

logger = logging.getLogger(__name__)

# Model choice: claude-sonnet-4-6 is fast + cheap for high-volume review
_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 1024

# Four-tier governance rules — included verbatim in the prompt so the model
# grounds its answer in the project's own governance model.
_GOVERNANCE_RULES = """\
Four-tier governance model:
  Tier 1 - Auto-approve: creates (non-sensitive), appends, links,
           admin/editor web_ui writes. No checks needed.
  Tier 2 - Auto-approve with conflict detection: updates and modifications
           on non-sensitive content. Inline checks; clean = auto-approve,
           conflicts escalate to Tier 3.
  Tier 3 - Batch/AI review: high-sensitivity content (system, strategic)
           and Tier 2 escalations. YOU are reviewing items at this tier.
  Tier 4 - Human-only: deletions, sensitivity changes, governance mods.
"""

_SYSTEM_PROMPT = """\
You are a governance reviewer for a knowledge base system. Your job is to \
evaluate proposed changes and decide whether they should be approved, rejected, \
or escalated to a human reviewer.

{governance_rules}

You are reviewing a Tier 3 item. This means the content is either \
high-sensitivity (system or strategic) or was escalated from Tier 2 due to \
conflicts.

Evaluate the proposed change against:
1. Whether the content is coherent, well-formed, and appropriate for its target path
2. Whether it conflicts with or contradicts existing related entries
3. Whether a high-sensitivity write is justified by the content
4. Whether the change type (create/update/append) makes sense for the target

Respond with ONLY a JSON object (no markdown, no code fences):
{{"action": "approve" | "reject" | "escalate", "reasoning": "<1-3 sentences>", "confidence": <0.0-1.0>}}

Rules:
- "approve" = safe to promote, no issues found
- "reject" = clearly problematic (spam, contradicts existing data, malformed)
- "escalate" = ambiguous or you are not confident; let a human decide
- When in doubt, ALWAYS escalate. Never auto-approve on ambiguity.
- confidence < 0.7 should always escalate regardless of action
""".format(governance_rules=_GOVERNANCE_RULES)


def _build_user_prompt(staging_item: dict, context_entries: list[dict]) -> str:
    """Construct the user prompt with the staging item and related context."""
    parts = []

    parts.append("## Proposed Change")
    parts.append(f"- **Target path:** {staging_item.get('target_path', 'unknown')}")
    parts.append(f"- **Change type:** {staging_item.get('change_type', 'unknown')}")
    parts.append(f"- **Submitted by:** {staging_item.get('submitted_by', 'unknown')}")
    parts.append(f"- **Governance tier:** {staging_item.get('governance_tier', 'unknown')}")

    meta = staging_item.get("proposed_meta") or {}
    if meta:
        parts.append(f"- **Content type:** {meta.get('content_type', 'unspecified')}")
        parts.append(f"- **Sensitivity:** {meta.get('sensitivity', 'unspecified')}")

    title = staging_item.get("proposed_title")
    if title:
        parts.append(f"- **Proposed title:** {title}")

    content = staging_item.get("proposed_content")
    if content:
        # Truncate very long content to stay within token budget
        truncated = content[:4000]
        if len(content) > 4000:
            truncated += "\n... [truncated]"
        parts.append(f"\n### Proposed Content\n{truncated}")

    # Existing evaluator notes (e.g., from Tier 2 escalation)
    notes = staging_item.get("evaluator_notes")
    if notes:
        parts.append(f"\n### Existing Evaluator Notes\n{notes}")

    if context_entries:
        parts.append("\n## Related Existing Entries (for conflict/coherence check)")
        for entry in context_entries[:5]:  # cap at 5
            parts.append(f"\n### {entry.get('title', 'Untitled')} ({entry.get('logical_path', '')})")
            summary = entry.get("summary") or (entry.get("content", "")[:500])
            parts.append(summary)

    return "\n".join(parts)


async def _fetch_related_entries(conn, staging_item: dict) -> list[dict]:
    """Fetch 3-5 related entries by logical_path prefix or tag overlap."""
    from psycopg.rows import dict_row

    related: list[dict] = []
    target_path = staging_item.get("target_path", "")
    org_id = staging_item.get("org_id")

    # Strategy 1: entries sharing a path prefix
    if target_path and "/" in target_path:
        prefix = target_path.rsplit("/", 1)[0] + "/"
        cur = await conn.execute(
            """
            SELECT id, title, content, summary, logical_path, content_type, tags
            FROM entries
            WHERE org_id = %s AND logical_path LIKE %s
            ORDER BY updated_at DESC
            LIMIT 3
            """,
            (org_id, f"{prefix}%"),
        )
        cur.row_factory = dict_row
        related.extend(await cur.fetchall())

    # Strategy 2: entries with overlapping tags
    meta = staging_item.get("proposed_meta") or {}
    tags = meta.get("tags", [])
    if tags and len(related) < 5:
        cur = await conn.execute(
            """
            SELECT id, title, content, summary, logical_path, content_type, tags
            FROM entries
            WHERE org_id = %s AND tags && %s
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (org_id, tags, 5 - len(related)),
        )
        cur.row_factory = dict_row
        tag_entries = await cur.fetchall()
        seen_ids = {str(r["id"]) for r in related}
        for entry in tag_entries:
            if str(entry["id"]) not in seen_ids:
                related.append(entry)

    return related[:5]


async def review_staging_item(
    conn: Any,
    staging_item: dict,
    context_entries: list[dict] | None = None,
) -> AIReviewResult:
    """Evaluate a Tier 3 staging item using the Anthropic API.

    Args:
        conn: Database connection (used to fetch related entries if context_entries is None)
        staging_item: The staging row as a dict
        context_entries: Optional pre-fetched related entries; fetched automatically if None

    Returns:
        AIReviewResult with action, reasoning, and confidence.
        On any error or missing API key, returns escalate (fail safe).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.info("ANTHROPIC_API_KEY not set — AI reviewer disabled, escalating")
        return AIReviewResult(
            action="escalate",
            reasoning="AI reviewer disabled (ANTHROPIC_API_KEY not set)",
            confidence=0.0,
        )

    # Fetch related entries if not provided
    if context_entries is None:
        try:
            context_entries = await _fetch_related_entries(conn, staging_item)
        except Exception as exc:
            logger.warning("Failed to fetch related entries for AI review: %s", exc)
            context_entries = []

    user_prompt = _build_user_prompt(staging_item, context_entries)

    try:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=api_key)
        response = await client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )

        # Extract text from response
        text = response.content[0].text.strip()

        # Parse JSON response
        result = json.loads(text)
        action = result.get("action", "escalate")
        reasoning = result.get("reasoning", "No reasoning provided")
        confidence = float(result.get("confidence", 0.0))

        # Validate action
        if action not in ("approve", "reject", "escalate"):
            logger.warning("AI reviewer returned invalid action '%s', escalating", action)
            return AIReviewResult(
                action="escalate",
                reasoning=f"AI returned invalid action '{action}': {reasoning}",
                confidence=confidence,
            )

        # Low confidence = always escalate
        if confidence < 0.7 and action != "escalate":
            logger.info(
                "AI reviewer confidence %.2f < 0.7 for action '%s', overriding to escalate",
                confidence,
                action,
            )
            return AIReviewResult(
                action="escalate",
                reasoning=f"Low confidence ({confidence:.2f}): {reasoning}",
                confidence=confidence,
            )

        return AIReviewResult(action=action, reasoning=reasoning, confidence=confidence)

    except json.JSONDecodeError as exc:
        logger.warning("AI reviewer returned non-JSON response: %s", exc)
        return AIReviewResult(
            action="escalate",
            reasoning=f"AI review parse error: {exc}",
            confidence=0.0,
        )
    except Exception as exc:
        logger.error("AI reviewer call failed: %s", exc)
        return AIReviewResult(
            action="escalate",
            reasoning=f"AI review error: {exc}",
            confidence=0.0,
        )
