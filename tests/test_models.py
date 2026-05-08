"""Pure-Python unit tests for Pydantic model defaults + enum membership.

These tests don't require a running API or DB — they exercise the models
directly. Used to lock the epistemic-axis enums (Sprint 0050, ADR #69)
to the exact value sets that ship in db/migrations/033_epistemic_axis.sql.

Run:
  pytest tests/test_models.py -v
"""

from __future__ import annotations

from api.models import (
    ClaimType,
    EntryCreate,
    EntryResponse,
    EntryUpdate,
    SourceConfidence,
    VerificationStatus,
)


# =============================================================================
# Enum value sets — must stay byte-identical to the SQL enum definitions
# in db/migrations/033_epistemic_axis.sql.
# =============================================================================


def test_claim_type_values():
    assert {m.value for m in ClaimType} == {
        "event",
        "observation",
        "claim",
        "rule",
    }


def test_source_confidence_values():
    assert {m.value for m in SourceConfidence} == {
        "verified",
        "reported",
        "inferred",
        "rumor",
    }


def test_verification_status_values():
    assert {m.value for m in VerificationStatus} == {
        "verified",
        "pending",
        "disputed",
        "superseded",
    }


# =============================================================================
# EntryCreate — epistemic fields optional; defaults None (the staging write
# path infers defaults from content_type, and the DB columns backfill the
# rest via NOT NULL DEFAULT).
# =============================================================================


def test_entry_create_epistemic_fields_optional():
    e = EntryCreate(
        title="t",
        content="c",
        content_type="note",
        logical_path="/p",
    )
    assert e.claim_type is None
    assert e.source_confidence is None


def test_entry_create_accepts_explicit_epistemic_fields():
    e = EntryCreate(
        title="t",
        content="c",
        content_type="decision",
        logical_path="/p",
        claim_type="claim",
        source_confidence="verified",
    )
    assert e.claim_type is ClaimType.CLAIM
    assert e.source_confidence is SourceConfidence.VERIFIED


# =============================================================================
# EntryUpdate — all epistemic fields optional, including the reviewer-driven
# verification_status + conflict_with overrides.
# =============================================================================


def test_entry_update_epistemic_fields_default_none():
    u = EntryUpdate()
    assert u.claim_type is None
    assert u.source_confidence is None
    assert u.verification_status is None
    assert u.conflict_with is None


def test_entry_update_accepts_verification_status_and_conflict_with():
    u = EntryUpdate(
        verification_status="disputed",
        conflict_with=["00000000-0000-0000-0000-000000000001"],
    )
    assert u.verification_status is VerificationStatus.DISPUTED
    assert u.conflict_with == ["00000000-0000-0000-0000-000000000001"]


# =============================================================================
# EntryResponse — epistemic fields default to None / [] so pre-0.9.0
# replayed payloads still validate.
# =============================================================================


def test_entry_response_epistemic_defaults():
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    r = EntryResponse(
        id="e",
        org_id="o",
        title="t",
        content="c",
        summary=None,
        content_type="note",
        logical_path="/p",
        sensitivity="shared",
        department=None,
        owner_id=None,
        tags=[],
        domain_meta={},
        version=1,
        status="published",
        source="api",
        created_by="u",
        updated_by="u",
        created_at=now,
        updated_at=now,
    )
    assert r.claim_type is None
    assert r.source_confidence is None
    assert r.verification_status is None
    assert r.conflict_with == []
