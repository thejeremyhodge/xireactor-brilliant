# xiReactor Brilliant API Reference

> **Note:** Examples below use `http://localhost:8010` as the base URL. Replace with your deployed Brilliant API URL if running remotely (e.g., `https://your-domain.example.com`).

Base URL: `http://localhost:8010`

---

## Authentication

All endpoints (except `/health`, `/auth/login`, and `/invitations/redeem`) require a Bearer token in the `Authorization` header.

```
Authorization: Bearer <api_key>
```

**API key types and source tracking:**

| Key Type | Source Tag | Behavior |
|---|---|---|
| `interactive` | `web_ui` | Direct writes for admin/editor roles |
| `agent` | `agent` | All writes routed through staging pipeline |
| `api_integration` | `api` | Direct writes for admin/editor roles |

**Seed data keys for development:**

| User | Role | Key |
|---|---|---|
| Admin User | admin | `bkai_adm1_testkey_admin` |
| Agent Bot | agent | `bkai_agnt_testkey_agent` |

**Error responses:**

| Status | Meaning |
|---|---|
| 401 | Missing, invalid, or expired API key |
| 403 | Insufficient permissions for this action |
| 404 | Resource not found (or hidden by RLS) |
| 409 | Conflict (e.g., staging item already reviewed) |
| 422 | Validation error (invalid content_type, sensitivity, etc.) |

---

## Endpoints

### 1. Health Check

**`GET /health`**

Returns API status. No authentication required.

```bash
curl -s http://localhost:8010/health
```

**Response:**

```json
{
  "status": "ok"
}
```

---

### 2. Session Init

**`GET /session-init`**

Returns a pre-assembled context bundle for agent session start. Dynamically selects index depth based on KB size. Includes a `pending_reviews` section that surfaces Tier 3+ governance items awaiting human review, scoped to the caller's organization. The `count` field gives the total (up to 20), `items` previews the top 5 with age, and `review_url` links to the full filtered staging list.

```bash
curl -s http://localhost:8010/session-init \
  -H "Authorization: Bearer bkai_adm1_testkey_admin"
```

**Depth selection:**

| Total Entries | Index Depth |
|---|---|
| ≤50 | L4 (summaries) |
| ≤500 | L3 (relationships) |
| ≤5000 | L2 (document index) |
| >5000 | L1 (category counts) |

**Response (200):**

```json
{
  "index": {
    "depth": 4,
    "total_entries": 15,
    "categories": [
      { "content_type": "context", "count": 5 },
      ...
    ],
    "entries": [
      {
        "id": "a1b2c3d4-...",
        "title": "Client Onboarding SOP",
        "content_type": "resource",
        "logical_path": "processes/onboarding/client-sop",
        "summary": "Step-by-step process for onboarding...",
        "updated_at": "2026-04-04T12:00:00"
      },
      ...
    ],
    "summaries": { "a1b2c3d4-...": "Step-by-step process..." },
    "relationships": [
      { "source_id": "...", "target_id": "...", "link_type": "relates_to" }
    ]
  },
  "system_entries": [
    {
      "id": "...",
      "title": "Type Registry",
      "content": "...",
      "content_type": "system",
      "logical_path": "System/type-registry"
    }
  ],
  "pending_reviews": {
    "count": 2,
    "items": [
      {
        "id": "...",
        "target_path": "Projects/alpha/brief",
        "change_type": "update",
        "governance_tier": 3,
        "submitted_by": "...",
        "age_hours": 4.2
      }
    ],
    "review_url": "/staging?status=pending&tier_gte=3"
  },
  "metadata": {
    "total_entries": 15,
    "last_updated": "2026-04-04T12:00:00",
    "user": {
      "id": "...",
      "display_name": "Admin User",
      "role": "admin",
      "department": null,
      "source": "web_ui"
    }
  }
}
```

---

### 3. Create Entry

**`POST /entries`**

Create a new knowledge base entry. The `source` field is auto-set from your API key type.

**Request body:**

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `title` | string | yes | | Entry title |
| `content` | string | yes | | Markdown content |
| `content_type` | string | yes | | One of: `context`, `project`, `meeting`, `decision`, `intelligence`, `daily`, `resource`, `department`, `team`, `system`, `onboarding` |
| `logical_path` | string | yes | | Hierarchical path (e.g., `projects/alpha/overview`) |
| `sensitivity` | string | no | `shared` | One of: `system`, `strategic`, `operational`, `private`, `project`, `meeting`, `shared` |
| `summary` | string | no | null | Brief summary for L4 index |
| `department` | string | no | null | Department scope |
| `tags` | string[] | no | `[]` | Searchable tags |
| `domain_meta` | object | no | `{}` | Arbitrary metadata |
| `project_id` | string | no | null | Associated project ID |

```bash
curl -s -X POST http://localhost:8010/entries \
  -H "Authorization: Bearer bkai_adm1_testkey_admin" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Client Onboarding SOP",
    "content": "# Client Onboarding\n\nStep 1: Initial consultation...",
    "content_type": "resource",
    "logical_path": "processes/onboarding/client-sop",
    "sensitivity": "shared",
    "tags": ["onboarding", "sop"]
  }'
```

**Response (201):**

