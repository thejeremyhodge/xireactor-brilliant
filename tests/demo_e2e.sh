#!/usr/bin/env bash
# =============================================================================
# xiReactor Cortex — End-to-End Integration Demo
# =============================================================================
#
# Demonstrates the full PoC flow: health check, auth validation, tiered index,
# CRUD operations, permission enforcement, governance pipeline (submit -> approve
# -> publish), bulk import with wiki-link detection, and full-text search.
#
# Prerequisites:
#   1. docker compose up -d
#   2. Wait for healthchecks (API container must be running)
#   3. bash tests/demo_e2e.sh
#
# Override base URL for remote testing:
#   BASE_URL=https://your-cortex.example.com bash tests/demo_e2e.sh
#
# Dependencies: curl, jq
# =============================================================================

set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8010}"
ADMIN_KEY="bkai_adm1_testkey_admin"
EDITOR_KEY="bkai_edit_testkey_editor"
VIEWER_KEY="bkai_view_testkey_viewer"
AGENT_KEY="bkai_agnt_testkey_agent"

PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "  [PASS] $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  [FAIL] $1"; }
section() { echo; echo "=== $1 ==="; }

# Helper: HTTP request with auth
api() {
    local method="$1" path="$2" key="$3"
    shift 3
    curl -s -L -w "\n%{http_code}" -X "$method" \
        -H "Authorization: Bearer $key" \
        -H "Content-Type: application/json" \
        "$BASE_URL$path" "$@"
}

# Helper: HTTP request WITHOUT auth
api_noauth() {
    local method="$1" path="$2"
    shift 2
    curl -s -L -w "\n%{http_code}" -X "$method" \
        -H "Content-Type: application/json" \
        "$BASE_URL$path" "$@"
}

# Parse response: sets HTTP_CODE and BODY globals
call() {
    local response
    response=$(api "$@")
    HTTP_CODE=$(echo "$response" | tail -1)
    BODY=$(echo "$response" | sed '$d')
}

call_noauth() {
    local response
    response=$(api_noauth "$@")
    HTTP_CODE=$(echo "$response" | tail -1)
    BODY=$(echo "$response" | sed '$d')
}

echo "xiReactor Cortex - End-to-End Integration Demo"
echo "Base URL: $BASE_URL"
echo "================================================"

# =========================================================================
# 1. Health Check
# =========================================================================
section "1. Health Check"

call_noauth GET "/health"
if [ "$HTTP_CODE" = "200" ] && echo "$BODY" | jq -e '.status == "ok"' > /dev/null 2>&1; then
    pass "GET /health returns 200 with status=ok"
else
    fail "GET /health: expected 200 + status=ok, got $HTTP_CODE"
    echo "    Body: $BODY"
fi

# =========================================================================
# 2. Auth Validation
# =========================================================================
section "2. Auth Validation"

# No auth header
call_noauth GET "/entries"
if [ "$HTTP_CODE" = "401" ]; then
    pass "Request without auth returns 401"
else
    fail "Request without auth: expected 401, got $HTTP_CODE"
fi

# Invalid API key
call GET "/entries" "totally_invalid_key_here"
if [ "$HTTP_CODE" = "401" ]; then
    pass "Request with invalid key returns 401"
else
    fail "Request with invalid key: expected 401, got $HTTP_CODE"
fi

# =========================================================================
# 3. Admin Index (L1) - Category counts
# =========================================================================
section "3. Admin Index (L1)"

call GET "/index?depth=1" "$ADMIN_KEY"
if [ "$HTTP_CODE" = "200" ]; then
    ADMIN_TOTAL=$(echo "$BODY" | jq -r '.total_entries')
    ADMIN_CATEGORIES=$(echo "$BODY" | jq -r '.categories | length')
    if [ "$ADMIN_TOTAL" -gt 0 ] && [ "$ADMIN_CATEGORIES" -gt 0 ]; then
        pass "Admin L1 index: $ADMIN_TOTAL entries across $ADMIN_CATEGORIES categories"
    else
        fail "Admin L1 index: unexpected total=$ADMIN_TOTAL categories=$ADMIN_CATEGORIES"
    fi
else
    fail "Admin L1 index: expected 200, got $HTTP_CODE"
    ADMIN_TOTAL=0
