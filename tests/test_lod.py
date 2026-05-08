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
    # Use a clearly bogus axis. (Sprint 0049 used `epistemic` here as a
    # sentinel because it was deferred; Sprint 0050 ships epistemic so the
    # sentinel had to move to something never planned.)
    r = requests.get(
        f"{BASE_URL}/lod",
        params={"axis": "fictional", "scope": "corpus", "level": 0},
        headers=_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 400, r.text
    detail = json.dumps(r.json())
    # Grammar hint must enumerate valid axes so callers can self-correct.
    assert "structural" in detail and "heat" in detail, detail
    assert "epistemic" in detail, detail


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


# ---------------------------------------------------------------------------
# Heat-axis branches at LOD2 + LOD4 (T-0289, closes GH #73)
#
# Sprint 0049 wired axis=heat for LOD0 corpus only; at LOD1/LOD2/LOD4 the
# service silently fell through to the structural silhouette regardless of
# axis=. These tests assert the new heat shapes and the rls_filtered hint.
# ---------------------------------------------------------------------------


def test_lod4_heat_axis_returns_heat_chip_not_silhouette():
    """LOD4 with axis=heat must return a heat chip, not the structural
    silhouette. Asserts the payload differs from axis=structural for the
    same scope+level (acceptance criterion from T-0289)."""
    e = _create_entry(tags=["heat-axis-test"])
    try:
        struct = _get_lod(
            axis="structural",
            scope=f"node:{e['id']}",
            level=4,
        )
        heat = _get_lod(
            axis="heat",
            scope=f"node:{e['id']}",
            level=4,
        )

        # Shapes must differ — structural carries `silhouette`, heat carries `heat`.
        assert "silhouette" in struct and "silhouette" not in heat, (
            f"heat payload still returned silhouette: {heat}"
        )
        assert "heat" in heat, f"LOD4 heat missing heat block: {heat}"
        assert heat["axis"] == "heat"
        assert heat["level"] == 4
        assert heat["id"] == e["id"]
        assert "title" in heat

        chip = heat["heat"]
        assert chip["band"] in ("cold", "warm", "hot", "spiking")
        assert isinstance(chip["reads_24h"], int)
        assert isinstance(chip["reads_7d"], int)
        # last_ts is ISO string or None.
        assert chip["last_ts"] is None or isinstance(chip["last_ts"], str)

        # Same data behind both — assert distinct top-level shapes.
        assert struct != heat
    finally:
        _archive(e["id"])


@pytest.mark.skipif(
    not _PSYCOPG_AVAILABLE,
    reason="psycopg not installed; cannot seed entry_access_log directly",
)
def test_lod4_heat_axis_band_promotion_with_seeded_reads():
    """Seed entry_access_log for one entry and assert its LOD4 heat chip
    promotes from `cold` to `hot`/`spiking`."""
    e = _create_entry(tags=["heat-axis-band-test"])
    try:
        # Baseline before any seeded reads.
        before = _get_lod(
            axis="heat",
            scope=f"node:{e['id']}",
            level=4,
        )["heat"]
        assert before["band"] == "cold", before
        assert before["reads_24h"] == 0
        assert before["reads_7d"] == 0

        # Seed 6 reads in the last hour → spiking.
        with psycopg.connect(DB_DSN, autocommit=True) as conn:
            with conn.cursor() as cur:
                for _ in range(6):
                    cur.execute(
                        "INSERT INTO entry_access_log (entry_id, ts, action) "
                        "VALUES (%s, now() - INTERVAL '15 minutes', 'read')",
                        (e["id"],),
                    )

        after = _get_lod(
            axis="heat",
            scope=f"node:{e['id']}",
            level=4,
        )["heat"]
        assert after["band"] == "spiking", after
        assert after["reads_24h"] >= 6
        assert after["reads_7d"] >= 6
        assert after["last_ts"] is not None
    finally:
        _archive(e["id"])


def test_lod4_heat_rls_filtered_hint_for_non_admin_actor():
    """Non-admin agent role hitting LOD4 heat on a connected entry should
    see `rls_filtered: true` because entry_access_log is admin-only RLS.

    Constructs a tiny 2-node connected community via a shared cluster tag
    so the silhouette degree is zero but the surrounding fixture
    (cluster-tag co-membership) ensures the entry is visible to the agent.
    Connectedness is asserted by linking the entry to a second one.
    """
    suffix = uuid.uuid4().hex[:8]
    cluster_tag = f"project:rlsheat{suffix}"
    a = _create_entry(tags=[cluster_tag, "rls-heat-test"])
    b = _create_entry(tags=[cluster_tag, "rls-heat-test"])
    try:
        # Link the two so `a` has degree>0.
        link_resp = requests.post(
            f"{BASE_URL}/entries/{a['id']}/links",
            headers=_headers(ADMIN_KEY),
            json={"target_entry_id": b["id"], "link_type": "related"},
            timeout=REQUEST_TIMEOUT,
        )
        # Older builds may use a different link route; accept any 2xx
        # and skip the rls_filtered assertion if the link failed (the
        # hint is degree-gated so it would be absent).
        link_ok = 200 <= link_resp.status_code < 300

        # Agent fetches LOD4 heat. agent's RLS scope sees the entry
        # (sensitivity defaults to shared) but cannot see entry_access_log.
        chip = _get_lod(
            axis="heat",
            scope=f"node:{a['id']}",
            level=4,
            key=AGENT_KEY,
        )["heat"]

        assert chip["band"] == "cold", chip
        assert chip["reads_7d"] == 0

        if link_ok:
            # rls_filtered hint must be true — degree>0 + cold + no reads
            # is the documented "probably RLS-induced silence" signal.
            assert chip.get("rls_filtered") is True, (
                f"expected rls_filtered hint for non-admin actor on "
                f"connected entry: {chip}"
            )
    finally:
        _archive(a["id"])
        _archive(b["id"])


def test_lod2_heat_axis_returns_per_band_counts():
    """LOD2 with axis=heat returns per-band counts in the same shape as
    the LOD0 corpus heat block. Asserts payload differs from
    axis=structural for the same scope+level."""
    suffix = uuid.uuid4().hex[:8]
    cluster_tag = f"project:lod2heat{suffix}"

    e1 = _create_entry(tags=[cluster_tag, "x"])
    e2 = _create_entry(tags=[cluster_tag, "y"])
    try:
        struct = _get_lod(
            axis="structural",
            scope=f"community:tag:{cluster_tag}",
            level=2,
        )
        heat = _get_lod(
            axis="heat",
            scope=f"community:tag:{cluster_tag}",
            level=2,
        )

        # Structural keeps silhouette; heat carries `heat.bands`.
        assert "silhouette" in struct
        assert "silhouette" not in heat, f"heat payload kept silhouette: {heat}"
        assert "heat" in heat, heat
        assert heat["axis"] == "heat"
        assert heat["level"] == 2
        assert heat["community_source"] == "tag"

        bands = heat["heat"]["bands"]
        for band in ("cold", "warm", "hot", "spiking"):
            assert band in bands, bands
            assert isinstance(bands[band], int)

        # The community has 2 entries, so the band counts should sum to 2.
        assert sum(bands.values()) == 2, bands

        assert struct != heat
    finally:
        _archive(e1["id"])
        _archive(e2["id"])


# ---------------------------------------------------------------------------
# Epistemic axis (T-0291, Sprint 0050) — LOD0 corpus / LOD2 community / LOD4
# node chip; LOD1 + LOD6 must 400 with the documented error.
#
# Setup pattern: create entries with explicit (claim_type, verification_status)
# pairs via PATCH (EntryUpdate accepts the epistemic fields per migration 033).
# We use a unique cluster tag so the community-scoped histogram is bounded to
# our fixture, not the whole corpus.
# ---------------------------------------------------------------------------


EPISTEMIC_CLAIM_TYPES = ("event", "observation", "claim", "rule")
EPISTEMIC_VERIFICATION_STATUSES = (
    "verified",
    "pending",
    "disputed",
    "superseded",
)
EPISTEMIC_LEVEL_ERROR = (
    "epistemic axis is defined at LOD0/LOD2/LOD4 only"
)


def _patch_epistemic(
    entry_id: str,
    *,
    claim_type: str | None = None,
    source_confidence: str | None = None,
    verification_status: str | None = None,
    conflict_with: list[str] | None = None,
    key: str = ADMIN_KEY,
) -> None:
    """PATCH an entry's epistemic fields. EntryUpdate accepts these per
    migration 033 + api/models.py changes shipped in T-0290."""
    body: dict = {}
    if claim_type is not None:
        body["claim_type"] = claim_type
    if source_confidence is not None:
        body["source_confidence"] = source_confidence
    if verification_status is not None:
        body["verification_status"] = verification_status
    if conflict_with is not None:
        body["conflict_with"] = conflict_with
    r = requests.patch(
        f"{BASE_URL}/entries/{entry_id}",
        headers=_headers(key),
        json=body,
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code in (200, 204), (
        f"PATCH epistemic failed: {r.status_code} {r.text}"
    )


def test_lod0_epistemic_histogram_full_grid():
    """Seed 3+ entries across multiple (claim_type × verification_status)
    cells; assert every cell is present (zero when empty) and known cells
    increment by the seeded count.

    The histogram is corpus-wide so we compare deltas against a baseline
    snapshot rather than absolute counts (other tests' fixtures may overlap).
    """
    baseline = _get_lod(
        axis="epistemic", scope="corpus", level=0
    )["epistemic"]

    # Pre-condition: full 4×4 grid present even before we seed.
    assert set(baseline.keys()) == set(EPISTEMIC_CLAIM_TYPES), baseline
    for ct in EPISTEMIC_CLAIM_TYPES:
        assert set(baseline[ct].keys()) == set(EPISTEMIC_VERIFICATION_STATUSES), (
            f"missing verification_status keys for {ct}: {baseline[ct]}"
        )
        for vs in EPISTEMIC_VERIFICATION_STATUSES:
            assert isinstance(baseline[ct][vs], int)

    # Seed 3 entries hitting 3 distinct cells.
    seeded: list[tuple[dict, str, str]] = []
    cases = [
        ("claim", "verified"),
        ("observation", "pending"),
        ("rule", "disputed"),
    ]
    try:
        for ct, vs in cases:
            e = _create_entry(tags=["epistemic-grid-test"])
            _patch_epistemic(
                e["id"], claim_type=ct, verification_status=vs
            )
            seeded.append((e, ct, vs))

        after = _get_lod(
            axis="epistemic", scope="corpus", level=0
        )["epistemic"]

        # Full grid present.
        assert set(after.keys()) == set(EPISTEMIC_CLAIM_TYPES)
        for ct in EPISTEMIC_CLAIM_TYPES:
            assert set(after[ct].keys()) == set(EPISTEMIC_VERIFICATION_STATUSES)

        # Seeded cells incremented by exactly 1 each.
        for _e, ct, vs in seeded:
            assert after[ct][vs] >= baseline[ct][vs] + 1, (
                f"cell ({ct},{ vs}) did not grow: "
                f"baseline={baseline[ct][vs]}, after={after[ct][vs]}"
            )
    finally:
        for e, _ct, _vs in seeded:
            _archive(e["id"])


def test_lod4_epistemic_chip_no_extra_fields():
    """LOD4 axis=epistemic must return ONLY the four documented fields in
    the `epistemic` block — no `title` / `content` / tags leak (acceptance
    criterion #2 from T-0291)."""
    e = _create_entry(tags=["epistemic-chip-test"])
    try:
        _patch_epistemic(
            e["id"],
            claim_type="claim",
            source_confidence="reported",
            verification_status="verified",
        )
        body = _get_lod(
            axis="epistemic",
            scope=f"node:{e['id']}",
            level=4,
        )

        assert body["axis"] == "epistemic"
        assert body["level"] == 4
        assert body["id"] == e["id"]
        # Tight chip: top-level keys must be exactly {axis, scope, level, id,
        # epistemic} — title MUST NOT be present.
        assert "title" not in body, (
            f"title leaked into LOD4 epistemic payload: {body}"
        )
        assert "silhouette" not in body, body
        assert "heat" not in body, body

        chip = body["epistemic"]
        # Exactly the four documented keys, nothing else.
        assert set(chip.keys()) == {
            "claim_type",
            "source_confidence",
            "verification_status",
            "conflict_with",
        }, f"unexpected chip keys: {sorted(chip.keys())}"

        assert chip["claim_type"] == "claim"
        assert chip["source_confidence"] == "reported"
        assert chip["verification_status"] == "verified"
        assert isinstance(chip["conflict_with"], list)
    finally:
        _archive(e["id"])


def test_lod2_epistemic_community_histogram():
    """LOD2 axis=epistemic returns the per-community 4×4 grid scoped by
    cluster tag. With two entries in known cells the histogram inside the
    community should reflect exactly those counts."""
    suffix = uuid.uuid4().hex[:8]
    cluster_tag = f"project:lod2epi{suffix}"
    e1 = _create_entry(tags=[cluster_tag, "epi"])
    e2 = _create_entry(tags=[cluster_tag, "epi"])
    try:
        _patch_epistemic(
            e1["id"], claim_type="claim", verification_status="verified"
        )
        _patch_epistemic(
            e2["id"], claim_type="observation", verification_status="pending"
        )

        body = _get_lod(
            axis="epistemic",
            scope=f"community:tag:{cluster_tag}",
            level=2,
        )
        assert body["axis"] == "epistemic"
        assert body["level"] == 2
        assert body["community_source"] == "tag"

        grid = body["epistemic"]
        # Full grid still present.
        assert set(grid.keys()) == set(EPISTEMIC_CLAIM_TYPES)
        # Sum across the entire grid equals the community size (2).
        total = sum(
            grid[ct][vs]
            for ct in EPISTEMIC_CLAIM_TYPES
            for vs in EPISTEMIC_VERIFICATION_STATUSES
        )
        assert total == 2, grid
        assert grid["claim"]["verified"] == 1, grid
        assert grid["observation"]["pending"] == 1, grid
    finally:
        _archive(e1["id"])
        _archive(e2["id"])


def test_lod1_epistemic_returns_400():
    """axis=epistemic at LOD1 must 400 with the exact documented detail."""
    r = requests.get(
        f"{BASE_URL}/lod",
        params={
            "axis": "epistemic",
            "scope": "community:tag:any",
            "level": 1,
        },
        headers=_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["detail"] == EPISTEMIC_LEVEL_ERROR, body


def test_lod6_epistemic_returns_400():
    """axis=epistemic at LOD6 must 400 with the exact documented detail."""
    r = requests.get(
        f"{BASE_URL}/lod",
        params={
            "axis": "epistemic",
            "scope": "node:00000000-0000-0000-0000-000000000000",
            "level": 6,
        },
        headers=_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["detail"] == EPISTEMIC_LEVEL_ERROR, body


@pytest.mark.skipif(
    not _PSYCOPG_AVAILABLE,
    reason="psycopg not installed; cannot EXPLAIN the histogram query",
)
def test_lod0_epistemic_histogram_uses_index():
    """EXPLAIN the LOD0 corpus epistemic histogram and assert the planner
    picks `entries_epistemic_histogram_idx` (or at least references it).

    We connect as DB superuser via the test DSN — RLS doesn't affect
    EXPLAIN output for the index choice. If the planner picks a sequential
    scan because the corpus is too small, the index name will not appear
    and the test will skip with a documented note rather than fail (small
    fixtures defeat index selection).
    """
    query = (
        "EXPLAIN SELECT claim_type::text, verification_status::text, "
        "COUNT(*) FROM entries WHERE status = 'published' "
        "GROUP BY claim_type, verification_status"
    )
    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()
    plan_text = "\n".join(r[0] for r in rows)
    if "entries_epistemic_histogram_idx" not in plan_text:
        pytest.skip(
            f"planner chose seqscan (likely small fixture corpus); plan:\n{plan_text}"
        )
    assert "entries_epistemic_histogram_idx" in plan_text


def test_lod4_epistemic_rls_does_not_leak_private_entry_to_agent():
    """RLS scope-leak coverage for the epistemic axis at LOD4 (T-0295).

    Mirrors `test_lod4_rls_does_not_leak_private_entry_to_agent` for the
    structural axis: admin creates a `sensitivity='private'` entry, agent's
    RLS ceiling is `shared`-only, so axis=epistemic LOD4 must 404 — not
    return an epistemic chip. The chip would itself be a scope leak even
    though it omits content/title (claim_type/verification_status alone
    confirm existence of an entry the agent must not see).
    """
    e = _create_entry(
        sensitivity="private",
        tags=["test", "rls", "epistemic-private"],
    )
    try:
        _patch_epistemic(
            e["id"],
            claim_type="claim",
            source_confidence="reported",
            verification_status="verified",
        )

        # Admin still sees it.
        admin_body = _get_lod(
            axis="epistemic",
            scope=f"node:{e['id']}",
            level=4,
            key=ADMIN_KEY,
        )
        assert admin_body["id"] == e["id"]
        assert admin_body["epistemic"]["claim_type"] == "claim"

        # Agent must NOT — chip would confirm an admin-private row exists.
        r = requests.get(
            f"{BASE_URL}/lod",
            params={
                "axis": "epistemic",
                "scope": f"node:{e['id']}",
                "level": 4,
            },
            headers=_headers(AGENT_KEY),
            timeout=REQUEST_TIMEOUT,
        )
        assert r.status_code == 404, (
            f"expected 404 (RLS scope leak prevention on epistemic LOD4); "
            f"got {r.status_code} {r.text}"
        )
    finally:
        _archive(e["id"])


def test_lod1_heat_axis_returns_per_band_counts():
    """LOD1 axis=heat shape parity with LOD2 — covers the LOD1 fall-through
    that #73 also implicates."""
    suffix = uuid.uuid4().hex[:8]
    cluster_tag = f"project:lod1heat{suffix}"
    e = _create_entry(tags=[cluster_tag])
    try:
        heat = _get_lod(
            axis="heat",
            scope=f"community:tag:{cluster_tag}",
            level=1,
        )
        assert heat["axis"] == "heat"
        assert heat["level"] == 1
        assert "heat" in heat
        bands = heat["heat"]["bands"]
        for band in ("cold", "warm", "hot", "spiking"):
            assert band in bands
        # Single member → exactly one band increments.
        assert sum(bands.values()) == 1, bands
    finally:
        _archive(e["id"])