```json
{
  "id": "a1b2c3d4-...",
  "org_id": "...",
  "title": "Client Onboarding SOP",
  "content": "# Client Onboarding\n\nStep 1: Initial consultation...",
  "summary": null,
  "content_type": "resource",
  "logical_path": "processes/onboarding/client-sop",
  "sensitivity": "shared",
  "department": null,
  "owner_id": "...",
  "tags": ["onboarding", "sop"],
  "domain_meta": {},
  "version": 1,
  "status": "published",
  "source": "web_ui",
  "created_by": "...",
  "updated_by": "...",
  "created_at": "2026-04-04T12:00:00Z",
  "updated_at": "2026-04-04T12:00:00Z"
}
```

---

### 3. Get Entry

**`GET /entries/{id}`**

Retrieve a single entry by ID. RLS automatically enforces visibility based on your role and permissions.

```bash
curl -s http://localhost:8010/entries/a1b2c3d4-... \
  -H "Authorization: Bearer bkai_adm1_testkey_admin"
```

**Response (200):** Same shape as Create Entry response.

**Response (404):** Entry does not exist or is not visible to your role.

---

### 4. List / Search Entries

**`GET /entries`**

List entries with optional full-text search and filters. Results are automatically filtered by RLS.

**Query parameters:**

| Param | Type | Default | Description |
|---|---|---|---|
| `q` | string | null | Full-text search query (uses PostgreSQL `websearch_to_tsquery`) |
| `content_type` | string | null | Filter by content type |
| `logical_path` | string | null | Filter by path prefix (e.g., `projects/` matches all projects) |
| `department` | string | null | Filter by department |
| `tag` | string | null | Filter by tag (entries containing this tag) |
| `limit` | int | 50 | Results per page (1-200) |
| `offset` | int | 0 | Pagination offset |

**Full-text search:**

```bash
curl -s "http://localhost:8010/entries?q=onboarding&limit=10" \
  -H "Authorization: Bearer bkai_adm1_testkey_admin"
```

**Filter by content type:**

```bash
curl -s "http://localhost:8010/entries?content_type=decision" \
  -H "Authorization: Bearer bkai_adm1_testkey_admin"
```

**Filter by path prefix:**

```bash
curl -s "http://localhost:8010/entries?logical_path=projects/" \
  -H "Authorization: Bearer bkai_adm1_testkey_admin"
```

**Response (200):**

```json
{
  "entries": [
    { "id": "...", "title": "...", "content": "...", ... }
  ],
  "total": 42,
  "limit": 10,
  "offset": 0
}
```

When `q` is provided, results are ranked by relevance. Otherwise, results are ordered by `updated_at` descending.

---

### 5. Update Entry

**`PUT /entries/{id}`**

Partial update of an entry. Only include fields you want to change. Automatically creates a version snapshot before applying changes and bumps the version number.

Supports optimistic concurrency: pass `expected_version` to reject the update if the entry has been modified since you last read it (returns 409 Conflict).

**Request body (all fields optional):**

| Field | Type | Description |
|---|---|---|
| `title` | string | New title |
| `content` | string | New content |
| `summary` | string | New summary |
| `content_type` | string | New content type |
| `logical_path` | string | New path |
| `sensitivity` | string | New sensitivity level |
| `department` | string | New department |
| `tags` | string[] | Replacement tags array |
| `domain_meta` | object | Replacement metadata object |
| `expected_version` | int | Optimistic concurrency check — reject if entry version differs |

```bash
curl -s -X PUT http://localhost:8010/entries/a1b2c3d4-... \
  -H "Authorization: Bearer bkai_adm1_testkey_admin" \
  -H "Content-Type: application/json" \
  -d '{
    "content": "# Client Onboarding (Updated)\n\nRevised step 1...",
    "tags": ["onboarding", "sop", "v2"],
    "expected_version": 3
  }'
```

**Response (200):** Updated entry with incremented `version` field.

**Response (409):** Entry has been modified — `expected_version` does not match current version.

---

### 5a. Append to Entry

**`PATCH /entries/{id}/append`**

Append content to an existing entry without replacing existing text. Atomically concatenates new content after a separator. Creates a version snapshot before applying.

**Request body:**

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `content` | string | yes | | Content to append |
| `separator` | string | no | `"\n\n"` | Separator between existing and new content |

```bash
curl -s -X PATCH http://localhost:8010/entries/a1b2c3d4-.../append \
  -H "Authorization: Bearer bkai_adm1_testkey_admin" \
  -H "Content-Type: application/json" \
  -d '{
    "content": "## New Section\n\nAdditional information added later."
  }'
```

**Response (200):** Updated entry with incremented `version` field and concatenated content.

**Response (403):** Agent keys must use staging — submit via `POST /staging` with `change_type: append`.

---

### 6. Delete Entry (Soft Delete)

**`DELETE /entries/{id}`**

Soft-deletes an entry by setting its status to `archived`. The entry remains in the database but is excluded from search results and index maps.

```bash
curl -s -X DELETE http://localhost:8010/entries/a1b2c3d4-... \
  -H "Authorization: Bearer bkai_adm1_testkey_admin"
```

**Response (200):**

```json
{
  "message": "Entry archived"
}
```

---

### 7. Create Link

**`POST /entries/{id}/links`**

Create a typed link from one entry to another. Links represent relationships between knowledge base entries.

**Request body:**

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `target_entry_id` | string | yes | | ID of the target entry |
| `link_type` | string | yes | | One of: `relates_to`, `supersedes`, `contradicts`, `depends_on`, `part_of`, `tagged_with` |
| `weight` | float | no | 1.0 | Link strength (0.0 to 1.0+) |
| `metadata` | object | no | `{}` | Arbitrary link metadata |

