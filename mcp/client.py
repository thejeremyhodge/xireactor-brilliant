"""Cortex API HTTP client with auth and error handling."""

import os

import httpx


class CortexClient:
    """Async HTTP client for the Cortex REST API."""

    def __init__(self):
        self.base_url = os.environ.get("CORTEX_BASE_URL", "http://localhost:8010").rstrip("/")
        self.api_key = os.environ.get("CORTEX_API_KEY", "")

    def _headers(self, api_key: str | None = None) -> dict[str, str]:
        """Build request headers, optionally overriding the default API key."""
        key = api_key if api_key is not None else self.api_key
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, path: str, *, api_key: str | None = None, **kwargs) -> dict:
        """Make an HTTP request and return parsed JSON or error dict."""
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=30) as http:
            resp = await http.request(method, url, headers=self._headers(api_key), **kwargs)

        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            return {"error": True, "status": resp.status_code, "detail": body}

        if resp.status_code == 204:
            return {"ok": True}

        return resp.json()

    async def get(self, path: str, params: dict | None = None, *, api_key: str | None = None) -> dict:
        return await self._request("GET", path, api_key=api_key, params=params)

    async def post(self, path: str, json: dict | None = None, *, api_key: str | None = None) -> dict:
        return await self._request("POST", path, api_key=api_key, json=json)

    async def put(self, path: str, json: dict | None = None, *, api_key: str | None = None) -> dict:
        return await self._request("PUT", path, api_key=api_key, json=json)

    async def patch(self, path: str, json: dict | None = None, *, api_key: str | None = None) -> dict:
        return await self._request("PATCH", path, api_key=api_key, json=json)

    async def delete(self, path: str, *, api_key: str | None = None) -> dict:
        return await self._request("DELETE", path, api_key=api_key)
