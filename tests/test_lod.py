"""Tests for the multi-LOD endpoint (Sprint 0049, T-0285).

Covers:
  * LOD0 corpus (structural + heat) — top-level shape contract.
  * LOD1 community-by-tag and community-by-path — happy path.
  * LOD2 community silhouette — fixed-shape card.
  * LOD4 node silhouette — happy path on a freshly-created entry.
  * LOD6 section outline — fenced-code-block exclusion.
  * Grammar validation — invalid axis, level/scope mismatches → 400.
  * RLS scoping — agent (User B) cannot LOD4 a private admin entry.
  * Heat-banding — admin inserts known `entry_access_log` rows and
    asserts the bands populate.

Prerequisites:
  1. docker compose up -d  (API on :8010, Postgres on :5442)
  2. pip install -r tests/requirements-dev.txt

Run:
  pytest tests/test_lod.py -v
"""

from __future__ import annotations

import json
import os
import uuid

import pytest
import requests

try:
    import psycopg
    _PSYCOPG_AVAILABLE = True
except ImportError:
    _PSYCOPG_AVAILABLE = False


BASE_URL = os.environ.get("BRILLIANT_BASE_URL", "http://localhost:8010")
DB_DSN = os.environ.get(
    "BRILLIANT_DB_DSN",
    "postgresql://postgres:dev@localhost:5442/brilliant",
)
ADMIN_KEY = "bkai_adm1_testkey_admin"
# Per `tests/demo_e2e.sh` — agent ceiling is `shared`-only, so an
# admin-owned `sensitivity='private'` entry is invisible to the agent.
AGENT_KEY = "bkai_agnt_testkey_agent"
REQUEST_TIMEOUT = 10.0