**Link types:**

| Type | Directionality | Meaning |
|---|---|---|
| `relates_to` | bidirectional | General relationship |
| `supersedes` | directional | Source replaces target |
| `contradicts` | bidirectional | Source conflicts with target |
| `depends_on` | directional | Source requires target |
| `part_of` | directional | Source is a component of target |
| `tagged_with` | directional | Source is categorized by target |

```bash
curl -s -X POST http://localhost:8010/entries/a1b2c3d4-.../links \
  -H "Authorization: Bearer bkai_adm1_testkey_admin" \
  -H "Content-Type: application/json" \
  -d '{
    "target_entry_id": "e5f6g7h8-...",
    "link_type": "relates_to",
    "weight": 1.0
  }'
```

**Response (201):**

```json
{
  "id": "...",
  "source_entry_id": "a1b2c3d4-...",
  "target_entry_id": "e5f6g7h8-...",
  "link_type": "relates_to",
  "weight": 1.0,
  "metadata": {},
  "created_by": "...",
  "source": "web_ui",
  "created_at": "2026-04-04T12:00:00Z"
}
```

---

### 8. Get Neighbors (Link Traversal)

**`GET /entries/{id}/links`**

Traverse the knowledge graph from an entry. Returns neighboring entries connected by links, with configurable traversal depth.

**Query parameters:**

| Param | Type | Default | Description |
|---|---|---|---|
| `depth` | int | 1 | Traversal depth (1-3). 1 = direct neighbors only. 2-3 = recursive CTE with cycle prevention. |

Bidirectional link types (`relates_to`, `contradicts`) are traversed in both directions. Directional link types (`supersedes`, `depends_on`, `part_of`, `tagged_with`) follow the outgoing direction only.

**1-hop neighbors:**

```bash
curl -s "http://localhost:8010/entries/a1b2c3d4-.../links?depth=1" \
  -H "Authorization: Bearer bkai_adm1_testkey_admin"
```

**2-hop traversal:**

```bash
curl -s "http://localhost:8010/entries/a1b2c3d4-.../links?depth=2" \
  -H "Authorization: Bearer bkai_adm1_testkey_admin"
```

**Response (200):**

```json
{
  "origin_id": "a1b2c3d4-...",
  "depth": 2,
  "neighbors": [
    {
      "entry_id": "e5f6g7h8-...",
      "title": "CRM Integration Guide",
      "summary": "How to connect to the CRM system...",
      "content_type": "resource",
      "link_type": "relates_to",
      "weight": 1.0,
      "depth": 1
    },
    {
      "entry_id": "i9j0k1l2-...",
      "title": "API Authentication Spec",
      "summary": null,
      "content_type": "context",
      "link_type": "depends_on",
      "weight": 0.8,
      "depth": 2
    }
  ]
}
```

---

### 9. Content Type Registry

**`GET /types`**

List all registered content types. Returns canonical types and aliases.

```bash
curl -s http://localhost:8010/types \
  -H "Authorization: Bearer bkai_adm1_testkey_admin"
```

**Response (200):**

```json
{
  "types": [
    { "name": "context", "description": "Organizational context", "alias_of": null, "is_active": true },
    { "name": "project", "description": "Project documentation", "alias_of": null, "is_active": true },
    { "name": "tasks", "description": "", "alias_of": "project", "is_active": true },
    ...
  ]
}
```

**`POST /types`** (admin only)

Register a new content type.

**Query parameters:**

| Param | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Type name |
| `description` | string | no | Type description |
| `alias_of` | string | no | Canonical type this is an alias for |

```bash
curl -s -X POST "http://localhost:8010/types?name=playbook&description=Operational+playbooks" \
  -H "Authorization: Bearer bkai_adm1_testkey_admin"
```

**Response (201):** The created type record.

**Response (403):** Only admins can register content types.

**Response (409):** Content type already exists.

---

### 9a. Tiered Index Map

**`GET /index`**

Returns a permission-filtered, tiered index map of the entire knowledge base. This is the primary mechanism for giving agents ambient context about what is in the KB.

**Query parameters:**

| Param | Type | Default | Description |
|---|---|---|---|
| `depth` | int | 1 | Index depth level (1-5) |
| `path` | string | null | Filter by logical_path prefix (e.g., `Projects/`) |
| `content_type` | string | null | Filter by content type |

**Depth levels:**

| Level | Contains | Token Cost | Use Case |
|---|---|---|---|
| L1 | Category counts by content_type | Minimal | Quick overview of KB structure |
| L2 | + Document index (titles, IDs, paths, timestamps) | Low | Know what exists, find by title |
| L3 | + Relationships between entries | Medium | Understand how documents relate |
| L4 | + Summaries for each entry | High | Decide which entries to read in full |
| L5 | + Full content of all entries | Very high | Load everything (use sparingly) |

**L1 - Categories only:**

```bash
curl -s "http://localhost:8010/index?depth=1" \
  -H "Authorization: Bearer bkai_adm1_testkey_admin"
```

**Response:**

```json
{
  "depth": 1,
  "total_entries": 15,
  "categories": [
    { "content_type": "context", "count": 5 },
    { "content_type": "decision", "count": 3 },
    { "content_type": "project", "count": 3 },
    { "content_type": "meeting", "count": 2 },
    { "content_type": "resource", "count": 2 }
  ]
}
```