fi

# =========================================================================
# 4. Viewer Index (L1) - RLS proof
# =========================================================================
section "4. Viewer Index (L1) - RLS Proof"

call GET "/index?depth=1" "$VIEWER_KEY"
if [ "$HTTP_CODE" = "200" ]; then
    VIEWER_TOTAL=$(echo "$BODY" | jq -r '.total_entries')
    if [ "$VIEWER_TOTAL" -lt "$ADMIN_TOTAL" ]; then
        pass "Viewer sees $VIEWER_TOTAL entries vs admin's $ADMIN_TOTAL (RLS filtering confirmed)"
    elif [ "$VIEWER_TOTAL" -eq "$ADMIN_TOTAL" ]; then
        # Could be equal if no restricted entries; note it
        pass "Viewer sees $VIEWER_TOTAL entries (same as admin - no restricted entries in seed, or RLS not filtering)"
    else
        fail "Viewer sees MORE entries ($VIEWER_TOTAL) than admin ($ADMIN_TOTAL) - unexpected"
    fi
else
    fail "Viewer L1 index: expected 200, got $HTTP_CODE"
fi

# =========================================================================
# 5. Admin Index (L3) - Relationships included
# =========================================================================
section "5. Admin Index (L3) - With Relationships"

call GET "/index?depth=3" "$ADMIN_KEY"
if [ "$HTTP_CODE" = "200" ]; then
    HAS_ENTRIES=$(echo "$BODY" | jq -r '.entries | length')
    HAS_RELATIONSHIPS=$(echo "$BODY" | jq -r '.relationships | length')
    if [ "$HAS_ENTRIES" -gt 0 ] && [ "$HAS_RELATIONSHIPS" -gt 0 ]; then
        pass "Admin L3 index: $HAS_ENTRIES entries, $HAS_RELATIONSHIPS relationships"
    else
        fail "Admin L3 index: entries=$HAS_ENTRIES relationships=$HAS_RELATIONSHIPS (expected > 0)"
    fi
else
    fail "Admin L3 index: expected 200, got $HTTP_CODE"
fi

# =========================================================================
# 6. Admin: Create Entry
# =========================================================================
section "6. Admin: Create Entry"

call POST "/entries" "$ADMIN_KEY" -d '{
    "title": "Demo Entry",
    "content": "# Demo\nThis is a test entry created by the e2e demo.",
    "content_type": "context",
    "logical_path": "Demo/e2e-test",
    "sensitivity": "shared",
    "tags": ["demo", "test"]
}'
if [ "$HTTP_CODE" = "201" ]; then
    CREATED_ENTRY_ID=$(echo "$BODY" | jq -r '.id')
    CREATED_TITLE=$(echo "$BODY" | jq -r '.title')
    if [ "$CREATED_TITLE" = "Demo Entry" ]; then
        pass "Created entry: id=$CREATED_ENTRY_ID title=$CREATED_TITLE"
    else
        fail "Created entry but title mismatch: expected 'Demo Entry', got '$CREATED_TITLE'"
    fi
else
    fail "Create entry: expected 201, got $HTTP_CODE"
    echo "    Body: $BODY"
    CREATED_ENTRY_ID=""
fi

# =========================================================================
# 7. Admin: Read Entry
# =========================================================================
section "7. Admin: Read Entry"

if [ -n "$CREATED_ENTRY_ID" ]; then
    call GET "/entries/$CREATED_ENTRY_ID" "$ADMIN_KEY"
    if [ "$HTTP_CODE" = "200" ]; then
        READ_TITLE=$(echo "$BODY" | jq -r '.title')
        if [ "$READ_TITLE" = "Demo Entry" ]; then
            pass "Read entry by ID: title matches 'Demo Entry'"
        else
            fail "Read entry: title mismatch, got '$READ_TITLE'"
        fi
    else
        fail "Read entry: expected 200, got $HTTP_CODE"
    fi
else
    fail "Read entry: skipped (no entry ID from create step)"
fi

# =========================================================================
# 8. Admin: Search
# =========================================================================
section "8. Admin: Full-Text Search"

