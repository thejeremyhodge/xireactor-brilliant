#!/usr/bin/env bash
# Smoke test for install.sh.
#
# --host  : run against the current host (expects Docker preinstalled).
#           This is the mode used in CI (ubuntu-22.04 runners have Docker)
#           and by maintainers on Mac.
# --dind  : run inside an ubuntu:22.04 container with docker-in-docker.
#           Deferred — for now this mode prints a note and delegates to --host.
#
# SAFETY: this script tears the stack down (`docker compose down -v`) and
# overwrites ./.env. It requires either TEST_INSTALLER_ALLOW_DESTROY=1 or
# the `--allow-destroy` flag to guard against trashing a maintainer's local KB.

set -euo pipefail

MODE="--host"
ALLOW_DESTROY="${TEST_INSTALLER_ALLOW_DESTROY:-0}"
KEY_FILE="/tmp/brilliant-smoke-key.$$.txt"
SUMMARY_FILE="/tmp/brilliant-smoke-summary.$$.txt"
API_URL="http://localhost:8010"

while [ $# -gt 0 ]; do
  case "$1" in
    --host)           MODE="--host"; shift ;;
    --dind)           MODE="--dind"; shift ;;
    --allow-destroy)  ALLOW_DESTROY=1; shift ;;
    -h|--help)
      sed -n '2,13p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *) echo "Unknown flag: $1" >&2; exit 64 ;;
  esac
done

if [ "$ALLOW_DESTROY" != "1" ]; then
  cat >&2 <<MSG
test_installer.sh will run 'docker compose down -v' and overwrite ./.env.
Re-run with --allow-destroy (or TEST_INSTALLER_ALLOW_DESTROY=1) to proceed.
MSG
  exit 1
fi

if [ "$MODE" = "--dind" ]; then
  echo "[smoke] --dind mode not yet implemented; falling back to --host."
  echo "[smoke] CI runs on ubuntu-22.04 runners with Docker preinstalled — --host covers it."
  MODE="--host"
fi

cleanup() {
  local rc=$?
  echo "[smoke] cleanup (rc=$rc)"
  docker compose down -v 2>/dev/null || true
  rm -f "$KEY_FILE" "$SUMMARY_FILE" ./install.log ./brilliant-credentials.txt 2>/dev/null || true
  return "$rc"
}
trap cleanup EXIT

cd "$(dirname "$0")/.."

echo "[smoke] tearing down any pre-existing stack"
docker compose down -v 2>/dev/null || true

echo "[smoke] removing pre-existing .env (if any)"
rm -f ./.env

START_TS=$(date +%s)

echo "[smoke] running ./install.sh"
./install.sh \
  --admin-email smoke@example.com \
  --force \
  --key-out "$KEY_FILE" \
  | tee "$SUMMARY_FILE"

END_TS=$(date +%s)
ELAPSED=$(( END_TS - START_TS ))
echo "[smoke] install completed in ${ELAPSED}s"

if [ "$ELAPSED" -gt 300 ]; then
  echo "[smoke] FAIL: install took ${ELAPSED}s (>300s budget)" >&2
  exit 10
fi

echo "[smoke] asserting /health is 200"
curl -fsS "${API_URL}/health" >/dev/null || {
  echo "[smoke] FAIL: /health did not return 200" >&2
  exit 11
}

echo "[smoke] asserting credentials file has all six keys"
# Sprint 0043 T-0253 — brilliant-credentials.txt is now a key=value file
# with six fields: admin_email, admin_api_key, oauth_client_id,
# oauth_client_secret, mcp_url, login_url. Assert each key is present
# with a non-empty value (oauth_client_secret is long hex; the grep
# tolerates any non-empty value after the equals sign).
for required_key in admin_email admin_api_key oauth_client_id oauth_client_secret mcp_url login_url; do
  if ! grep -Eq "^${required_key}=.+" "$KEY_FILE"; then
    echo "[smoke] FAIL: ${required_key} missing or empty in $KEY_FILE" >&2
    echo "[smoke] credentials file contents:" >&2
    cat "$KEY_FILE" >&2 || true
    exit 14
  fi
done

echo "[smoke] asserting /entries with the admin key is 200"
key="$(grep '^admin_api_key=' "$KEY_FILE" | head -n 1 | cut -d'=' -f2-)"
if [ -z "$key" ]; then
  echo "[smoke] FAIL: admin_api_key value is empty" >&2
  exit 15
