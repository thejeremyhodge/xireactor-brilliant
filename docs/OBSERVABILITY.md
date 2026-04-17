# Observability

Brilliant records two kinds of telemetry so admins can answer "what is the team actually doing with the KB?" — per-request performance and per-entry reads. Both live in Postgres, both are admin-only by RLS, and both are queryable via the API and the `get_usage_stats` MCP tool.

## Overview

| Captured | Not captured |
|---|---|
| HTTP method, path template (`/entries/{entry_id}`, not raw IDs), status, duration, response size | Request bodies |
| Which `entry_id` each read surfaced | Raw query strings beyond the pre-parsed template |
| Authenticated `actor_id` (user ID, not email) and `org_id` | IP addresses |
| Best-effort `approx_tokens = response_bytes // 4` | Full request/response payloads |

Middleware is fire-and-forget: the request returns first, the log row is inserted from a background task. A logging failure is caught and logged as a stdlib warning — it cannot surface to the client.

## Tables

Both tables are append-only (no UPDATE/DELETE policies). Introduced in `db/migrations/023_access_log.sql`.

### `entry_access_log`

One row per (actor, entry) surfaced in a read response. Multi-entry responses (list, graph, neighbors) use a single batched INSERT per request.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGSERIAL` | |
| `org_id` | `TEXT` | RLS scope |
| `actor_type` | `TEXT` | `user`, `agent`, `api` |
| `actor_id` | `TEXT` | e.g. `usr_admin` |
| `entry_id` | `UUID` | the entry that was read |
| `source` | `TEXT` | `web_ui`, `agent`, `api` |
| `ts` | `TIMESTAMPTZ` | default `now()` |

Indexes: `(org_id, ts DESC)`, `(org_id, entry_id, ts DESC)`.

### `request_log`

One row per HTTP request, excluding `/health` and `/static/*`.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGSERIAL` | |
| `org_id` | `TEXT` (nullable) | `NULL` for unauthenticated requests |
| `actor_id` | `TEXT` (nullable) | `NULL` for unauthenticated requests |
| `endpoint` | `TEXT` | path template, truncated to ≤256 chars |
| `method` | `TEXT` | |
| `status` | `INTEGER` | |
| `response_bytes` | `INTEGER` | from `Content-Length` when known |
| `approx_tokens` | `INTEGER` | `response_bytes // 4` when known |
| `duration_ms` | `INTEGER` | full stack (CORS + auth + handler) |
| `ts` | `TIMESTAMPTZ` | default `now()` |

Indexes: `(org_id, ts DESC)`, `(org_id, endpoint, ts DESC)`.

## RLS

Both tables `ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY`. The only SELECT policy is `TO kb_admin USING (org_id = current_setting('app.org_id'))`. Non-admin roles have SELECT granted at the table level but no policy — FORCE RLS returns zero rows (rather than erroring) when a viewer/editor/commenter/agent tries to read.

INSERT is open to all kb roles with `WITH CHECK (org_id IS NULL OR org_id = current_setting('app.org_id'))`. This lets an unauthenticated request still be logged (e.g. a 401 on a bad token), and keeps tenant isolation intact for everything else.

## Privacy / PII

- **Logged:** `actor_id` (the user ID string, not email). `org_id`. Path template. Status. Duration. Response size.
- **Not logged:** IP addresses. Request bodies. Response bodies. Email addresses. Raw query strings beyond the parameter parsing that produced the path template.
- **Per-tenant config:** not implemented yet. All tenants log the same fields. An `organizations.settings` flag to suppress actor_id capture is an obvious next step.
- **`approx_tokens`:** a rough `bytes // 4` estimate. Not a replacement for a real tokenizer; useful as an order-of-magnitude proxy for cost attribution.

## Retention

No automatic retention is applied. Tables grow monotonically until an operator prunes them. A reasonable default would be **90 days** for both tables.

Manual retention as a one-liner from the admin DSN:

```sql
DELETE FROM request_log WHERE ts < now() - interval '90 days';
DELETE FROM entry_access_log WHERE ts < now() - interval '90 days';
```

Or schedule via `pg_cron` / external cron. A first-class retention toggle is on the roadmap.

## Rollup endpoints

All endpoints are admin-only (non-admin callers get a `403` before the DB is queried). All are org-scoped via RLS. `since` accepts `1h`, `24h`, `7d`, `30d`.

### `GET /analytics/top-entries?actor_type=&since=24h&limit=20&offset=0`

```json
{
  "items": [
    { "entry_id": "a0000000-...", "title": "Mission Statement", "reads": 17 }
  ],
  "limit": 20,
  "offset": 0,
  "since": "24h"
}
```

### `GET /analytics/top-endpoints?since=24h&limit=20&offset=0`

```json
{
  "items": [
    {
      "endpoint": "/entries",
      "count": 27,
      "avg_duration_ms": 16.0,
      "p95_duration_ms": 34.0
    }
  ],
  "limit": 20,
  "offset": 0,
  "since": "24h"
}
```

p95 is computed via `percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)`.

### `GET /analytics/session-depth?actor_id=usr_admin&since=24h`

```json
{
  "actor_id": "usr_admin",
  "since": "24h",
  "windows": [
    { "window_start": "2026-04-16T17:45:00Z", "requests": 18, "entries_touched": 4, "duration_s": 246 }
  ]
}
```

Bucketing is 15-minute windows (`date_trunc('minute', ts) - (EXTRACT(MINUTE FROM ts)::int % 15) * interval '1 minute'`). `request_log` and `entry_access_log` are joined with `FULL OUTER JOIN` so a window shows up even if it has only requests or only entry reads.

## MCP tool

`get_usage_stats` wraps the three rollup endpoints for Claude Co-work / Claude Code.

```
get_usage_stats(
  kind: "top-entries" | "top-endpoints" | "session-depth" | "summary" = "top-entries",
  since: "1h" | "24h" | "7d" | "30d" = "24h",
  actor_type: str | None = None,   # for top-entries
  actor_id: str | None = None,     # required for session-depth
  limit: int = 20,
)
```

- `kind="summary"` fans out all three rollup calls concurrently (`asyncio.gather`) and returns `{"top_entries": ..., "top_endpoints": ..., "session_depth": ...}`.
- Non-admin callers receive `{"error": "admin-only", "detail": ...}` — never a raised exception, never a 500.
- Full reference: `skill/references/api-reference.md`.

## Overhead

The middleware targets **<2ms p95** under load. The insert runs in an `asyncio.create_task` so it does not block the response. The background coroutine opens a fresh pooled connection, executes `SET LOCAL ROLE kb_admin; SET LOCAL app.org_id = '...'; INSERT ...;` in one short transaction, and exits.

Formal load measurements are owed to the concurrency harness in a follow-up; the smoke-test numbers on a local MacBook (9–27 ms end-to-end request latency for `/entries` with logging on) left plenty of headroom.

## Test coverage

`tests/test_observability.py` ships 11 integration cases covering: middleware behavior, `/health` exclusion, single vs. batched inserts, graph cache interaction, viewer 403 on analytics, rollup response shapes, `since` parser, `actor_type` validation, and RLS enforcement at the psql layer. Runs against the live stack (`BASE_URL`, `DB_DSN` env-overridable).