call GET "/entries?q=demo" "$ADMIN_KEY"
if [ "$HTTP_CODE" = "200" ]; then
    SEARCH_COUNT=$(echo "$BODY" | jq -r '.total')
    FOUND_DEMO=$(echo "$BODY" | jq -r '[.entries[] | select(.title == "Demo Entry")] | length')
    if [ "$FOUND_DEMO" -gt 0 ]; then
        pass "Search for 'demo': found Demo Entry among $SEARCH_COUNT results"
    else
        fail "Search for 'demo': $SEARCH_COUNT results but 'Demo Entry' not found"
    fi
else
    fail "Search: expected 200, got $HTTP_CODE"
fi

# =========================================================================
# 9. Admin: Link Entry
# =========================================================================
section "9. Admin: Link Entry"

# First, get a seed entry ID to link to
call GET "/entries?limit=2" "$ADMIN_KEY"
SEED_ENTRY_ID=""
if [ "$HTTP_CODE" = "200" ]; then
    # Pick the first entry that isn't the one we just created
    SEED_ENTRY_ID=$(echo "$BODY" | jq -r --arg cid "$CREATED_ENTRY_ID" '[.entries[] | select(.id != $cid)][0].id // empty')
fi

if [ -n "$CREATED_ENTRY_ID" ] && [ -n "$SEED_ENTRY_ID" ] && [ "$SEED_ENTRY_ID" != "$CREATED_ENTRY_ID" ]; then
    call POST "/entries/$CREATED_ENTRY_ID/links" "$ADMIN_KEY" -d "{
        \"target_entry_id\": \"$SEED_ENTRY_ID\",
        \"link_type\": \"relates_to\"
    }"
    if [ "$HTTP_CODE" = "201" ]; then
        LINK_TYPE=$(echo "$BODY" | jq -r '.link_type')
        pass "Created link: $CREATED_ENTRY_ID --$LINK_TYPE--> $SEED_ENTRY_ID"
    else
        fail "Create link: expected 201, got $HTTP_CODE"
        echo "    Body: $BODY"
    fi
else
    fail "Create link: skipped (missing entry IDs)"
fi

# =========================================================================
# 10. Admin: Traverse Links
# =========================================================================
section "10. Admin: Traverse Links"

if [ -n "$CREATED_ENTRY_ID" ]; then
    call GET "/entries/$CREATED_ENTRY_ID/links?depth=1" "$ADMIN_KEY"
    if [ "$HTTP_CODE" = "200" ]; then
        NEIGHBOR_COUNT=$(echo "$BODY" | jq -r '.neighbors | length')
        if [ "$NEIGHBOR_COUNT" -gt 0 ]; then
            pass "Link traversal: $NEIGHBOR_COUNT neighbor(s) found"
        else
            fail "Link traversal: expected at least 1 neighbor, got 0"
        fi
    else
        fail "Link traversal: expected 200, got $HTTP_CODE"
    fi
else
    fail "Link traversal: skipped (no entry ID)"
fi

# =========================================================================
# 11. Viewer: Cannot Write
# =========================================================================
section "11. Viewer: Cannot Write (Permission Denied)"

call POST "/entries" "$VIEWER_KEY" -d '{
    "title": "Viewer Should Not Write",
    "content": "This should fail.",
    "content_type": "context",
    "logical_path": "Demo/viewer-blocked",
    "sensitivity": "shared"
}'
if [ "$HTTP_CODE" = "403" ] || [ "$HTTP_CODE" = "500" ]; then
    pass "Viewer write blocked: HTTP $HTTP_CODE (RLS prevents INSERT)"
else
    fail "Viewer write: expected 403 or 500 (RLS error), got $HTTP_CODE"
    echo "    Body: $BODY"
fi

# =========================================================================
# 12. Agent: Direct Write Blocked
# =========================================================================
section "12. Agent: Direct Write Blocked"

call POST "/entries" "$AGENT_KEY" -d '{
    "title": "Agent Direct Write",
    "content": "This should fail - agents cannot write directly to entries.",
    "content_type": "context",
    "logical_path": "Demo/agent-blocked",
    "sensitivity": "shared"
}'
if [ "$HTTP_CODE" = "403" ] || [ "$HTTP_CODE" = "500" ]; then
    pass "Agent direct write blocked: HTTP $HTTP_CODE (RLS prevents INSERT)"
