"""Invite generation and redemption endpoints for org onboarding."""

import hashlib
import string
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg.rows import dict_row

from auth import UserContext, get_current_user
from database import get_db, get_pool
from models import (
    InviteCreate,
    InviteRedeem,
    InviteRedeemResponse,
    InviteResponse,
)

router = APIRouter(tags=["invitations"])

# Characters for invite code segments (uppercase alphanumeric)
_CODE_CHARS = string.ascii_uppercase + string.digits


def _generate_invite_code() -> str:
    """Generate a CTX-XXXX-XXXX invite code."""
    seg1 = "".join(secrets.choice(_CODE_CHARS) for _ in range(4))
    seg2 = "".join(secrets.choice(_CODE_CHARS) for _ in range(4))
    return f"CTX-{seg1}-{seg2}"


def _generate_token() -> str:
    """Generate a random single-use token (64 hex chars)."""
    return secrets.token_hex(32)


def _generate_user_id() -> str:
    """Generate a user ID in usr_ + 16 hex chars format."""
    return f"usr_{secrets.token_hex(8)}"


def _generate_api_key() -> str:
    """Generate an API key in bkai_ + 32 hex chars format."""
    return f"bkai_{secrets.token_hex(16)}"


def _hash_password(value: str) -> str:
    """Bcrypt hash a string value."""
    return bcrypt.hashpw(value.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _require_admin(user: UserContext) -> None:
    """Raise 403 if user is not an admin."""
    if user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Only admins can manage invitations",
        )


def _row_to_response(row: dict, token: str | None = None) -> InviteResponse:
    """Convert a database row dict to an InviteResponse."""
    return InviteResponse(
        id=str(row["id"]),
        org_id=str(row["org_id"]),
        invite_code=row["invite_code"],
        token=token,
        default_role=row["default_role"],
        email_hint=row.get("email_hint"),
        status=row["status"],
        invited_by=str(row["invited_by"]) if row.get("invited_by") else None,
        expires_at=row["expires_at"],
        created_at=row["created_at"],
    )


_SELECT_COLS = """
    id, org_id, invite_code, default_role, email_hint,
    status, invited_by, expires_at, created_at
"""


@router.post("", response_model=InviteResponse, status_code=201)
async def create_invitation(
    body: InviteCreate,
    user: UserContext = Depends(get_current_user),
):
    """Generate a new invite code (admin only). Returns code + token shown once."""
    _require_admin(user)

    invite_code = _generate_invite_code()
    raw_token = _generate_token()
    token_hash = _hash_password(raw_token)
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)

    async with get_db(user) as conn:
        cur = await conn.execute(
            f"""
            INSERT INTO invitations (
                org_id, invite_code, token_hash, default_role,
                email_hint, invited_by, expires_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s
            )
            RETURNING {_SELECT_COLS}
            """,
            (
                user.org_id,
                invite_code,
                token_hash,
                body.default_role,
                body.email_hint,
                user.id,
                expires_at,
            ),
        )
        cur.row_factory = dict_row
        row = await cur.fetchone()
        return _row_to_response(row, token=raw_token)


