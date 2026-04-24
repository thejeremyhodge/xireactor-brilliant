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


def _resolve_api_base_url() -> str:
    """Resolve the API's outbound-callable base URL (mcp → api tool calls).

    Env-only resolution (T-0264.4, Sprint 0045):

    1. ``BRILLIANT_BASE_URL`` — the canonical outbound URL. Compose sets
       this to ``http://api:8000`` (service-network address); ``render.yaml``
       wires it via ``fromService.property:host`` (internal service name).
       On Render this is bare; we prepend ``https://`` if no scheme is
       present.
    2. ``http://localhost:8010`` — last-resort default for local-dev /
       debug runs without compose.

    DB read of ``brilliant_settings.api_public_url`` was removed. That
    column is semantically the *browser-visible* URL (used by
    ``mcp/remote_server.py::_resolve_api_public_url`` to build OAuth
    redirects); reading it for outbound poisoned mcp→api calls any time
    an operator set it for OAuth reasons. See ST-0209 / demo4 incident
    for the exact failure this guards against.
    """
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
