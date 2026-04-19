"""Brilliant API HTTP client with auth and error handling.

Sprint 0039 (T-0229): the MCP no longer holds an admin-scoped API key.
Instead it authenticates with a *service-role* key (``BRILLIANT_SERVICE_API_KEY``)
and passes ``X-Act-As-User: <user_id>`` on every call. The API's auth
middleware honors that header only when the presenting key has
``key_type = 'service'``; any other key with ``X-Act-As-User`` is 403'd.

The per-request ``act_as`` kwarg is the knob: tool handlers pull the
OAuth-bound ``user_id`` off the authenticated ``AccessToken`` and thread
it through every ``api.get/post/...`` call. Service-level calls (no
``act_as``) are technically still possible here but the tool layer never
makes them — a missing ``user_id`` on a remote-transport request 401s at
the tool handler before we ever get here.
"""

import os

import httpx
import psycopg


def _resolve_api_base_url() -> str:
    """Resolve the API's outbound-callable public base URL.

    Resolution order — first non-empty wins:

    1. ``brilliant_settings.api_public_url`` — populated at API boot from
       its own ``$RENDER_EXTERNAL_URL`` (migration 032). This is the
       authoritative source on Render, where ``fromService.property:host``
       only yields the internal service name (``brilliant-api``) that is
       NOT publicly routable.
    2. ``BRILLIANT_API_PUBLIC_URL`` — explicit operator override.
    3. ``BRILLIANT_BASE_URL`` — legacy env var. On Render this is the bare
       internal hostname; we prepend ``https://`` if no scheme is present.
       Kept for local dev (``http://localhost:8010``) and custom deploys.
    4. ``http://localhost:8010`` — local-dev last-resort default.

    DB read is a one-time cost at client construction (module import in
    ``remote_server.py`` / ``server.py``). A missing ``DATABASE_URL``, an
    unreachable DB, or a pre-032 schema all fall through silently to the
    env-var tier — tests and stdio-only deployments are unaffected.
    """
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if dsn:
        try:
            with psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT api_public_url FROM brilliant_settings WHERE id = 1"
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        return str(row[0]).rstrip("/")
        except Exception:  # noqa: BLE001 — pre-032 schema or DB down → env fallback
            pass

    raw = os.environ.get("BRILLIANT_API_PUBLIC_URL", "").strip()
    if raw:
        if not raw.startswith(("http://", "https://")):
            raw = f"https://{raw}"
        return raw.rstrip("/")

    raw = os.environ.get("BRILLIANT_BASE_URL", "http://localhost:8010").strip()
    if raw and not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    return raw.rstrip("/")


class BrilliantClient:
    """Async HTTP client for the Brilliant REST API."""

    def __init__(self):
        self.base_url = _resolve_api_base_url()
        # Service-role key. The MCP must present a service-typed key for
        # ``X-Act-As-User`` to be honored upstream. Empty string is a
        # legitimate local-dev value (stdio transport against a dev API
        # that doesn't check auth) — we don't fail-loud here so the stdio
        # server continues to work. The remote server relies on tool
        # handlers raising 401 when no user_id is bound, so a missing
        # service key would surface as a 401 from the API itself.
        self.api_key = os.environ.get("BRILLIANT_SERVICE_API_KEY", "")

    def _headers(
        self,
        api_key: str | None = None,
        act_as_user_id: str | None = None,
    ) -> dict[str, str]:
        """Build request headers.

        ``api_key`` overrides the default service key for a single call
        (used by e.g. ``redeem_invite`` which must not present any auth).
        ``act_as_user_id``, when set, adds the ``X-Act-As-User`` header so
        the API acts-as the target user while still authenticating with
        the MCP's service key.
        """
        key = api_key if api_key is not None else self.api_key
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        if act_as_user_id:
            headers["X-Act-As-User"] = act_as_user_id
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        api_key: str | None = None,
        act_as: str | None = None,
        **kwargs,
    ) -> dict:
        """Make an HTTP request and return parsed JSON or error dict."""
        url = f"{self.base_url}{path}"
        headers = self._headers(api_key=api_key, act_as_user_id=act_as)
        async with httpx.AsyncClient(timeout=30) as http:
            resp = await http.request(method, url, headers=headers, **kwargs)

        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            return {"error": True, "status": resp.status_code, "detail": body}

        if resp.status_code == 204:
            return {"ok": True}

        return resp.json()

    async def get(
        self,
        path: str,
        params: dict | None = None,
        *,
        api_key: str | None = None,
        act_as: str | None = None,
    ) -> dict:
        return await self._request("GET", path, api_key=api_key, act_as=act_as, params=params)

    async def post(
        self,
        path: str,
        json: dict | None = None,
        *,
        api_key: str | None = None,
        act_as: str | None = None,
    ) -> dict:
        return await self._request("POST", path, api_key=api_key, act_as=act_as, json=json)

    async def put(
        self,
        path: str,
        json: dict | None = None,
        *,
        api_key: str | None = None,
        act_as: str | None = None,
    ) -> dict:
        return await self._request("PUT", path, api_key=api_key, act_as=act_as, json=json)

    async def patch(
        self,
        path: str,
        json: dict | None = None,
        *,
        api_key: str | None = None,
        act_as: str | None = None,
    ) -> dict:
        return await self._request("PATCH", path, api_key=api_key, act_as=act_as, json=json)

    async def delete(
        self,
        path: str,
        *,
        api_key: str | None = None,
        act_as: str | None = None,
    ) -> dict:
        return await self._request("DELETE", path, api_key=api_key, act_as=act_as)

    async def post_multipart(
        self,
        path: str,
        files: dict,
        params: dict | None = None,
        *,
        api_key: str | None = None,
        act_as: str | None = None,
    ) -> dict:
        """POST a multipart/form-data request.

        `files` follows httpx's convention:
            {"file": (filename, bytes, content_type)}

        The default ``Content-Type: application/json`` header from `_headers`
        is stripped — httpx must set its own multipart boundary header.
        """
        url = f"{self.base_url}{path}"
        headers = self._headers(api_key=api_key, act_as_user_id=act_as)
        # Let httpx populate the multipart Content-Type (with boundary).
        headers.pop("Content-Type", None)

        async with httpx.AsyncClient(timeout=60) as http:
            resp = await http.post(url, headers=headers, files=files, params=params)

        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            return {"error": True, "status": resp.status_code, "detail": body}

        if resp.status_code == 204:
            return {"ok": True}

        return resp.json()