@router.post("/redeem", response_model=InviteRedeemResponse)
async def redeem_invitation(body: InviteRedeem):
    """Redeem an invite code (unauthenticated). Returns API key shown once.

    Single-use on attempt: any failed validation revokes the invite.
    Uses two transactions so that revoke-on-failure commits even when we
    raise an HTTP error.
    """
    pool = get_pool()
    async with pool.connection() as conn:
        # --- Phase 1: look up and validate the invite ---
        # Use autocommit for the lookup so we can commit the revoke separately
        # from the success path.
        async with conn.transaction():
            cur = await conn.execute(
                """
                SELECT id, org_id, invite_code, token_hash, default_role,
                       status, expires_at
                FROM invitations
                WHERE invite_code = %s
                FOR UPDATE
                """,
                (body.invite_code,),
            )
            row = await cur.fetchone()

            if row is None:
                raise HTTPException(status_code=404, detail="Invite not found")

            inv_id, org_id, invite_code, token_hash, default_role, status, expires_at = row

            # Validate — store failure info so we can commit revoke before raising
            fail_detail: str | None = None
            fail_code: int = 403

            if status != "pending":
                fail_detail = f"Invite is no longer valid (status: {status})"
                fail_code = 400
            elif expires_at < datetime.now(timezone.utc):
                await conn.execute(
                    "UPDATE invitations SET status = 'revoked' WHERE id = %s",
                    (inv_id,),
                )
                fail_detail = "Invite has expired"
                fail_code = 400
            elif not bcrypt.checkpw(
                body.token.encode("utf-8"), token_hash.encode("utf-8")
            ):
                await conn.execute(
                    "UPDATE invitations SET status = 'revoked' WHERE id = %s",
                    (inv_id,),
                )
                fail_detail = "Invalid invite token"
                fail_code = 403

        # Transaction committed — revoke (if any) is persisted
        if fail_detail is not None:
            raise HTTPException(status_code=fail_code, detail=fail_detail)

        # --- Phase 2: all checks passed — create user + API key + mark redeemed ---
        async with conn.transaction():
            user_id = _generate_user_id()
            email_hash = hashlib.sha256(body.email.lower().encode()).hexdigest()
            password_hash = _hash_password(body.password)
            raw_api_key = _generate_api_key()
            api_key_hash = _hash_password(raw_api_key)
            key_prefix = raw_api_key[:9]  # "bkai_XXXX"

            # Create user record with email and password_hash
            await conn.execute(
                """
                INSERT INTO users (id, org_id, email_hash, email, password_hash, display_name, role)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (user_id, str(org_id), email_hash, body.email.lower(), password_hash, body.display_name, default_role),
            )

            # Create API key record (includes org_id — bug fix)
            await conn.execute(
                """
                INSERT INTO api_keys (user_id, org_id, key_prefix, key_hash, key_type)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (user_id, str(org_id), key_prefix, api_key_hash, "interactive"),
            )

            # Mark invite as redeemed
            await conn.execute(
                """
                UPDATE invitations
                SET status = 'redeemed',
                    redeemed_at = %s,
                    redeemed_by = %s
                WHERE id = %s
                """,
                (datetime.now(timezone.utc), user_id, inv_id),
            )

            return InviteRedeemResponse(
                user_id=user_id,
                api_key=raw_api_key,
                email=body.email.lower(),
                display_name=body.display_name,
                role=default_role,
                org_id=str(org_id),
            )


@router.get("", response_model=list[InviteResponse])
async def list_invitations(
    status: str | None = Query(None, description="Filter by status"),
    user: UserContext = Depends(get_current_user),
):
    """List invitations for the user's org (admin only)."""
    _require_admin(user)

    conditions = []
    params: list = []

    if status:
        conditions.append("status = %s")
        params.append(status)

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    async with get_db(user) as conn:
        cur = await conn.execute(
            f"""
            SELECT {_SELECT_COLS}
            FROM invitations
            {where_clause}
            ORDER BY created_at DESC
            """,
            params,
        )
        cur.row_factory = dict_row
        rows = await cur.fetchall()
        return [_row_to_response(r) for r in rows]


@router.delete("/{invitation_id}")
async def revoke_invitation(
    invitation_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Revoke an invitation (admin only, soft delete)."""
    _require_admin(user)

    async with get_db(user) as conn:
        cur = await conn.execute(
            """
            UPDATE invitations
            SET status = 'revoked'
            WHERE id = %s AND status = 'pending'
            RETURNING id
            """,
            (invitation_id,),
        )
        row = await cur.fetchone()

        if row is None:
            raise HTTPException(
                status_code=404,
                detail="Invitation not found or already redeemed/revoked",
            )

        return {"message": "Invitation revoked"}
