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

echo "[smoke] asserting /entries with the admin key is 200"
key="$(cat "$KEY_FILE")"
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

echo "[smoke] asserting key file is one line"
lines="$(wc -l <"$KEY_FILE")"
if [ "$lines" -ne 1 ]; then
  echo "[smoke] FAIL: key file has $lines lines, expected 1" >&2
  exit 14
fi

echo "[smoke] PASS (${ELAPSED}s, under the 300s budget)"