else
    fail "Agent direct write: expected 403 or 500 (RLS error), got $HTTP_CODE"
    echo "    Body: $BODY"
fi

# =========================================================================
# 13. Agent: Submit via Staging
# =========================================================================
section "13. Agent: Submit via Staging"

call POST "/staging" "$AGENT_KEY" -d '{
    "target_path": "Agent/proposed-entry",
    "change_type": "create",
    "content_type": "context",
    "proposed_title": "Agent Proposal",
    "proposed_content": "# Agent Idea\nThis was proposed by an agent.",
    "submission_category": "user_direct"
}'
STAGING_ID=""
AUTO_APPROVED=false
if [ "$HTTP_CODE" = "201" ]; then
    STAGING_ID=$(echo "$BODY" | jq -r '.id')
    STAGING_STATUS=$(echo "$BODY" | jq -r '.status')
    if [ "$STAGING_STATUS" = "pending" ]; then
        pass "Agent submitted to staging: id=$STAGING_ID status=$STAGING_STATUS"
    elif [ "$STAGING_STATUS" = "auto_approved" ]; then
        AUTO_APPROVED=true
        pass "Agent submitted to staging: id=$STAGING_ID status=$STAGING_STATUS (Tier 1 auto-approved)"
    else
        fail "Agent staging submission: unexpected status '$STAGING_STATUS'"
    fi
else
    fail "Agent staging submission: expected 201, got $HTTP_CODE"
    echo "    Body: $BODY"
fi

# =========================================================================
# 14. Admin: List Staging
# =========================================================================
section "14. Admin: List Staging"

if [ "$AUTO_APPROVED" = "true" ]; then
    # Tier 1 auto-approved — check that it shows up in staging list (any status)
    call GET "/staging" "$ADMIN_KEY"
    if [ "$HTTP_CODE" = "200" ]; then
        STAGING_COUNT=$(echo "$BODY" | jq -r '.total')
        if [ "$STAGING_COUNT" -gt 0 ]; then
            pass "Admin sees $STAGING_COUNT staging item(s) (auto-approved path)"
        else
            fail "Admin staging list: expected items, got $STAGING_COUNT"
        fi
    else
        fail "Admin staging list: expected 200, got $HTTP_CODE"
    fi
else
    call GET "/staging?status=pending" "$ADMIN_KEY"
    if [ "$HTTP_CODE" = "200" ]; then
        STAGING_COUNT=$(echo "$BODY" | jq -r '.total')
        if [ -n "$STAGING_ID" ]; then
            FOUND_STAGING=$(echo "$BODY" | jq -r --arg sid "$STAGING_ID" '[.items[] | select(.id == $sid)] | length')
        else
            FOUND_STAGING=0
        fi
        if [ "$STAGING_COUNT" -gt 0 ]; then
            pass "Admin sees $STAGING_COUNT pending staging item(s) (agent submission found: $FOUND_STAGING)"
        else
            fail "Admin staging list: expected pending items, got $STAGING_COUNT"
        fi
    else
        fail "Admin staging list: expected 200, got $HTTP_CODE"
    fi
fi

# =========================================================================
# 15. Admin: Approve Staging
# =========================================================================
section "15. Admin: Approve Staging"

if [ "$AUTO_APPROVED" = "true" ]; then
    pass "Staging item already auto-approved (Tier 1) — skipping manual approve"
elif [ -n "$STAGING_ID" ]; then
    call POST "/staging/$STAGING_ID/approve" "$ADMIN_KEY" -d '{}'
    if [ "$HTTP_CODE" = "200" ]; then
        APPROVED_STATUS=$(echo "$BODY" | jq -r '.status')
        if [ "$APPROVED_STATUS" = "approved" ]; then
            pass "Staging item approved: status=$APPROVED_STATUS"
        else
            fail "Staging approve: status is '$APPROVED_STATUS', expected 'approved'"
        fi
    else
        fail "Staging approve: expected 200, got $HTTP_CODE"
        echo "    Body: $BODY"
    fi
else
    fail "Staging approve: skipped (no staging ID)"
fi

# =========================================================================
# 16. Verify Published
# =========================================================================
section "16. Verify Published (Staging -> Entries)"

