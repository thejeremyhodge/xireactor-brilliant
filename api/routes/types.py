"""Content type registry endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from psycopg.rows import dict_row

from auth import UserContext, get_current_user
from database import get_db

router = APIRouter(tags=["types"])


@router.get("")
async def list_types(
    user: UserContext = Depends(get_current_user),
):
    """List all content types from the registry."""
    async with get_db(user) as conn:
        cur = await conn.execute(
            """
            SELECT name, description, alias_of, is_active
            FROM content_type_registry
            ORDER BY alias_of NULLS FIRST, name
            """
        )
        cur.row_factory = dict_row
        rows = await cur.fetchall()
        return {"types": rows}


@router.post("", status_code=201)
async def create_type(
    name: str,
    description: str = "",
    alias_of: str | None = None,
    user: UserContext = Depends(get_current_user),
):
    """Register a new content type (admin only)."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can register content types")

    async with get_db(user) as conn:
        # Check if alias target exists
        if alias_of:
            cur = await conn.execute(
                "SELECT name FROM content_type_registry WHERE name = %s AND alias_of IS NULL",
                (alias_of,),
            )
            if await cur.fetchone() is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"Alias target '{alias_of}' does not exist or is itself an alias",
                )

        try:
            cur = await conn.execute(
                """
                INSERT INTO content_type_registry (name, description, alias_of)
                VALUES (%s, %s, %s)
                RETURNING name, description, alias_of, is_active
                """,
                (name, description, alias_of),
            )
            cur.row_factory = dict_row
            row = await cur.fetchone()
            return row
        except Exception as e:
            if "duplicate key" in str(e).lower():
                raise HTTPException(status_code=409, detail=f"Content type '{name}' already exists")
            raise
