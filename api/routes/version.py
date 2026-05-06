"""``GET /version`` — public version + skill-compatibility advisory.

Sprint 0048. The skill calls this (via the MCP ``get_version`` tool) on
every session start to decide whether to proceed silently, warn the user
about an available upgrade, or refuse outright as incompatible. No auth
required — the skill needs to perform the handshake before it knows
whether its API key is valid (and the data is non-sensitive in any case).

``Cache-Control: no-cache`` so operators see a freshly-deployed version
bump immediately, without an intermediary cache hiding it.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from _version import (
    API_VERSION,
    LATEST_SKILL_VERSION,
    MIN_SKILL_VERSION,
    SKILL_DOWNLOAD_URL,
)

router = APIRouter(tags=["version"])


@router.get("/version")
async def get_version() -> JSONResponse:
    """Return the API version + skill compatibility metadata."""
    payload = {
        "api_version": API_VERSION,
        "min_skill_version": MIN_SKILL_VERSION,
        "latest_skill_version": LATEST_SKILL_VERSION,
        "skill_download_url": SKILL_DOWNLOAD_URL,
    }
    return JSONResponse(payload, headers={"Cache-Control": "no-cache"})