fi
code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${key}" "${API_URL}/entries")"
if [ "$code" != "200" ]; then
  echo "[smoke] FAIL: /entries returned $code, expected 200" >&2
  exit 12
fi

echo "[smoke] asserting key file is mode 600"
# Cross-platform mode check: GNU stat uses -c, BSD stat uses -f.
mode="$(stat -c '%a' "$KEY_FILE" 2>/dev/null || stat -f '%Lp' "$KEY_FILE")"
if [ "$mode" != "600" ]; then
  echo "[smoke] FAIL: key file mode is $mode, expected 600" >&2
  exit 13
fi

echo "[smoke] asserting banner's Key file path is absolute and resolvable from any CWD"
# Extract the credentials-file path from the banner (captured in
# $SUMMARY_FILE). The banner format is:
#   Credentials file (contains 6 fields — keep it safe):
#     <path> (mode 600)
# So we match the header line, advance one line, and strip the "(mode 600)"
# suffix + leading whitespace. Sprint 0043 T-0253 banner reshape.
banner_key_path="$(awk '/Credentials file/ {getline; gsub(/^[[:space:]]+/, "", $0); sub(/ \(mode 600\).*$/, "", $0); print; exit}' "$SUMMARY_FILE")"
if [ -z "$banner_key_path" ]; then
  echo "[smoke] FAIL: could not extract Key file path from banner" >&2
  echo "[smoke] banner contents:" >&2
  cat "$SUMMARY_FILE" >&2 || true
  exit 16
