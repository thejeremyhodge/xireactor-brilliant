-- 026_pg_trgm.sql
-- Enable trigram similarity search for the fuzzy-fallback path on entry
-- list/search endpoints (spec 0037, T-0208, issue #26).
--
-- Behaviour: the API runs its existing FTS query first and only re-issues
-- against these indexes when the caller passes `fuzzy=true` AND FTS returned
-- zero rows. We therefore index `title` and `content` as separate GIN
-- trigram indexes rather than a concatenated expression — it lets us
-- similarity-order by title (which is what users typically typo) and
-- fall back to content only if needed. Both indexes sit on the same
-- table that RLS policies already protect, so no new grants are required.
--
-- Idempotent: `CREATE EXTENSION IF NOT EXISTS` and `CREATE INDEX IF NOT
-- EXISTS` are both no-ops on re-apply.
--
-- Depends on: 001_core.sql (defines entries.title / entries.content)
-- Paired with: api/routes/entries.py (`fuzzy=true` query param),
--              mcp/tools.py (`fuzzy` kwarg on search_entries).

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- GIN trigram indexes. `gin_trgm_ops` supports both `%` (similarity) and
-- `<%` (word_similarity) operators; the API layer uses `<%` for the
-- fuzzy fallback because a short query ("klaude") matched against a
-- multi-word haystack ("Working with claude test") scores way higher
-- with word_similarity (0.57) than whole-text similarity (0.15), and
-- is the right semantic for "did the user typo one word in a longer
-- title/body".
CREATE INDEX IF NOT EXISTS idx_entries_title_trgm
    ON entries USING GIN (title gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_entries_content_trgm
    ON entries USING GIN (content gin_trgm_ops);
