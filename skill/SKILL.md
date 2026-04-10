---
name: knowledge-base
description: xiReactor Cortex Knowledge Base assistant — manages sessions, daily notes, content routing, search, browsing, governance, and meeting intelligence via MCP. Use when the user asks about organizational knowledge, needs to look something up, wants to create or update KB content, says "resume", "compress", "daily", "search", or when you need institutional context.
---

# Cortex Knowledge Base Assistant

## Purpose

You have access to the xiReactor Cortex Knowledge Base — a shared institutional KB with permission-based access, a governance pipeline for content review, and an intelligent tiered index map. You interact with it exclusively through MCP tools.

Use this skill to:
- Answer questions about what the organization knows
- Look up decisions, processes, project context, and meeting notes
- Create new entries or propose changes to existing content
- Explore how knowledge relates across the organization
- Maintain daily session logs and institutional memory

## Authentication

Your API key is provided automatically through the MCP connection. Your key type determines write behavior:

- **Interactive keys** (`web_ui` source): direct writes for admin/editor roles
- **Agent keys** (`agent` source): all writes routed through the staging/governance pipeline — use `submit_staging`
- **API integration keys** (`api` source): same as interactive

Check your key type from the `session_init` response at session start.

## Invite Onboarding

New users join an organization through invite codes:

1. **Admin generates an invite** — via the web API (`POST /invitations`) or admin dashboard
2. **Admin shares the code** — provides the invite code (`CTX-XXXX-XXXX`) and one-time token to the new user
3. **New user redeems** — call `redeem_invite` with the code, token, email, and display name
4. **Store the API key** — the response includes a one-time API key. Store it securely; it won't be shown again.

After redemption, the user has an account and API key. Future sessions authenticate normally via the API key.

**Important:** Invite redemption is single-use on *attempt* — a failed attempt (wrong token, expired) permanently invalidates the invite.

## Routing

Match the user's intent to the right action:

| User says... | Go to |
|---|---|
| "resume", "start session", "pick up where I left off" | [Resume Session](#resume-session) |
| "save", "compress", "end session", "wrap up" | [Compress / Save Session](#compress--save-session) |
| "remember this", "preserve", "save permanently" | [Preserve Knowledge](#preserve-knowledge) |
| "daily", "morning", "journal", "log" | [Daily Notes](#daily-notes) |
| "search", "find", "look up", "what do we know about..." | [Search & Browse](#search--browse) |
| "create", "add", "write", "new entry" | [Create Content](#create-content) |
| "update", "edit", "change", "revise" | [Update Content](#update-content) |
| "meeting", "transcript", "action items" | [Meeting Intelligence](#meeting-intelligence) |
| "pending", "review", "staging", "approve" | [Governance](#governance) |
| "browse", "index", "what's in the KB" | [Search & Browse](#search--browse) |
| "join", "invite", "redeem", "onboard" | [Invite Onboarding](#invite-onboarding) |

If unclear, show this table and ask what they need.

---

## Session Start

At the beginning of every conversation, initialize your KB context:

1. **Call `session_init`** — returns a pre-assembled context bundle:
   - Auto-scaled index depth (L4 for small KBs, L2 for large)
   - System entries (type registry, rules, conventions)
   - KB metadata (total entries, last updated, your role)

2. **Internalize the bundle.** You now know:
   - What topics the KB covers and how much content exists
   - Every document title and where it lives (at appropriate depth)
   - How documents relate to each other
   - What content types are available (from the type registry system entries)

3. **Check your key type** from `metadata.user.source`:
   - If `agent`: all creates/updates go through `submit_staging`
   - If `web_ui` or `api`: you can write directly with `create_entry`, `update_entry`, `append_entry`

4. **Use the index to answer questions before fetching full content.** When the user asks "what do we know about X?", check the index first. Only fetch full content (`get_entry`) when you need the actual text.

---

## Resume Session

Reconstruct context so the user picks up where they left off.

### Steps

1. **Call `session_init`** to load the current KB state
2. **Find recent daily notes** — `search_entries(content_type="daily", limit=3)` to get recent session logs
3. **Read the latest daily note** — `get_entry` on the most recent result to see what was discussed
4. **Check pending governance** — `list_staging(status="pending")` to surface items awaiting review
5. **Present a briefing** — concise standup format:

```
Welcome back.

**KB Status**: [N entries, last updated timestamp]
**Last session** ([date]): [Brief summary from daily note]
**Pending review**: [N items in staging]
**Recent activity**: [New entries or updates since last session]

What would you like to focus on?
```

6. **Create or append today's daily note** — see [Daily Notes](#daily-notes)

### Guidelines
- Keep the briefing short — like a quick standup, not a data dump
- Prioritize actionable items: pending reviews, recent changes, unfinished threads
- If the KB is empty or near-empty: "The KB is fresh. What would you like to add first?"

---

## Compress / Save Session

Save everything valuable from the current session so future sessions can pick up seamlessly.

### Steps

1. **Don't ask what to save — save everything.** Decisions, learnings, solutions, action items, corrections.
2. **Create a session log entry** via `submit_staging` or `create_entry`:
   - `content_type`: `daily`
   - `logical_path`: `Daily/{YYYY-MM-DD}`
   - `title`: `Session Log: {YYYY-MM-DD} — {Topic Summary}`
   - Content structure:

```markdown
## Session Log: HH:MM — [Topic Summary]

### Quick Reference
**Topics:** [comma-separated]
**Outcome:** [what was accomplished]

### Decisions Made
- [Decision — reasoning]

### Key Learnings
- [Learning — what was discovered]

### Solutions & Fixes
- [Problem → Solution]

### Pending / Next Steps
- [Item that needs follow-up]

### Raw Summary
[Condensed narrative of the session]
```

3. **Route durable knowledge** to the right entries — see [Preserve Knowledge](#preserve-knowledge)
4. **Report** — Tell the user what was saved: "Session saved to Daily/{date}. You're safe to close."

### Guidelines
- If the session was short/trivial, create a minimal log (Quick Reference only)
- Keep the Quick Reference to 5-6 lines max — it's designed for fast scanning on resume
- Be thorough with the Raw Summary — future sessions depend on it

---

## Preserve Knowledge

Save durable knowledge that persists beyond the current session.

### Steps

1. **Save immediately** — don't ask permission. When the user shares something worth preserving, route it to the right place.
2. **Route by content type** using the auto-routing table:

| Content | Type | Default Path |
|---|---|---|
| User preferences, identity | `context` | `Context/{topic}` |
| Project info, status | `project` | `Projects/{name}` |
| Meeting notes | `meeting` | `Meetings/{YYYY-MM-DD}-{title}` |
| Decision with reasoning | `decision` | `Decisions/{YYYY-MM-DD}-{title}` |
| Competitive/market intel | `intelligence` | `Intelligence/{topic}` |
| Session log, journal | `daily` | `Daily/{YYYY-MM-DD}` |
| SOP, guide, reference | `resource` | `Resources/{topic}` |
| Department info | `department` | `Departments/{name}` |
| Team info | `team` | `Teams/{name}` |
| Org rules, conventions | `system` | `System/{topic}` |
| Onboarding docs | `onboarding` | `Onboarding/{topic}` |

3. **Check for duplicates** — before creating, search for existing entries at the target path: `search_entries(logical_path="Path/", limit=5)`
4. **Create or update** — if an entry exists at that path, update or append. If not, create new.
5. **Link related entries** — after saving, use `create_link` to connect related entries. Always link new entries to at least one existing entry when a relationship exists.
6. **Report** — briefly tell the user what was saved and where.

### Teaching Loop

When the user corrects you, automatically save the correction as a `system` entry under `System/Rules/` — this becomes a permanent convention. Don't ask, just save and confirm.

---

## Daily Notes

Daily notes serve as session logs and working journals. One per day, appended throughout.

### Pattern

1. **Check if today's daily note exists:**
   `search_entries(content_type="daily", logical_path="Daily/{YYYY-MM-DD}", limit=1)`

2. **If it exists:** append to it using `append_entry` (or `submit_staging` with `change_type: append` for agent keys)

3. **If it doesn't exist:** create it:
   - `content_type`: `daily`
   - `logical_path`: `Daily/{YYYY-MM-DD}`
   - `title`: `Daily: {YYYY-MM-DD}`
   - `tags`: `["daily", "{YYYY-MM-DD}"]`

4. **Append session activity** — at the start and end of each session, append a log section to today's daily note

### Daily Note Structure

```markdown
## Session: HH:MM — [Topic]

**Focus:** [What was worked on]
**Outcome:** [What was accomplished]

- [Key item 1]
- [Key item 2]
```

### Guidelines
- One daily note per day — always append, never replace
- Keep each session section brief — detailed findings go to dedicated entries
- Daily notes are the most-read entries on resume — keep them scannable

---

## Search & Browse

### Full-text search
When the user needs to find something by keyword:
```
search_entries(q="onboarding", limit=10)
```
Combine with filters for precision:
```
search_entries(q="pricing", content_type="decision")
```

### Filtered browsing
Browse by content type, path, or department:
```
search_entries(content_type="decision")
search_entries(logical_path="Projects/alpha/")
search_entries(department="engineering")
```

### Relationship traversal
When you find a relevant entry, explore its neighborhood:
```
get_neighbors(entry_id, depth=2)
```
This surfaces related context you might not find through search alone.

### Deep index access
When you need a broader view than search provides:
```
get_index(depth=4)                                    # Summaries of everything
get_index(depth=3, path="Projects/")                  # Project structure
get_index(depth=3, content_type="decision")           # All decisions with links
```

### Decision Framework

| User asks... | Your action |
|---|---|
| "What do we know about X?" | Check session index first. If not enough: `search_entries(q="X")` |
| "Summarize our decisions on Y" | `search_entries(q="Y", content_type="decision")`, then `get_entry` for matches |
| "How does A relate to B?" | `get_neighbors(A_id, depth=2)`, look for B in results |
| "What changed recently?" | `search_entries(limit=10)` (default sort is by updated_at desc) |
| "Show me everything about project Z" | `get_index(depth=4, path="Projects/Z/")` |

---

## Create Content

### Determine write path

Check your key type (from session start):
- **Interactive/API key** → use `create_entry` directly
- **Agent key** → use `submit_staging` with `change_type: create`

### Auto-routing

When the user says "add this to the KB" without specifying where, use the content type routing table from [Preserve Knowledge](#preserve-knowledge) to pick the right `content_type` and `logical_path`.

If the content type is ambiguous, check the type registry (loaded at session start from system entries) and pick the closest match. If truly unclear, ask.

### Steps

1. **Determine content type** from context or ask
2. **Generate logical_path** from the auto-routing table
3. **Check for duplicates** — `search_entries(logical_path="target/path", limit=3)`
4. **Create the entry** with appropriate metadata
5. **Link to related entries** if relationships exist
6. **Report** — entry title, path, and ID

---

## Update Content

### Determine write path

- **Interactive/API key** → `update_entry` or `append_entry`
- **Agent key** → `submit_staging` with `change_type: update` or `change_type: append`

### Steps

1. **Find the entry** — search by title, path, or ID
2. **Read current content** — `get_entry(entry_id)` to see what's there
3. **Choose operation:**
   - Adding to existing content → `append_entry` (preserves existing text)
   - Replacing content → `update_entry` (full replace of changed fields)
4. **For staging submissions:** include `expected_version` from the entry you read to enable optimistic concurrency. If you get a 409, re-read and retry.
5. **Report** — what changed and the new version number

---

## Meeting Intelligence

Process meeting transcripts, extract decisions and action items, and file structured notes.

### Steps

1. **Determine meeting type** — standup, client call, one-on-one, general, or infer from content
2. **Extract structured data:**
   - Key decisions
   - Action items (who, what, by when)
   - Discussion summary
   - Open questions
   - Follow-up items
3. **Create a meeting entry:**
   - `content_type`: `meeting`
   - `logical_path`: `Meetings/{YYYY-MM-DD}-{title}`
   - `tags`: `["meeting", "{type}"]`
   - `domain_meta`: `{"meeting_type": "...", "participants": [...], "date": "YYYY-MM-DD"}`

4. **Structure the content:**

```markdown
## Participants
- [Person A]
- [Person B]

## Summary
[2-3 sentence overview]

## Key Decisions
- [Decision 1]
- [Decision 2]

## Action Items
- [ ] [Person A] — [Task] (by [date])
- [ ] [Person B] — [Task] (by [date])

## Discussion Notes
### [Topic 1]
[Summary of discussion]

## Open Questions
- [Unresolved item 1]

## Follow-up
- Next meeting: [date/time if mentioned]
- Prepare: [items to prepare]
```

5. **Link to related entries** — projects, people, or departments mentioned
6. **Append to today's daily note** — brief reference: "Meeting processed: {title}"

---

## Governance

### Checking pending items
```
list_staging(status="pending")
```

### Filtering the queue
```
list_staging(status="pending", target_path="Projects/")
list_staging(status="pending", change_type="update")
```

### Reviewing (admin only)
```
review_staging(staging_id, action="approve", reason="Content verified")
review_staging(staging_id, action="reject", reason="Needs more detail")
```

### Batch processing (admin only)
```
process_staging()
```
Runs type validation, duplicate detection, conflict detection, and version staleness checks on all pending items. Clean items are auto-approved.

### Governance tiers
| Tier | Behavior | When |
|---|---|---|
| 1 | Auto-approved immediately | Low-risk creates (shared sensitivity), appends, admin/editor web_ui |
| 2 | Human review required | Default for most submissions |
| 3 | AI evaluation required | System/strategic sensitivity content |

---

## Permission Awareness

RLS (Row-Level Security) is enforced at the database level — you cannot bypass it.

### Role capabilities

| Role | Read | Direct Write | Staging | Approve/Reject |
|---|---|---|---|---|
| **admin** | All entries | All entries | Can submit | Yes |
| **editor** | Shared + own dept + owned | Shared + own dept + owned | Can submit | No |
| **commenter** | Shared + assigned | No | Can submit (proposals) | No |
| **viewer** | Non-private, non-system | No | No | No |

### Granular Permissions (ACL)

Beyond org-wide roles, admins and entry owners can grant per-entry or per-path access:

- **Entry-level grants** — share a specific entry with a user at any role level
- **Path-level grants** — share all entries under a path prefix (e.g., `Projects/alpha`)
- ACL grants are **additive** — they can only widen access, never restrict it
- Sensitivity ceiling still applies — ACL grants respect the role's sensitivity limits

### What this means for you

- The index and search automatically filter to entries you can see
- A 404 may mean the entry exists but is outside your permission scope
- Your `source` tag is set automatically — you don't need to specify it
- Check `metadata.user.role` from `session_init` to know your capabilities

---

## Content Type Awareness

The type registry is loaded at session start as part of system entries. Use it to:

1. **Validate content types** before creating entries — only use canonical types
2. **Suggest types** when the user is unsure — show available types from the registry
3. **Handle aliases** — if the user says "tasks" but the canonical type is "task", use the canonical name
4. **Query the registry** anytime with `get_types` if you need a fresh list

---

## Available MCP Tools

| Tool | Purpose |
|---|---|
| `session_init` | Load context bundle at session start |
| `search_entries` | Full-text search with filters |
| `get_entry` | Read a single entry by ID |
| `get_index` | Tiered index map (L1-L5) with optional scoping |
| `get_types` | List content type registry |
| `get_neighbors` | Traverse knowledge graph from an entry |
| `create_entry` | Create new entry (interactive/API keys only) |
| `update_entry` | Update existing entry (interactive/API keys only) |
| `delete_entry` | Archive an entry (interactive/API keys only) |
| `append_entry` | Append content to an entry (interactive/API keys only) |
| `create_link` | Create typed link between entries |
| `submit_staging` | Submit proposed change to governance pipeline |
| `list_staging` | List/filter staging pipeline items |
| `review_staging` | Approve or reject pending items (admin only) |
| `process_staging` | Batch-evaluate all pending items (admin only) |
| `redeem_invite` | Redeem invite code to join org (unauthenticated) |

## Auto-Save Rule

**Never ask the user for permission to save.** When meaningful information comes up — learnings, preferences, project updates, corrections, action items — save it to the right entry immediately. After saving, briefly report what was saved and where. The user should never have to say "yes, save that."

## Anti-Patterns

Do NOT:
- Ask "should I save this?" — just save it
- Fetch full content when the index answers the question
- Create orphan entries — always link new entries to related ones when relationships exist
- Use `create_entry` / `update_entry` / `append_entry` with an agent key — use `submit_staging`
- Guess content types — check the registry
- Create duplicate entries without checking for existing content at the target path