**L3 - Structure + Relationships (recommended session start):**

```bash
curl -s "http://localhost:8010/index?depth=3" \
  -H "Authorization: Bearer bkai_adm1_testkey_admin"
```

**Response:**

```json
{
  "depth": 3,
  "total_entries": 15,
  "categories": [
    { "content_type": "context", "count": 5 },
    ...
  ],
  "entries": [
    {
      "id": "a1b2c3d4-...",
      "title": "Client Onboarding SOP",
      "content_type": "resource",
      "logical_path": "processes/onboarding/client-sop",
      "updated_at": "2026-04-04T12:00:00Z"
    },
    ...
  ],
  "relationships": [
    {
      "source_id": "a1b2c3d4-...",
      "target_id": "e5f6g7h8-...",
      "link_type": "relates_to"
    },
    ...
  ]
}
```

**L5 - Full content:**

```bash
curl -s "http://localhost:8010/index?depth=5" \
  -H "Authorization: Bearer bkai_adm1_testkey_admin"
```

**Response (adds `summaries` and `contents` maps):**

```json
{
  "depth": 5,
  "total_entries": 15,
  "categories": [...],
  "entries": [...],
  "relationships": [...],
  "summaries": {
    "a1b2c3d4-...": "Step-by-step process for onboarding new clients...",
    ...
  },
  "contents": {
    "a1b2c3d4-...": "# Client Onboarding\n\nStep 1: Initial consultation...",
    ...
  }
}
```

**Permission filtering:** Different users see different index results. A viewer sees fewer entries than an admin because RLS filters out private and system-level content.

---

### 10. Submit to Staging

**`POST /staging`**

Submit a proposed change to the governance pipeline. All writes from agent-type API keys are routed through staging regardless of the user's role.

**Request body:**

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `target_path` | string | yes | | Logical path for the proposed entry |
| `proposed_content` | string | yes | | Proposed markdown content |
| `change_type` | string | no | `create` | One of: `create`, `update`, `append`, `delete` |
| `proposed_title` | string | no | null | Proposed title |
| `content_type` | string | create: yes | null | Canonical type (context, project, decision, intelligence, etc.). Required for `create`. Validated against type registry at submission — returns 422 if invalid. |
| `target_entry_id` | string | no | null | Existing entry ID (required for `update`/`append`) |
| `proposed_meta` | object | no | null | Proposed metadata (sensitivity, tags, domain_meta, etc.) |
| `submission_category` | string | no | `user_direct` | Submission category |
| `expected_version` | int | no | null | For update/append: optimistic concurrency check (returns 409 if stale) |

**Governance tier auto-assignment (4-tier model):**

| Condition | Tier | Meaning |
|---|---|---|
| Admin/editor via web_ui | 1 | Auto-approve (no checks) |
| Create with shared sensitivity | 1 | Auto-approve (low-risk) |
| Append / create_link | 1 | Auto-approve (additive) |
| Delete operations | 4 | Human-only (destructive) |
| System/strategic sensitivity | 3 | Batch/AI review |
| Updates on non-sensitive content | 2 | Auto-approve with inline conflict detection |
| Everything else | 2 | Auto-approve with conflict detection |

Tier 2 items run inline checks (version staleness, duplicate hash, concurrent pending edits). If all checks pass, they auto-approve like Tier 1. If any check fails, they escalate to Tier 3.

```bash
curl -s -X POST http://localhost:8010/staging \
  -H "Authorization: Bearer bkai_agnt_testkey_agent" \
  -H "Content-Type: application/json" \
  -d '{
    "target_path": "decisions/2026-04-04-new-pricing",
    "change_type": "create",
    "proposed_title": "New Pricing Model Decision",
    "proposed_content": "# Pricing Decision\n\nWe decided to adopt tiered pricing...",
    "proposed_meta": {
      "content_type": "decision",
      "sensitivity": "operational"
    },
    "submission_category": "user_direct"
  }'
```

**Response (201):**

```json
{
  "id": "s1t2u3v4-...",
  "org_id": "...",
  "target_entry_id": null,
  "target_path": "decisions/2026-04-04-new-pricing",
  "change_type": "create",
  "proposed_title": "New Pricing Model Decision",
  "proposed_content": "# Pricing Decision\n\nWe decided to adopt tiered pricing...",
  "proposed_meta": { "content_type": "decision", "sensitivity": "operational" },
  "governance_tier": 2,
  "submission_category": "user_direct",
  "status": "pending",
  "priority": 3,
  "submitted_by": "...",
  "source": "agent",
  "created_at": "2026-04-04T12:00:00Z"
}
```

---

### 11. List Staging Items

**`GET /staging`**

List items in the staging pipeline. Admins see all items; non-admins see only their own submissions (enforced by RLS).

**Query parameters:**

| Param | Type | Default | Description |
|---|---|---|---|
| `status` | string | `pending` | Filter by status: `pending`, `approved`, `rejected`, `auto_approved` |
| `target_path` | string | null | Filter by path prefix (e.g., `Projects/`) |
| `change_type` | string | null | Filter by change type: `create`, `update`, `append`, `delete` |
| `since` | string | null | ISO datetime — return items created on or after this time |

```bash
curl -s "http://localhost:8010/staging?status=pending" \
  -H "Authorization: Bearer bkai_adm1_testkey_admin"
```

