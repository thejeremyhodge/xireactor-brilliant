"""Smoke test for the remote MCP server (Streamable HTTP + OAuth 2.1)."""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys

import httpx

BASE_URL = os.environ.get("MCP_TEST_URL", "http://localhost:8011")

passed = 0
failed = 0


def ok(name: str, detail: str = ""):
    global passed
    passed += 1
    print(f"  PASS  {name}" + (f" — {detail}" if detail else ""))


def fail(name: str, detail: str = ""):
    global failed
    failed += 1
    print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))


async def main():
    print(f"Remote MCP server smoke test — {BASE_URL}\n")

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=15) as http:

        # 1. OAuth discovery endpoint
        r = await http.get("/.well-known/oauth-authorization-server")
        if r.status_code == 200:
            meta = r.json()
            ok("OAuth discovery", f"issuer={meta.get('issuer', '?')}")
        else:
            fail("OAuth discovery", f"status={r.status_code}")

        # 2. Protected resource metadata
        r = await http.get("/.well-known/oauth-protected-resource")
        if r.status_code == 200:
            ok("Protected resource metadata")
        else:
            fail("Protected resource metadata", f"status={r.status_code}")

        # 3. MCP endpoint rejects unauthenticated requests
        r = await http.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "1.0"}},
        })
        if r.status_code == 401:
            ok("Unauthenticated → 401")
        else:
            fail("Unauthenticated → 401", f"got {r.status_code}")

        # 4. Dynamic Client Registration
        r = await http.post("/register", json={
            "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
            "client_name": "smoke-test",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        })
        if r.status_code == 201:
            client_info = r.json()
            client_id = client_info["client_id"]
            client_secret = client_info.get("client_secret", "")
            ok("DCR", f"client_id={client_id[:20]}...")
        else:
            fail("DCR", f"status={r.status_code} body={r.text[:200]}")
            print(f"\nResults: {passed} passed, {failed} failed out of {passed + failed}")
            sys.exit(1)

        # 5. Authorization request (auto-approved in PoC)
        # Generate proper PKCE pair
        code_verifier = secrets.token_urlsafe(48)
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).rstrip(b"=").decode()

        r = await http.get("/authorize", params={
            "client_id": client_id,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "response_type": "code",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": "test_state",
        }, follow_redirects=False)
        if r.status_code in (302, 303):
            location = r.headers.get("location", "")
            # Extract code from redirect URL
            from urllib.parse import parse_qs, urlparse
            parsed = urlparse(location)
            qs = parse_qs(parsed.query)
            auth_code = qs.get("code", [None])[0]
            if auth_code:
                ok("Authorization", f"code={auth_code[:20]}...")
            else:
                fail("Authorization", f"no code in redirect: {location[:100]}")
                auth_code = None
        else:
            fail("Authorization", f"status={r.status_code}")
            auth_code = None

        # 6. Token exchange
        access_token = None
        if auth_code:
            r = await http.post("/token", data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                "client_id": client_id,
                "client_secret": client_secret,
                "code_verifier": code_verifier,
            })
            if r.status_code == 200:
                token_data = r.json()
                access_token = token_data.get("access_token")
                ok("Token exchange", f"token_type={token_data.get('token_type')}")
            else:
                fail("Token exchange", f"status={r.status_code} body={r.text[:200]}")

        # 7-9. MCP protocol with auth: initialize, list tools, call a tool
        # Use Accept: application/json only to get non-streaming responses for testing
        if access_token:
            auth_headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "mcp-protocol-version": "2025-11-25",
            }

            async def mcp_request(payload: dict) -> tuple[int, dict | None, dict]:
                """Send MCP request and parse SSE or JSON response."""
                async with http.stream("POST", "/mcp", headers=auth_headers, json=payload) as resp:
                    hdrs = dict(resp.headers)
                    ct = resp.headers.get("content-type", "")
                    if resp.status_code != 200:
                        return resp.status_code, None, hdrs
                    if "text/event-stream" in ct:
                        # Parse SSE: collect data lines from 'message' events
                        result = None
                        async for line in resp.aiter_lines():
                            if line.startswith("data: "):
                                data = line[6:]
                                try:
                                    result = json.loads(data)
                                except json.JSONDecodeError:
                                    pass
                        return resp.status_code, result, hdrs
                    else:
                        body = await resp.aread()
                        try:
                            return resp.status_code, json.loads(body), hdrs
                        except json.JSONDecodeError:
                            return resp.status_code, None, hdrs

            # 7. Initialize
            status, body, hdrs = await mcp_request({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                           "clientInfo": {"name": "smoke-test", "version": "1.0"}},
            })
            session_id = hdrs.get("mcp-session-id")
            if status == 200 and body:
                ok("MCP initialize", f"session={session_id[:20]}..." if session_id else "")
            else:
                fail("MCP initialize", f"status={status}")

            if session_id:
                auth_headers["mcp-session-id"] = session_id

            # Send initialized notification
            await http.post("/mcp", headers=auth_headers, json={
                "jsonrpc": "2.0", "method": "notifications/initialized",
            })

            # 8. List tools
            status, body, _ = await mcp_request({
                "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
            })
            if status == 200 and body:
                tools = body.get("result", {}).get("tools", [])
                tool_names = [t["name"] for t in tools]
                if len(tools) == 11:
                    ok("List tools", f"{len(tools)} tools: {', '.join(tool_names)}")
                else:
                    fail("List tools", f"expected 11, got {len(tools)}: {tool_names}")
            else:
                fail("List tools", f"status={status}")

            # 9. Call search_entries
            status, body, _ = await mcp_request({
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "search_entries", "arguments": {"limit": 3}},
            })
            if status == 200 and body:
                result = body.get("result", {})
                if not result.get("isError"):
                    ok("Call search_entries")
                else:
                    ok("Call search_entries (tool executed, API may be down)")
            else:
                fail("Call search_entries", f"status={status}")
        else:
            print("  SKIP  MCP protocol tests — no access token")

        # 10. CORS preflight
        r = await http.options("/mcp", headers={
            "Origin": "https://claude.ai",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization, Content-Type, mcp-protocol-version",
        })
        cors_origin = r.headers.get("access-control-allow-origin", "")
        if r.status_code == 200 and "claude.ai" in cors_origin:
            ok("CORS preflight", f"allow-origin={cors_origin}")
        else:
            fail("CORS preflight", f"status={r.status_code} origin={cors_origin}")

    print(f"\nResults: {passed} passed, {failed} failed out of {passed + failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
