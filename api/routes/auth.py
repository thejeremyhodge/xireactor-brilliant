"""Authentication routes: email+password login."""

import secrets

import bcrypt
from fastapi import APIRouter, HTTPException
from psycopg.rows import dict_row

from database import get_pool
from models import LoginRequest, LoginResponse, UserResponse

router = APIRouter(tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest):
    """Authenticate with email + password, returns an active API key + user info.

    Uses raw pool (not RLS-scoped) since this is an unauthenticated endpoint.
    Email lookup is case-insensitive (stored lowercase).
    """
    email = body.email.strip().lower()

    pool = get_pool()
    async with pool.connection() as conn:
        # Look up user by email
        cur = await conn.execute(
            """
            SELECT id, org_id, display_name, email, role, department,
                   is_active, password_hash
            FROM users
            WHERE email = %s
            """,
            (email,),
        )
        cur.row_factory = dict_row
        user = await cur.fetchone()

        if user is None:
            raise HTTPException(status_code=401, detail="Invalid email or password")

        if not user["is_active"]:
            raise HTTPException(status_code=401, detail="Account is deactivated")

        if not user["password_hash"]:
            raise HTTPException(status_code=401, detail="Invalid email or password")

        # Verify password
        if not bcrypt.checkpw(
            body.password.encode("utf-8"),
            user["password_hash"].encode("utf-8"),
        ):
            raise HTTPException(status_code=401, detail="Invalid email or password")

        # Generate a fresh session API key so the frontend has a usable
        # Bearer token.  We mint a new interactive key on every login;
        # production would use JWTs, but this unblocks the frontend now.
        suffix = secrets.token_hex(12)
        key_prefix = f"bkai_{suffix[:4]}"
        full_key = f"{key_prefix}_{suffix[4:]}"
        key_hash = bcrypt.hashpw(
            full_key.encode("utf-8"), bcrypt.gensalt()
        ).decode("utf-8")

        await conn.execute(
            """
            INSERT INTO api_keys (user_id, org_id, key_hash, key_prefix, key_type, label)
            VALUES (%s, %s, %s, %s, 'interactive', 'Login session key')
            """,
            (user["id"], user["org_id"], key_hash, key_prefix),
        )

        return LoginResponse(
            api_key=full_key,
            user=UserResponse(
                id=user["id"],
                org_id=user["org_id"],
                display_name=user["display_name"],
                email=user["email"],
                role=user["role"],
                department=user["department"],
                is_active=user["is_active"],
            ),
        )