**Response (200):**

```json
{
  "items": [
    {
      "id": "s1t2u3v4-...",
      "target_path": "decisions/2026-04-04-new-pricing",
      "change_type": "create",
      "proposed_title": "New Pricing Model Decision",
      "status": "pending",
      "governance_tier": 2,
      "source": "agent",
      ...
    }
  ],
  "total": 1
}
```

---

### 12. Approve Staging Item

**`POST /staging/{id}/approve`**

Admin approves a pending staging item. On approval:
- For `create`: a new entry is created in the entries table with a version record
- For `update`: the target entry is updated with a new version record
- An audit log entry is created

**Request body (optional):**

| Field | Type | Description |
|---|---|---|
| `reason` | string | Approval reason (recorded in audit log) |

```bash
curl -s -X POST http://localhost:8010/staging/s1t2u3v4-.../approve \
  -H "Authorization: Bearer bkai_adm1_testkey_admin" \
  -H "Content-Type: application/json" \
  -d '{"reason": "Content looks good, approved for publication"}'
```

**Response (200):** Updated staging item with `status: "approved"`.

**Error (403):** Only admins can approve staging items.

**Error (409):** Staging item is not in `pending` status.

---

### 13. Reject Staging Item

**`POST /staging/{id}/reject`**

Admin rejects a pending staging item with optional reason.

**Request body (optional):**

| Field | Type | Description |
|---|---|---|
| `reason` | string | Rejection reason (stored in evaluator_notes, recorded in audit log) |

```bash
curl -s -X POST http://localhost:8010/staging/s1t2u3v4-.../reject \
  -H "Authorization: Bearer bkai_adm1_testkey_admin" \
  -H "Content-Type: application/json" \
  -d '{"reason": "Needs more detail on pricing tiers"}'
```

**Response (200):** Updated staging item with `status: "rejected"`.

**Error (403):** Only admins can reject staging items.

**Error (409):** Staging item is not in `pending` status.

---

### 14. Batch Process Staging

**`POST /staging/process`**

Admin batch-evaluates all pending staging items. Runs deterministic checks:
- Type validation (content_type exists in registry)
- Duplicate detection (content_hash matches existing entry)
- Conflict detection (multiple pending items target same entry)
- Version staleness (entry modified after staging submission)

Clean items are auto-approved and promoted to entries. Duplicates are deferred. Stale items are rejected.

```bash
curl -s -X POST http://localhost:8010/staging/process \
  -H "Authorization: Bearer bkai_adm1_testkey_admin"
```

**Response (200):**

```json
{
  "total_processed": 5,
  "approved": 3,
  "flagged": 1,
  "rejected": 1,
  "details": [
    { "id": "...", "action": "approved", "reason": "Clean — auto-approved" },
    { "id": "...", "action": "flagged", "reason": "Duplicate content_hash" },
    { "id": "...", "action": "rejected", "reason": "Entry version stale" }
  ]
}
```

**Error (403):** Only admins can run batch processing.

---

### 15. Vault Import Pipeline

The import pipeline supports previewing imports (dry-run collision detection), executing imports with batch tracking, rolling back entire batches, and listing import history.

For each file: extracts title from first `# Heading` (or filename), parses YAML frontmatter as `domain_meta`, infers `content_type` from the logical path, and computes a content hash. Wiki-links (`[[Target Title]]`) in file content are detected and converted to typed `entry_links` between matching entries (including existing entries already in the database).

**Governance routing:**
- Admin/editor with interactive key: direct INSERT into entries
- Agent/commenter: all files routed to staging

---

#### 15a. Preview Import (Dry Run)

**`POST /import/preview`**

Analyze files without modifying the database. Returns collision detection, type mappings, and projected counts.

**Request body:**

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `files` | array | yes | | Array of file objects |
| `files[].filename` | string | yes | | Filename (e.g., `onboarding-guide.md`) |
| `files[].content` | string | yes | | Full file content (markdown) |
| `base_path` | string | no | `""` | Prefix for logical_path (e.g., `projects/alpha`) |

```bash
curl -s -X POST http://localhost:8010/import/preview \
  -H "Authorization: Bearer bkai_adm1_testkey_admin" \
  -H "Content-Type: application/json" \
  -d '{
    "base_path": "projects/alpha",
    "files": [
      {
        "filename": "overview.md",
        "content": "# Project Alpha Overview\n\nThis project involves [[Design Spec]] and [[Timeline]]."
      },
      {
        "filename": "design-spec.md",
        "content": "# Design Spec\n\nDetailed design for Project Alpha."
      }
    ]
  }'
```

**Response (200):**

```json
{
  "files_analyzed": 2,
  "would_create": 2,
  "would_stage": 0,
  "would_link": 2,
  "collisions": [
    {
      "filename": "design-spec.md",
      "proposed_title": "Design Spec",
      "proposed_path": "projects/alpha/design-spec",
      "existing_entry_id": "a1b2c3d4-...",
      "collision_type": "title",
      "resolution": "skip"
    }
  ],
  "type_mappings": {
    "overview.md": "project",
    "design-spec.md": "resource"
  },
  "unrecognized_types": [],
  "errors": []
}
```

**Collision types:**

| Type | Detection |
|---|---|
| `path` | An existing entry has the same `logical_path` |
| `title` | An existing entry has the same title (case-insensitive, same org) |
| `content_hash` | An existing entry has identical content |

