"""Unit tests for the AI reviewer's epistemic-axis outputs (Sprint 0050, T-0294).

These exercise the verification_status / conflict_with code path of
`api/services/ai_reviewer.py` without a live LLM or DB. The Anthropic client
is patched so each test can drive the LLM "decision" deterministically; the
opportunistic conflict-detection helper is exercised directly against
in-memory context entries.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import time
import types
import uuid
from pathlib import Path

import pytest

# Make `api` importable as a top-level package (mirrors how the FastAPI app
# is launched — `python -m uvicorn api.main:app` adds api/ to sys.path).
_API_DIR = Path(__file__).resolve().parent.parent / "api"
if str(_API_DIR) not in sys.path:
    sys.path.insert(0, str(_API_DIR))

from services import ai_reviewer  # noqa: E402
from services.ai_reviewer import _detect_conflicts, review_staging_item  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _staging_item(
    *,
    title: str = "How we route incidents",
    content: str = "Incidents route through PagerDuty then Slack. Owners acknowledge within 15 minutes.",
    tags: list[str] | None = None,
) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "org_id": str(uuid.uuid4()),
        "target_path": "ops/incident-routing",
        "change_type": "create",
        "submitted_by": str(uuid.uuid4()),
        "governance_tier": 3,
        "proposed_title": title,
        "proposed_content": content,
        "proposed_meta": {
            "content_type": "note",
            "sensitivity": "shared",
            "tags": tags if tags is not None else ["incidents", "ops"],
        },
    }


def _entry(*, title: str, content: str, tags: list[str] | None = None) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "title": title,
        "content": content,
        "summary": None,
        "logical_path": "ops/incident-routing",
        "content_type": "note",
        "tags": tags or [],
    }


class _FakeMessage:
    def __init__(self, payload: dict):
        self.content = [types.SimpleNamespace(text=json.dumps(payload))]


class _FakeMessages:
    def __init__(self, payload: dict):
        self._payload = payload

    async def create(self, **kwargs):
        return _FakeMessage(self._payload)


class _FakeAnthropic:
    """Drop-in for `anthropic.AsyncAnthropic` returning a canned JSON verdict."""

    def __init__(self, payload: dict):
        self.messages = _FakeMessages(payload)


def _patch_anthropic(monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    fake_module = types.SimpleNamespace(
        AsyncAnthropic=lambda api_key=None: _FakeAnthropic(payload)
    )
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


# ---------------------------------------------------------------------------
# Tests — one per acceptance criterion
# ---------------------------------------------------------------------------


def test_auto_approve_sets_verification_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1: action='approve' with high confidence ⇒ verification_status='verified'."""
    _patch_anthropic(
        monkeypatch,
        {
            "action": "approve",
            "reasoning": "Coherent and non-conflicting",
            "confidence": 0.95,
        },
    )

    staging = _staging_item()
    # Empty context — no opportunistic conflict candidates. Approve still
    # maps to 'verified' regardless of whether conflicts were found.
    result = asyncio.run(review_staging_item(conn=None, staging_item=staging, context_entries=[]))

    assert result.action == "approve"
    assert result.verification_status == "verified"
    assert not result.conflict_with  # None or [] — both acceptable


def test_escalate_without_conflicts_sets_verification_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-0295: action='escalate' with no overlapping context ⇒
    verification_status='pending' and conflict_with empty.

    The reviewer's mapping (api/services/ai_reviewer.py:326) is:
      escalate + no conflicts ⇒ pending
    This is the "needs human, but nothing to flag yet" path — the staging
    row sits awaiting reviewer attention rather than being marked disputed.
    """
    _patch_anthropic(
        monkeypatch,
        {
            "action": "escalate",
            "reasoning": "Ambiguous — needs human judgment",
            "confidence": 0.85,
        },
    )

    staging = _staging_item(tags=["incidents", "ops"])
    # Empty context — no opportunistic conflict candidates.
    result = asyncio.run(
        review_staging_item(conn=None, staging_item=staging, context_entries=[])
    )

    assert result.action == "escalate"
    assert result.verification_status == "pending"
    assert not result.conflict_with  # None or [] — both acceptable


def test_disputed_content_sets_verification_disputed_and_conflict_with(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2: reject + overlapping context entry ⇒ disputed + conflict_with populated."""
    _patch_anthropic(
        monkeypatch,
        {
            "action": "reject",
            "reasoning": "Contradicts existing routing playbook",
            "confidence": 0.9,
        },
    )

    staging = _staging_item(tags=["incidents", "ops"])
    # Context entry shares both tags ⇒ tag_overlap > 0 ⇒ flagged as conflict.
    overlapping = _entry(
        title="Incident routing playbook",
        content="Incidents route through PagerDuty and owners acknowledge within 15 minutes.",
        tags=["incidents", "ops"],
    )
    result = asyncio.run(
        review_staging_item(conn=None, staging_item=staging, context_entries=[overlapping])
    )

    assert result.action == "reject"
    assert result.verification_status == "disputed"
    assert result.conflict_with is not None and len(result.conflict_with) >= 1
    assert overlapping["id"] in result.conflict_with


def test_review_within_latency_budget() -> None:
    """AC3: conflict detection never exceeds the documented 1s budget.

    We exercise the synchronous helper directly with a large context list to
    guarantee we don't accidentally do something quadratic + expensive.
    """
    staging = _staging_item()
    # 50 contextually-similar entries — well above the 5-cap; budget must
    # still hold.
    context = [
        _entry(
            title=f"Incident routing variant {i}",
            content="Incidents route through PagerDuty and owners acknowledge within 15 minutes.",
            tags=["incidents", "ops"],
        )
        for i in range(50)
    ]

    started = time.monotonic()
    conflicts = _detect_conflicts(staging, context)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"conflict detection took {elapsed:.3f}s, exceeds 1s budget"
    # Sanity: detection actually ran and found at least one candidate.
    assert len(conflicts) > 0


def test_conflict_with_capped_at_five_ids() -> None:
    """AC4: conflict_with is capped at 5 ids regardless of context size."""
    staging = _staging_item(tags=["incidents", "ops"])
    # 12 overlapping entries — every one is a conflict candidate.
    context = [
        _entry(
            title=f"Routing entry {i}",
            content="Routes through PagerDuty.",
            tags=["incidents", "ops"],
        )
        for i in range(12)
    ]

    conflicts = _detect_conflicts(staging, context)

    assert len(conflicts) == 5, f"expected 5, got {len(conflicts)}"