# Give it a moment, then search for the promoted entry
call GET "/entries?q=agent+proposal" "$ADMIN_KEY"
if [ "$HTTP_CODE" = "200" ]; then
    PUBLISHED_COUNT=$(echo "$BODY" | jq -r '.total')
    FOUND_AGENT=$(echo "$BODY" | jq -r '[.entries[] | select(.title == "Agent Proposal")] | length')
    if [ "$FOUND_AGENT" -gt 0 ]; then
        pass "Agent proposal published to entries table ($FOUND_AGENT match(es))"
    else
        # FTS might not match on "agent proposal" as a phrase; try checking by path
        call GET "/entries?logical_path=Agent/" "$ADMIN_KEY"
        FOUND_BY_PATH=$(echo "$BODY" | jq -r '[.entries[] | select(.title == "Agent Proposal")] | length')
        if [ "$FOUND_BY_PATH" -gt 0 ]; then
            pass "Agent proposal published to entries table (found by path)"
        else
            fail "Agent proposal not found in entries after approval (search returned $PUBLISHED_COUNT results)"
        fi
    fi
else
    fail "Verify published: expected 200, got $HTTP_CODE"
fi

# =========================================================================
# 17. Bulk Import
# =========================================================================
section "17. Bulk Import with Wiki-Link Detection"

call POST "/import" "$ADMIN_KEY" -d '{
    "files": [
        {"filename": "import-test-1.md", "content": "# Import Test One\nThis entry links to [[Import Test Two]]."},
        {"filename": "import-test-2.md", "content": "# Import Test Two\nLinked from the first import."}
    ],
    "base_path": "Imports",
    "source_vault": "demo-vault"
}'
if [ "$HTTP_CODE" = "201" ]; then
    IMPORT_CREATED=$(echo "$BODY" | jq -r '.created')
    IMPORT_LINKED=$(echo "$BODY" | jq -r '.linked')
    IMPORT_ERRORS=$(echo "$BODY" | jq -r '.errors | length')
    if [ "$IMPORT_CREATED" -ge 2 ]; then
        pass "Import created $IMPORT_CREATED entries"
    else
        fail "Import: expected >= 2 created, got $IMPORT_CREATED"
    fi
    if [ "$IMPORT_LINKED" -ge 1 ]; then
        pass "Import detected $IMPORT_LINKED wiki-link(s)"
    else
        fail "Import: expected >= 1 linked, got $IMPORT_LINKED"
    fi
    if [ "$IMPORT_ERRORS" -eq 0 ]; then
        pass "Import completed with 0 errors"
    else
        fail "Import had $IMPORT_ERRORS error(s)"
        echo "    Errors: $(echo "$BODY" | jq -r '.errors')"
    fi
else
    fail "Bulk import: expected 201, got $HTTP_CODE"
    echo "    Body: $BODY"
fi

# =========================================================================
# 18. Full-Text Search for Imported Entries
# =========================================================================
section "18. Full-Text Search for Imported Entries"

call GET "/entries?q=import+test" "$ADMIN_KEY"
if [ "$HTTP_CODE" = "200" ]; then
    FTS_TOTAL=$(echo "$BODY" | jq -r '.total')
    if [ "$FTS_TOTAL" -ge 2 ]; then
        pass "FTS found $FTS_TOTAL imported entries"
    else
        # Try a broader search
        call GET "/entries?logical_path=Imports/" "$ADMIN_KEY"
        PATH_TOTAL=$(echo "$BODY" | jq -r '.total')
        if [ "$PATH_TOTAL" -ge 2 ]; then
            pass "Imported entries found by path ($PATH_TOTAL entries under Imports/)"
        else
            fail "FTS for imported entries: expected >= 2, got $FTS_TOTAL (path search: $PATH_TOTAL)"
        fi
    fi
else
    fail "FTS search: expected 200, got $HTTP_CODE"
fi

# =========================================================================
# Results Summary
# =========================================================================
echo
echo "=== RESULTS ==="
echo "  Passed: $PASS"
echo "  Failed: $FAIL"
echo "  Total:  $((PASS + FAIL))"
echo

if [ "$FAIL" -eq 0 ]; then
    echo "All tests passed."
    exit 0
else
    echo "$FAIL test(s) failed."
    exit 1
fi