---

#### 15b. Execute Import

**`POST /import`**

Execute the import with batch tracking. Creates an `import_batches` record and tags all created entries, staging items, and links with the batch ID. Supports collision resolution decisions from a prior preview.

**Request body:**

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `files` | array | yes | | Array of file objects |
| `files[].filename` | string | yes | | Filename (e.g., `onboarding-guide.md`) |
| `files[].content` | string | yes | | Full file content (markdown) |
| `base_path` | string | no | `""` | Prefix for logical_path (e.g., `projects/alpha`) |
| `source_vault` | string | no | `"vault"` | Name/identifier of the source vault |
| `collisions` | array | no | `[]` | Collision resolution decisions from preview |

**Collision resolution entry:**

| Field | Type | Description |
|---|---|---|
| `filename` | string | File that triggered the collision |
| `proposed_title` | string | Title extracted from the file |
| `proposed_path` | string | Computed logical_path |
| `existing_entry_id` | string | ID of the existing conflicting entry |
| `collision_type` | string | One of: `path`, `title`, `content_hash` |
| `resolution` | string | One of: `skip` (default), `rename`, `merge` |

```bash
curl -s -X POST http://localhost:8010/import \
  -H "Authorization: Bearer bkai_adm1_testkey_admin" \
  -H "Content-Type: application/json" \
  -d '{
    "base_path": "projects/alpha",
    "source_vault": "client-vault",
    "files": [
      {
        "filename": "overview.md",
        "content": "# Project Alpha Overview\n\nThis project involves [[Design Spec]] and [[Timeline]]."
      },
      {
        "filename": "design-spec.md",
        "content": "# Design Spec\n\nDetailed design for Project Alpha."
      },
      {
        "filename": "timeline.md",
        "content": "# Timeline\n\nQ2 2026: Phase 1 launch."
      }
    ],
    "collisions": [
      {
        "filename": "design-spec.md",
        "proposed_title": "Design Spec",
        "proposed_path": "projects/alpha/design-spec",
        "existing_entry_id": "a1b2c3d4-...",
        "collision_type": "title",
        "resolution": "rename"
      }
    ]
  }'
```

**Response (201):**

```json
{
  "created": 3,
  "staged": 0,
  "linked": 2,
  "errors": [],
  "batch_id": "b1c2d3e4-...",
  "collisions_resolved": 1
}
```

---

#### 15c. Rollback Import

**`DELETE /import/{batch_id}`**

Roll back an entire import batch. Archives all entries created by the batch (sets status to `archived`), removes links and pending staging items tagged with the batch ID. The batch status changes to `rolled_back`.

```bash
curl -s -X DELETE http://localhost:8010/import/b1c2d3e4-... \
  -H "Authorization: Bearer bkai_adm1_testkey_admin"
```

**Response (200):**

```json
{
  "batch_id": "b1c2d3e4-...",
  "entries_archived": 3,
  "links_removed": 2,
  "staging_removed": 0
}
```

**Error (404):** Import batch not found.

**Error (409):** Batch has already been rolled back.

---

#### 15d. List Import Batches

**`GET /import/batches`**

List import batches for the current organization.

**Query parameters:**

| Param | Type | Default | Description |
|---|---|---|---|
| `status` | string | null | Filter by status: `active`, `rolled_back` |

```bash
curl -s "http://localhost:8010/import/batches" \
  -H "Authorization: Bearer bkai_adm1_testkey_admin"
```

**Response (200):**

```json
[
  {
    "id": "b1c2d3e4-...",
    "org_id": "org_demo",
    "source_vault": "client-vault",
    "base_path": "projects/alpha",
    "status": "active",
    "file_count": 3,
    "created_count": 3,
    "staged_count": 0,
    "linked_count": 2,
    "skipped_count": 0,
    "error_count": 0,
    "created_by": "usr_admin",
    "created_at": "2026-04-06T12:00:00Z",
    "rolled_back_at": null,
    "rolled_back_by": null
  }
]
```

**Filter by status:**

```bash
curl -s "http://localhost:8010/import/batches?status=rolled_back" \
  -H "Authorization: Bearer bkai_adm1_testkey_admin"
```

---

### 16. Attachments

Upload local files as content-addressed blobs, optionally digest PDFs into staged entries, and retrieve originals via short-lived signed URLs. Blobs are deduped per-org by sha256; cross-org uploads of identical content produce distinct blob rows (tenant isolation holds even with a shared storage backend).

---

#### 16a. Upload Attachment

**`POST /attachments`**

Accepts a multipart file upload. Hashes the bytes on the fly, dedupes against the caller's org, persists via the configured `Storage` backend (local FS or S3-compatible), and returns blob metadata.

**Query parameters:**

| Param | Type | Default | Description |
|---|---|---|---|
| `digest` | bool | false | When true **and** the effective content-type is `application/pdf`, extract text via `pypdf` and create a staged entry with `submission_category='attachment_digest'`. On approval, the entry is linked back to the blob via `entry_attachments(role='source')`. |
| `content_type` | string | null | Explicit MIME override. Used when the uploading client can't set a proper multipart `Content-Type` (e.g. sends `application/octet-stream`). |

**Request body:** multipart/form-data with a single `file` part.

**Size cap:** 50 MiB per request by default; override via the `MAX_ATTACHMENT_BYTES` env var. Exceeding the cap returns **413**.