def _headers(key: str = ADMIN_KEY) -> dict:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _api_available() -> bool:
    try:
        return requests.get(f"{BASE_URL}/health", timeout=2.0).status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _api_available(),
    reason=f"Brilliant API not reachable at {BASE_URL} (start `docker compose up -d`).",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_lod(*, axis: str, scope: str = "corpus", level: int = 0,
             key: str = ADMIN_KEY, expect_status: int = 200) -> dict:
    r = requests.get(
        f"{BASE_URL}/lod",
        params={"axis": axis, "scope": scope, "level": level},
        headers=_headers(key),
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == expect_status, (
        f"GET /lod axis={axis} scope={scope} level={level}: "
        f"expected {expect_status}, got {r.status_code}: {r.text}"
    )
    return r.json() if r.headers.get("content-type", "").startswith("application/json") else {}


def _create_entry(
    *,
    key: str = ADMIN_KEY,
    title: str | None = None,
    content: str = "# placeholder\n",
    logical_path: str | None = None,
    sensitivity: str = "shared",
    tags: list[str] | None = None,
) -> dict:
    suffix = uuid.uuid4().hex[:10]
    body = {
        "title": title or f"lod-test-{suffix}",
        "content": content,
        "content_type": "context",
        "logical_path": logical_path or f"Tests/lod/{suffix}",
        "sensitivity": sensitivity,
        "tags": tags or ["test", "lod"],
    }
    r = requests.post(
        f"{BASE_URL}/entries",
        headers=_headers(key),
        json=body,
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 201, f"create failed: {r.status_code} {r.text}"
    return r.json()


def _archive(entry_id: str, key: str = ADMIN_KEY) -> None:
    try:
        requests.delete(
            f"{BASE_URL}/entries/{entry_id}",
            headers=_headers(key),
            timeout=REQUEST_TIMEOUT,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# LOD0 corpus — structural + heat
# ---------------------------------------------------------------------------


def test_lod0_corpus_structural_shape():
    body = _get_lod(axis="structural", scope="corpus", level=0)

    assert body["axis"] == "structural"
    assert body["scope"] == "corpus"
    assert body["level"] == 0

    s = body["structural"]
    # The data-layer service exposes these aggregate keys; the route names
    # them on the response: edges / relation_types / degree_bins / orphans
    # / size_distribution.
    for key in ("edges", "relation_types", "degree_bins", "orphans", "size_distribution"):
        assert key in s, f"structural missing key {key!r}: got {list(s.keys())}"

    assert isinstance(s["edges"], int)
    assert isinstance(s["relation_types"], dict)
    assert isinstance(s["degree_bins"], dict)
    for bin_key in ("avg", "median", "max"):
        assert bin_key in s["degree_bins"]
    assert isinstance(s["orphans"], int)
    assert isinstance(s["size_distribution"], dict)
    for bucket in ("<1KB", "1-10KB", "10-100KB", ">100KB"):
        assert bucket in s["size_distribution"]


def test_lod0_corpus_heat_shape():
    body = _get_lod(axis="heat", scope="corpus", level=0)

    assert body["axis"] == "heat"
    assert body["scope"] == "corpus"
    assert body["level"] == 0
    assert "heat" in body
    bands = body["heat"]["bands"]
    for band in ("cold", "warm", "hot", "spiking"):
        assert band in bands, f"heat.bands missing {band!r}: got {list(bands.keys())}"
        assert isinstance(bands[band], int)


# ---------------------------------------------------------------------------
# LOD1 community — by tag and by path
# ---------------------------------------------------------------------------


def test_lod1_community_by_tag_happy_path():
    """Create two entries sharing a unique cluster tag, then query LOD1."""
    suffix = uuid.uuid4().hex[:8]
    cluster_tag = f"project:lodtest{suffix}"

    e1 = _create_entry(tags=[cluster_tag, "test", "task"])
    e2 = _create_entry(tags=[cluster_tag, "test", "task:completed"])
    try:
        body = _get_lod(
            axis="structural",
            scope=f"community:tag:{cluster_tag}",
            level=1,
        )
        assert body["axis"] == "structural"
        assert body["level"] == 1
        assert body["community_source"] == "tag"

        c = body["community"]
        assert c["node_count"] == 2, c
        assert isinstance(c["edge_count"], int)
        assert isinstance(c["top_tags"], list)
        # LOD1 historically uses `dominant_content_types` (route preserves
        # the v0.7.x name); enforce that exact key.
        assert "dominant_content_types" in c, list(c.keys())
        assert isinstance(c["dominant_content_types"], list)
    finally:
        _archive(e1["id"])
        _archive(e2["id"])


def test_lod1_community_by_path_happy_path():
    suffix = uuid.uuid4().hex[:8]
    path_prefix = f"LodPath{suffix}"

    e1 = _create_entry(logical_path=f"{path_prefix}/a")
    e2 = _create_entry(logical_path=f"{path_prefix}/b/c")
    try:
        body = _get_lod(
            axis="structural",
            scope=f"community:path:{path_prefix}",
            level=1,
        )
        assert body["community_source"] == "path"
        c = body["community"]
        assert c["node_count"] == 2, c
        assert isinstance(c["edge_count"], int)
        assert "top_tags" in c
        assert "dominant_content_types" in c
    finally:
        _archive(e1["id"])
        _archive(e2["id"])


# ---------------------------------------------------------------------------
# LOD2 community silhouette
# ---------------------------------------------------------------------------


def test_lod2_community_silhouette_fixed_shape():
    suffix = uuid.uuid4().hex[:8]
    cluster_tag = f"project:lod2{suffix}"

    e1 = _create_entry(tags=[cluster_tag, "alpha", "beta", "gamma", "delta", "epsilon", "zeta"])
    e2 = _create_entry(tags=[cluster_tag, "alpha", "beta"])
    try:
        body = _get_lod(
            axis="structural",
            scope=f"community:tag:{cluster_tag}",
            level=2,
        )
        assert body["level"] == 2
        assert body["community_source"] == "tag"

        sil = body["silhouette"]
        for key in ("node_count", "edge_count", "top_tags",
                    "top_content_types", "community_source"):
            assert key in sil, f"silhouette missing {key!r}"

        assert sil["node_count"] == 2
        assert sil["community_source"] == "tag"
        # Caps: top_tags ≤ 5, top_content_types ≤ 3 (per route constants).
        assert len(sil["top_tags"]) <= 5
        assert len(sil["top_content_types"]) <= 3
    finally:
        _archive(e1["id"])
        _archive(e2["id"])


# ---------------------------------------------------------------------------
# LOD4 node silhouette
# ---------------------------------------------------------------------------


def test_lod4_node_silhouette_happy_path():
    e = _create_entry(
        tags=["project:atlas", "task", "review"],
        logical_path="Projects/atlas/notes",
    )
    try:
        body = _get_lod(
            axis="structural",
            scope=f"node:{e['id']}",
            level=4,
        )
        assert body["level"] == 4
        sil = body["silhouette"]
        for key in ("id", "title", "tags", "length",
                    "degree_in", "degree_out",
                    "tag_clusters", "path_cluster"):
            assert key in sil, f"LOD4 silhouette missing {key!r}: {list(sil.keys())}"

        assert sil["id"] == e["id"]
        assert isinstance(sil["tags"], list)
        # `project:atlas` and `task` look like cluster tags (prefix:value),
        # `review` is a plain tag — only the colon-prefixed ones land in
        # tag_clusters per the service.
        assert "project:atlas" in sil["tag_clusters"]
        assert sil["path_cluster"] == "Projects"
        assert isinstance(sil["length"], int)
        assert isinstance(sil["degree_in"], int)
        assert isinstance(sil["degree_out"], int)
    finally:
        _archive(e["id"])


# ---------------------------------------------------------------------------
# LOD6 section outline — fenced code block must NOT yield headings
# ---------------------------------------------------------------------------


def test_lod6_section_outline_excludes_fenced_code_headings():
    content = (
        "# Real Heading One\n"
        "\n"
        "Some prose here.\n"
        "\n"
        "## Real Heading Two\n"
        "\n"
        "```python\n"
        "# this is a code comment, NOT a heading\n"
        "x = 1\n"
        "```\n"
        "\n"
        "### Real Heading Three\n"
    )
    e = _create_entry(content=content)
    try:
        body = _get_lod(
            axis="structural",
            scope=f"node:{e['id']}",
            level=6,
        )
        assert body["level"] == 6
        outline = body["outline"]
        assert isinstance(outline, list)
        texts = [o["text"] for o in outline]
        assert "Real Heading One" in texts
        assert "Real Heading Two" in texts
        assert "Real Heading Three" in texts
        # The `#` inside the fenced code block must NOT be recognized.
        assert not any("code comment" in t for t in texts), (
            f"fenced-code `#` leaked into outline: {texts}"
        )
        # Levels must be ints.
        for h in outline:
            assert isinstance(h["level"], int)
            assert isinstance(h["line"], int)
    finally:
        _archive(e["id"])


# ---------------------------------------------------------------------------
# Grammar / validation errors
# ---------------------------------------------------------------------------


def test_lod_invalid_axis_returns_400_with_grammar_hint():
    r = requests.get(
        f"{BASE_URL}/lod",
        params={"axis": "epistemic", "scope": "corpus", "level": 0},
        headers=_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 400, r.text
    detail = json.dumps(r.json())
    # Grammar hint must enumerate valid axes so callers can self-correct.
    assert "structural" in detail and "heat" in detail, detail


def test_lod4_with_corpus_scope_returns_400():
    """level=4 requires node:<id> scope; corpus must be rejected."""
    r = requests.get(
        f"{BASE_URL}/lod",
        params={"axis": "structural", "scope": "corpus", "level": 4},
        headers=_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 400, r.text


def test_lod6_with_corpus_scope_returns_400():
    r = requests.get(
        f"{BASE_URL}/lod",
        params={"axis": "structural", "scope": "corpus", "level": 6},
        headers=_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# RLS scoping — agent (User B) cannot read admin's private entry's silhouette
# ---------------------------------------------------------------------------


def test_lod4_rls_does_not_leak_private_entry_to_agent():
    """Admin creates a `sensitivity='private'` entry; agent role's RLS
    ceiling is `shared`-only, so LOD4 must 404 — not return a silhouette.
    """
    e = _create_entry(
        sensitivity="private",
        tags=["test", "rls", "private"],
    )
    try:
        # Admin can see it.
        admin_body = _get_lod(
            axis="structural",
            scope=f"node:{e['id']}",
            level=4,
            key=ADMIN_KEY,
        )
        assert admin_body["silhouette"]["id"] == e["id"]

        # Agent must NOT — silhouette would be a scope leak.
        r = requests.get(
            f"{BASE_URL}/lod",
            params={
                "axis": "structural",
                "scope": f"node:{e['id']}",
                "level": 4,
            },
            headers=_headers(AGENT_KEY),
            timeout=REQUEST_TIMEOUT,
        )
        assert r.status_code == 404, (
            f"expected 404 (RLS scope leak prevention); got {r.status_code} {r.text}"
        )
    finally:
        _archive(e["id"])


# ---------------------------------------------------------------------------
# Heat banding fixture — admin-only insert into entry_access_log,
# then assert each band lights up.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _PSYCOPG_AVAILABLE,
    reason="psycopg not installed; cannot seed entry_access_log directly",
)
def test_heat_bands_populate_with_seeded_access_log():
    """Insert known rows into `entry_access_log` and assert the bands
    move correctly.

    `entry_access_log` is admin-only RLS; we connect as DB superuser
    (the test DSN) which bypasses RLS, mirroring the way the API
    runs the query as kb_admin in production.
    """
    # Three entries — one for each of cold (no log rows), hot (one
    # recent), spiking (many recent).
    cold = _create_entry(tags=["heat-test", "cold"])
    hot = _create_entry(tags=["heat-test", "hot"])
    spiking = _create_entry(tags=["heat-test", "spiking"])
    warm = _create_entry(tags=["heat-test", "warm"])

    # Capture baseline so we can compare deltas.
    baseline = _get_lod(axis="heat", scope="corpus", level=0)["heat"]["bands"]

    try:
        with psycopg.connect(DB_DSN, autocommit=True) as conn:
            with conn.cursor() as cur:
                # hot: 1 read in the last 24h (≤ SPIKING_READS_PER_24H=5).
                cur.execute(
                    "INSERT INTO entry_access_log (entry_id, ts, action) "
                    "VALUES (%s, now() - INTERVAL '1 hour', 'read')",
                    (hot["id"],),
                )
                # spiking: 6 reads in the last 24h (> 5 → spiking).
                for _ in range(6):
                    cur.execute(
                        "INSERT INTO entry_access_log (entry_id, ts, action) "
                        "VALUES (%s, now() - INTERVAL '30 minutes', 'read')",
                        (spiking["id"],),
                    )
                # warm: 1 read 3 days ago (in 7d window, NOT in 24h).
                cur.execute(
                    "INSERT INTO entry_access_log (entry_id, ts, action) "
                    "VALUES (%s, now() - INTERVAL '3 days', 'read')",
                    (warm["id"],),
                )

        body = _get_lod(axis="heat", scope="corpus", level=0)
        bands = body["heat"]["bands"]

        # Each band must be at least the baseline + 1 (since each fixture
        # entry contributes to a different band).
        assert bands["hot"] >= baseline["hot"] + 1, (
            f"hot did not increase: baseline={baseline}, got={bands}"
        )
        assert bands["spiking"] >= baseline["spiking"] + 1, (
            f"spiking did not increase: baseline={baseline}, got={bands}"
        )
        assert bands["warm"] >= baseline["warm"] + 1, (
            f"warm did not increase: baseline={baseline}, got={bands}"
        )
        # Cold count must have grown by at least 1 (the never-accessed
        # `cold` fixture). Other entries in the corpus may also be cold.
        assert bands["cold"] >= baseline["cold"] + 1, (
            f"cold did not increase: baseline={baseline}, got={bands}"
        )
    finally:
        for ent in (cold, hot, spiking, warm):
            _archive(ent["id"])
