"""Smoke test — exercises all 11 MCP tools against the live Cortex API."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from client import CortexClient

api = CortexClient()

passed = 0
failed = 0


async def test(name: str, coro):
    global passed, failed
    try:
        result = await coro
        if isinstance(result, dict) and result.get("error"):
            print(f"  FAIL  {name} — {result['status']}: {result['detail']}")
            failed += 1
        else:
            print(f"  PASS  {name}")
            passed += 1
        return result
    except Exception as e:
        print(f"  FAIL  {name} — {type(e).__name__}: {e}")
        failed += 1
        return None


async def main():
    print(f"Cortex MCP smoke test — {api.base_url}\n")

    # 1. Health check (direct, not an MCP tool)
    await test("health", api.get("/health"))

    # 2. get_index (L1)
    await test("get_index L1", api.get("/index", params={"depth": 1}))

    # 3. search_entries
    result = await test("search_entries", api.get("/entries", params={"limit": 5}))

    entry_id = None
    if result and not result.get("error") and result.get("entries"):
        entry_id = result["entries"][0]["id"]

    # 4. get_entry
    if entry_id:
        await test("get_entry", api.get(f"/entries/{entry_id}"))
    else:
        print("  SKIP  get_entry — no entries found")

    # 5. get_neighbors
    if entry_id:
        await test("get_neighbors", api.get(f"/entries/{entry_id}/links", params={"depth": 1}))
    else:
        print("  SKIP  get_neighbors — no entries found")

    # 6. create_entry
    create_result = await test("create_entry", api.post("/entries", json={
        "title": "MCP Smoke Test Entry",
        "content": "# Smoke Test\n\nThis entry was created by the MCP smoke test.",
        "content_type": "context",
        "logical_path": "tests/mcp-smoke-test",
        "sensitivity": "shared",
        "tags": ["test", "mcp"],
    }))

    new_id = None
    if create_result and not create_result.get("error"):
        new_id = create_result.get("id")

    # 7. update_entry
    if new_id:
        await test("update_entry", api.put(f"/entries/{new_id}", json={
            "content": "# Smoke Test (Updated)\n\nUpdated by MCP smoke test.",
            "tags": ["test", "mcp", "updated"],
        }))

    # 8. create_link
    if new_id and entry_id and new_id != entry_id:
        await test("create_link", api.post(f"/entries/{new_id}/links", json={
            "target_entry_id": entry_id,
            "link_type": "relates_to",
            "weight": 0.5,
        }))
    else:
        print("  SKIP  create_link — need two distinct entries")

    # 9. submit_staging
    staging_result = await test("submit_staging", api.post("/staging", json={
        "target_path": "tests/mcp-staging-test",
        "proposed_content": "# Staging Test\n\nProposed by MCP smoke test.",
        "change_type": "create",
        "proposed_title": "MCP Staging Test",
    }))

    # 10. list_staging
    await test("list_staging", api.get("/staging", params={"status": "pending"}))

    # 11. review_staging (approve)
    staging_id = None
    if staging_result and not staging_result.get("error"):
        staging_id = staging_result.get("id")
    if staging_id:
        await test("review_staging (approve)", api.post(f"/staging/{staging_id}/approve", json={
            "reason": "Smoke test auto-approve",
        }))
    else:
        print("  SKIP  review_staging — no staging item created")

    # 12. delete_entry (cleanup)
    if new_id:
        await test("delete_entry", api.delete(f"/entries/{new_id}"))

    print(f"\nResults: {passed} passed, {failed} failed out of {passed + failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