```bash
curl -s -X POST "http://localhost:8010/attachments?digest=true&content_type=application/pdf" \
  -H "Authorization: Bearer bkai_adm1_testkey_admin" \
  -F "file=@./fixture.pdf"
```

**Response (201):**

```json
{
  "blob_id": "b1c2d3e4-...",
  "sha256": "c0ffee...",
  "dedup": false,
  "size_bytes": 18421,
  "content_type": "application/pdf",
  "staging_id": "s1t2u3v4-..."
}
```

`staging_id` is present only when `digest=true` and the effective content-type was `application/pdf`. Uploading identical bytes to the same org a second time returns the original `blob_id` with `dedup: true`. Uploading the same bytes to a different org produces a new `blob_id` (no cross-org dedup).

---

#### 16b. Get Attachment

**`GET /attachments/{blob_id}`**

Returns a **302** redirect to a time-limited signed URL (5-minute TTL) for the underlying blob. Authorization is enforced via entry RLS: the caller must have read access to at least one entry that references the blob through `entry_attachments`. Callers without visibility receive **404** (not 403) to avoid leaking blob existence.

```bash
curl -sI "http://localhost:8010/attachments/b1c2d3e4-..." \
  -H "Authorization: Bearer bkai_adm1_testkey_admin"
```

**Response (302):** `Location: <signed URL>` — follow to fetch the bytes. Local-storage signed URLs point to `GET /attachments/_local/{key}?exp=...&sig=...` on the same host; S3-backed deployments return presigned S3 URLs.

**Response (404):** Blob does not exist, or the caller has no read access to any entry referencing it.

---

#### 16c. List Entry Attachments

**`GET /entries/{id}/attachments`**

List blobs attached to an entry, ordered by `created_at`. RLS filters out attachments on entries the caller cannot see.

```bash
curl -s "http://localhost:8010/entries/e5f6g7h8-.../attachments" \
  -H "Authorization: Bearer bkai_adm1_testkey_admin"
```

**Response (200):**

```json
{
  "entry_id": "e5f6g7h8-...",
  "attachments": [
    {
      "blob_id": "b1c2d3e4-...",
      "sha256": "c0ffee...",
      "content_type": "application/pdf",
      "size_bytes": 18421,
      "role": "source",
      "created_at": "2026-04-16T12:00:00Z"
    }
  ]
}
```

---

#### 16d. MCP Tool: `upload_attachment`

Exposed to co-work sessions via MCP. Reads a local file path, derives content-type from the extension when not supplied, and posts to `POST /attachments`.

**Signature:** `upload_attachment(path: str, digest: bool = True, content_type: str | None = None) -> dict`

- `path` — absolute path readable by the MCP server process. For Docker-hosted MCP, the file must live on a bind-mounted volume.
- `digest` — defaults to `True`; triggers the PDF digest pipeline when the effective content-type is `application/pdf`.
- `content_type` — explicit override; when omitted, derived via `mimetypes.guess_type(path)`, falling back to `application/octet-stream`.

Returns the `POST /attachments` response verbatim (see 16a). Typical end-to-end flow: call `upload_attachment("/path/to.pdf")` → inspect `staging_id` → confirm via `list_staging` → approve via `review_staging` (or let batch processing promote it).

---

## Valid Enumeration Values

**Content types:** `context`, `project`, `meeting`, `decision`, `intelligence`, `daily`, `resource`, `department`, `team`, `system`, `onboarding`

**Sensitivity levels:** `system`, `strategic`, `operational`, `private`, `project`, `meeting`, `shared`

**Link types:** `relates_to`, `supersedes`, `contradicts`, `depends_on`, `part_of`, `tagged_with`

**Staging statuses:** `pending`, `approved`, `auto_approved`, `rejected`, `deferred`, `superseded`, `merged`

**Staging change types:** `create`, `update`, `append`, `delete`, `create_link`

**Governance tiers:** 1 (auto-approve), 2 (auto-approve with conflict detection), 3 (batch/AI review), 4 (human-only)

**User roles:** `admin`, `editor`, `commenter`, `viewer`

---

## Auth & User Management Endpoints

### Login

**`POST /auth/login`** (no auth required)

Authenticate with email + password. Returns key_prefix + user info.

**Request body:**

| Field | Type | Required | Description |
|---|---|---|---|
| `email` | string | yes | User email (case-insensitive) |
| `password` | string | yes | User password |

**Response (200):**

```json
{
  "api_key": "bkai_adm1",
  "user": {
    "id": "usr_admin",
    "org_id": "org_demo",
    "display_name": "Alice Admin",
    "email": "alice@demo.org",
    "role": "admin",
    "department": "leadership",
    "is_active": true
  }
}
```

**Note:** Returns key_prefix (not full key) since only bcrypt hashes are stored. The full key was shown once at invite redemption.

### List Org Members

**`GET /org/members`** (admin only)

Returns all users in the caller's organization.

### Change User Role

**`PATCH /users/{user_id}/role`** (admin only)

**Request body:** `{ "role": "editor" }`

Cannot change your own role (returns 400).

### Deactivate User

**`PATCH /users/{user_id}/deactivate`** (admin only)

Sets `is_active=false`. Cannot deactivate yourself (returns 400).

### Remove User

**`DELETE /users/{user_id}`** (admin only)

Deactivates user + revokes all API keys. Cannot remove yourself (returns 400).

---

## Addenda (post-2026-04-04)