fi
case "$banner_key_path" in
  /*) : ;;  # absolute — good
  *)  echo "[smoke] FAIL: banner Key file path is relative ($banner_key_path)" >&2; exit 17 ;;
esac
# Resolve from a different CWD (as the operator would after the installer exits).
if ! ( cd /tmp && test -r "$banner_key_path" ); then
  echo "[smoke] FAIL: banner Key file path does not resolve from /tmp: $banner_key_path" >&2
  exit 18
fi

echo "[smoke] baseline scenario PASS (${ELAPSED}s, under the 300s budget)"

# ─────────────────────────────────────────────────────────────────────
# Port-conflict scenario (Sprint 0043 T-0257)
#
# Goal: with host port 5442 already bound, the installer must probe and
# pick the next +10 step (5452) for the DB, write BRILLIANT_DB_PORT to
# `.env`, and surface the chosen value in the banner / credentials file.
#
# Strategy:
#   1. Tear down the baseline stack.
#   2. Occupy 5442 with a disposable container (postgres:16-alpine is
#      small and trivially bindable; we trap-cleanup it no matter what).
#   3. Re-run install.sh; expect BRILLIANT_DB_PORT=5452 in `.env`.
#   4. Assert the chosen port flows into the banner's Postgres line.
#
# The API/MCP ports (8010/8011) are not artificially contended in this
# test — if a stray dev server is bound to them on a maintainer machine
# that's fine, the probe will just shift the api/mcp ports too and the
# banner will reflect whatever it picked.
# ─────────────────────────────────────────────────────────────────────

echo "[smoke] port-conflict scenario: occupying 5442"

CONFLICT_CONTAINER="bk-pg-conflict.$$"
CONFLICT_KEY_FILE="/tmp/brilliant-smoke-conflict-key.$$.txt"
CONFLICT_SUMMARY_FILE="/tmp/brilliant-smoke-conflict-summary.$$.txt"
conflict_cleanup() {
  docker rm -f "$CONFLICT_CONTAINER" >/dev/null 2>&1 || true
  rm -f "$CONFLICT_KEY_FILE" "$CONFLICT_SUMMARY_FILE" 2>/dev/null || true
}
# Add to the main trap chain by layering a nested cleanup. bash `trap`
# replaces rather than appends, so call both explicitly on EXIT.
trap 'conflict_cleanup; cleanup' EXIT

echo "[smoke] tearing down baseline stack before conflict probe"
docker compose down -v 2>/dev/null || true
rm -f ./.env ./install.log ./brilliant-credentials.txt 2>/dev/null || true

echo "[smoke] starting sentinel container on host port 5442"
if ! docker run --rm -d \
    -p 5442:5432 \
    -e POSTGRES_PASSWORD=conflict \
    --name "$CONFLICT_CONTAINER" \
    postgres:16-alpine >/dev/null; then
  echo "[smoke] FAIL: could not start sentinel postgres container on :5442" >&2
  exit 20
fi
# Give Docker a moment to actually bind the port before the probe runs.
sleep 2

# Sanity-check that the port really is occupied from the probe's POV.
if command -v lsof >/dev/null 2>&1; then
  if ! lsof -i :5442 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "[smoke] FAIL: sentinel container did not bind 5442 (lsof view)" >&2
    exit 21
  fi
fi

echo "[smoke] running ./install.sh against contested :5442"
./install.sh \
  --admin-email smoke-conflict@example.com \
  --force \
  --key-out "$CONFLICT_KEY_FILE" \
  | tee "$CONFLICT_SUMMARY_FILE"

echo "[smoke] asserting .env picked BRILLIANT_DB_PORT=5452"
if ! grep -Eq '^BRILLIANT_DB_PORT=5452$' ./.env; then
  echo "[smoke] FAIL: expected BRILLIANT_DB_PORT=5452 in .env" >&2
  echo "[smoke] .env port lines:" >&2
  grep -E '^BRILLIANT_(DB|API|MCP)_PORT=' ./.env >&2 || true
  exit 22
fi

echo "[smoke] asserting banner's Postgres line reports the chosen 5452 port"
if ! grep -Fq 'Postgres:     localhost:5452' "$CONFLICT_SUMMARY_FILE"; then
  echo "[smoke] FAIL: banner did not report localhost:5452" >&2
  echo "[smoke] conflict banner:" >&2
  cat "$CONFLICT_SUMMARY_FILE" >&2 || true
  exit 23
fi

echo "[smoke] asserting credentials file mentions the chosen ports"
# mcp_url / login_url should still be present with non-empty values.
# The exact ports depend on what else is bound on the machine; we just
# assert the six-field contract still holds under conflict conditions.
for required_key in admin_email admin_api_key oauth_client_id oauth_client_secret mcp_url login_url; do
  if ! grep -Eq "^${required_key}=.+" "$CONFLICT_KEY_FILE"; then
    echo "[smoke] FAIL: ${required_key} missing in conflict credentials file" >&2
    cat "$CONFLICT_KEY_FILE" >&2 || true
    exit 24
  fi
done

echo "[smoke] port-conflict scenario PASS"

# ─────────────────────────────────────────────────────────────────────
# Non-collision scenario (Sprint 0043 T-0259)
#
# Goal: two installs in separate dirs must produce independent compose
# projects — so `docker compose up` in install #B does not RECREATE
# install #A's containers (which would clobber A's pgdata volume with
# B's Postgres password → PoolTimeout crashloop).
#
# Strategy:
#   1. Capture install #A's state: COMPOSE_PROJECT_NAME from its .env,
#      container IDs via `docker compose ps -q`.
#   2. Copy this checkout to /tmp/bk-alt-$$/xireactor-brilliant (tree
#      only — no .env, no .git, no install.log). rsync's --exclude is
#      portable across mac + ubuntu-22.04 CI runners.
#   3. Run install.sh from the copy. Port-probe shifts off whatever
#      stack A is holding.
#   4. Assert:
#      - Copy's .env has a DIFFERENT COMPOSE_PROJECT_NAME.
#      - Stack A's container IDs are unchanged (not Recreated).
#      - Two distinct *_pgdata volumes exist in `docker volume ls`.
#   5. Tear down stack B (from the copy dir) before the EXIT trap runs.
# ─────────────────────────────────────────────────────────────────────

echo "[smoke] non-collision scenario: capturing install #A state"

A_PROJECT="$(grep '^COMPOSE_PROJECT_NAME=' ./.env | head -n 1 | cut -d'=' -f2-)"
if [ -z "$A_PROJECT" ]; then
  echo "[smoke] FAIL: install #A .env missing COMPOSE_PROJECT_NAME (T-0259)" >&2
  exit 30
fi
A_IDS_BEFORE="$(docker compose ps -q | sort | tr '\n' ' ')"
if [ -z "$A_IDS_BEFORE" ]; then
  echo "[smoke] FAIL: install #A has no running containers before sibling install" >&2
  exit 31
fi
echo "[smoke] install #A: project=${A_PROJECT} ids=${A_IDS_BEFORE}"

ALT_ROOT="/tmp/bk-alt-$$"
ALT_DIR="${ALT_ROOT}/xireactor-brilliant"
ALT_KEY_FILE="/tmp/brilliant-smoke-alt-key.$$.txt"
ALT_SUMMARY_FILE="/tmp/brilliant-smoke-alt-summary.$$.txt"

noncollision_cleanup() {
  # Tear down stack B from its own dir so its project (not A's) is targeted.
  if [ -d "$ALT_DIR" ] && [ -f "$ALT_DIR/docker-compose.yml" ]; then
    ( cd "$ALT_DIR" && docker compose down -v 2>/dev/null || true )
  fi
  rm -rf "$ALT_ROOT" 2>/dev/null || true
  rm -f "$ALT_KEY_FILE" "$ALT_SUMMARY_FILE" 2>/dev/null || true
}
# Chain: non-collision cleanup → conflict cleanup → baseline cleanup.
trap 'noncollision_cleanup; conflict_cleanup; cleanup' EXIT

echo "[smoke] copying tree to ${ALT_DIR} for install #B"
mkdir -p "$ALT_DIR"
# rsync with exclusions: skip .git (huge), .env + install.log + creds (stale),
# node_modules if it exists, and any tmp/test artifacts.
rsync -a \
  --exclude='.git' \
  --exclude='.env' \
  --exclude='install.log' \
  --exclude='brilliant-credentials.txt' \
  --exclude='*.pyc' \
  --exclude='__pycache__' \
  ./ "$ALT_DIR/"

REPO_DIR="$(pwd)"
cd "$ALT_DIR"

echo "[smoke] running ./install.sh in ${ALT_DIR} (install #B)"
./install.sh \
  --admin-email smoke-alt@example.com \
  --force \
  --key-out "$ALT_KEY_FILE" \
  | tee "$ALT_SUMMARY_FILE"

B_PROJECT="$(grep '^COMPOSE_PROJECT_NAME=' ./.env | head -n 1 | cut -d'=' -f2-)"
if [ -z "$B_PROJECT" ]; then
  echo "[smoke] FAIL: install #B .env missing COMPOSE_PROJECT_NAME" >&2
  exit 32
fi
if [ "$A_PROJECT" = "$B_PROJECT" ]; then
  echo "[smoke] FAIL: install #A and #B share COMPOSE_PROJECT_NAME (${A_PROJECT}) — T-0259 regression" >&2
  exit 33
fi
echo "[smoke] install #B: project=${B_PROJECT} (distinct from #A: ${A_PROJECT})"

# Stack A's containers must not have been recreated. Compare sorted IDs
# before/after. Any mismatch means compose touched A's containers.
cd "$REPO_DIR"
A_IDS_AFTER="$(docker compose ps -q | sort | tr '\n' ' ')"
if [ "$A_IDS_BEFORE" != "$A_IDS_AFTER" ]; then
  echo "[smoke] FAIL: install #A's container IDs changed after install #B ran" >&2
  echo "[smoke] before: ${A_IDS_BEFORE}" >&2
  echo "[smoke] after:  ${A_IDS_AFTER}" >&2
  exit 34
fi
echo "[smoke] install #A containers intact (IDs unchanged)"

# Two distinct pgdata volumes must exist — one per project.
if ! docker volume ls --format '{{.Name}}' | grep -q "^${A_PROJECT}_pgdata$"; then
  echo "[smoke] FAIL: expected volume ${A_PROJECT}_pgdata in docker volume ls" >&2
  docker volume ls --format '{{.Name}}' | grep -E '_pgdata$' >&2 || true
  exit 35
fi
if ! docker volume ls --format '{{.Name}}' | grep -q "^${B_PROJECT}_pgdata$"; then
  echo "[smoke] FAIL: expected volume ${B_PROJECT}_pgdata in docker volume ls" >&2
  docker volume ls --format '{{.Name}}' | grep -E '_pgdata$' >&2 || true
  exit 36
fi

echo "[smoke] non-collision scenario PASS (two independent stacks)"
echo "[smoke] PASS (all scenarios)"