The endpoints below were added or refined after this reference's initial snapshot. API-only — none are exposed through MCP.

### Authentication

**`POST /auth/login`**

Exchange email + password for an API key.

- Request: `{ "email": "...", "password": "..." }`
- Response: `{ "api_key": "...", "user": { id, email, role, source, ... } }`
- Note: response shape is `{api_key, user}` — **not** `{access_token}`. Use `api_key` directly as the bearer token; there is no JWT layer.

### Invitations

**`POST /invitations`** (admin only)

Create a new invite.

- Response field is `token` (not `invite_token`): `{ "code": "CTX-XXXX-XXXX", "token": "...", "expires_at": "..." }`

**`POST /invitations/redeem`**

Redeem an invite code.

- Request: `{ "code": "CTX-XXXX-XXXX", "token": "...", "email": "...", "display_name": "..." }`
- Response: `{ "api_key": "...", "user": {...} }`
- Single-use on *attempt* — a failed redemption permanently invalidates the invite.

### Comments

Comments are API-only. There is no MCP tool for commenting.

**`GET /entries/{id}/comments`**

List top-level comments on an entry. Threaded replies returned separately via `/comments/{id}/replies`.

**`POST /entries/{id}/comments`**

Create a top-level comment. Body: `{ "body": "markdown", "parent_id": null }`. For a reply, set `parent_id` to the parent comment id.

**`GET /comments/{id}/replies`**

Return direct replies to a comment (one level; fetch recursively for deeper threads).

**`PATCH /comments/{id}`**

Edit or resolve a comment. Body: `{ "body": "...", "resolved": true|false }`. Editors can edit their own; admins can edit any.

### Groups

**`GET /groups`** / **`POST /groups`**

List or create a group. Create body: `{ "name": "...", "description": "..." }`.

**`GET /groups/{id}`** / **`PATCH /groups/{id}`** / **`DELETE /groups/{id}`**

Read, rename/update, or delete a group. Delete cascades to group memberships and removes the group's permission grants.

**`GET /groups/{id}/members`** / **`POST /groups/{id}/members`** / **`DELETE /groups/{id}/members/{user_id}`**

List, add, or remove a group member. Group-scoped permissions (see [Permissions v2](#permissions)) propagate to members automatically.

### Bulk Graph

**`GET /graph`**

Bulk-export the full graph visible to the caller as a nodes + edges payload for the frontend `/graph` view.

- Response: `{ "nodes": [ { id, title, logical_path, content_type, ... } ], "edges": [ { source, target, link_type } ] }`
- Deduplicates nodes and edges; RLS-filtered to the caller's scope.
- Cached for 45 seconds per user.

### Rendered Content

**`GET /entries/{id}`** — rendering note (spec 0028)

The response's `content` field is now rendered, not raw:

- Frontmatter (`---\n…\n---` block, with or without a surrounding ```` ```yaml ```` fence) is stripped.
- `[[wiki-links]]` are resolved to markdown links of the form `[Label](/kb/<uuid>)`. Unresolved wiki-links pass through literally.
- The unrendered body is still available via the raw version endpoints; routine reads should use the rendered form.

---

## MCP Tools — Analytics

### get_usage_stats

Usage analytics rollups for admins. Wraps the three `GET /analytics/*` endpoints (added in spec 0034c) into a single MCP tool.

**Admin-only.** Non-admin callers receive a structured dict `{"error": "admin-only", "detail": ...}` — never a raised exception and never a raw 500.

**Parameters:**

| Param | Type | Default | Description |
|---|---|---|---|
| `kind` | string | `"top-entries"` | One of: `top-entries`, `top-endpoints`, `session-depth`, `summary` |
| `since` | string | `"24h"` | Window size. One of: `1h`, `24h`, `7d`, `30d` |
| `actor_type` | string | null | Optional filter for `top-entries`: `user`, `agent`, or `api` |
| `actor_id` | string | null | Required for `session-depth` to scope the breakdown to a single actor |
| `limit` | int | 20 | Page size for `top-entries` and `top-endpoints` |

**Kinds:**

- **`top-entries`** — Proxies `GET /analytics/top-entries`. Returns `{ "items": [{"entry_id", "title", "reads"}], "limit", "offset", "since" }`.
- **`top-endpoints`** — Proxies `GET /analytics/top-endpoints`. Returns `{ "items": [{"endpoint", "count", "avg_duration_ms", "p95_duration_ms"}], "limit", "offset", "since" }`.
- **`session-depth`** — Proxies `GET /analytics/session-depth`. Returns `{ "actor_id", "windows": [{"window_start", "requests", "entries_touched", "duration_s"}], "since" }`.
- **`summary`** — Fans out all three calls concurrently via `asyncio.gather` and returns `{ "top_entries": {...}, "top_endpoints": {...}, "session_depth": {...} }`. If any sub-call returns 403, the whole envelope collapses to `{"error": "admin-only", ...}`.

**Error contract:**

| Caller | Response |
|---|---|
| Admin | Rollup JSON as documented per kind |
| Non-admin (viewer/editor/commenter/agent) | `{"error": "admin-only", "detail": ...}` |
| Unknown `kind` value | `{"error": "invalid-kind", "detail": "unknown kind ..."}` |

**Example — summary for the last week:**

```python
await get_usage_stats(kind="summary", since="7d", limit=10)
```

Returns the same structured envelope as three separate calls, in one round-trip.
