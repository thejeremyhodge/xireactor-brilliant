#!/usr/bin/env bash
# xiReactor Brilliant — one-shot installer
# Zero-to-working-API on a fresh Mac or Linux box.
# See README.md for the canonical usage examples.

set -euo pipefail

# ---------- constants ----------

# Constants are consumed across phases; shellcheck can't see forward into
# function bodies that arrive in later tasks (T-0176/T-0177). Suppress the
# false-positive unused warnings at the declaration site.
readonly SCRIPT_VERSION="0.4.0"
readonly DEFAULT_CLONE_DIR="./xireactor-brilliant"
readonly REPO_SLUG="thejeremyhodge/xireactor-brilliant"
readonly API_URL="http://localhost:8010"
# shellcheck disable=SC2034
readonly MCP_URL="http://localhost:8011"
# shellcheck disable=SC2034
readonly PG_HOST_PORT="5442"
readonly LOG_FILE="./install.log"
readonly DEFAULT_KEY_OUT="./brilliant-credentials.txt"
readonly HEALTH_TIMEOUT_SECONDS=60
# shellcheck disable=SC2034
readonly VERIFY_TIMEOUT_SECONDS=30
# shellcheck disable=SC2034
readonly POLL_INTERVAL_SECONDS=2

# ---------- flag defaults ----------

ADMIN_EMAIL=""
ADMIN_PASSWORD=""
ADMIN_API_KEY=""
POSTGRES_PASSWORD=""
ANTHROPIC_API_KEY=""
KEY_OUT="${DEFAULT_KEY_OUT}"
FORCE=0
NO_INSTALL_DOCKER=0
DRY_RUN=0
MIGRATE_FROM_CORTEX=0
REF=""
REF_EXPLICIT=0
CLONE_DIR="${DEFAULT_CLONE_DIR}"

# ---------- logging ----------

log() {
  # $1 phase label, $2... message
  local phase="$1"; shift
  local msg="[$phase] $*"
  printf '%s\n' "$msg"
  if [ "$DRY_RUN" -eq 0 ]; then
    printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$msg" >>"$LOG_FILE"
  fi
}

die() {
  # $1 exit code, $2... message
  local code="$1"; shift
  printf 'ERROR: %s\n' "$*" >&2
  exit "$code"
}

mask() {
  # Print "****" for non-empty, "(unset)" for empty.
  if [ -z "${1:-}" ]; then
    printf '(unset)'
  else
    printf '****'
  fi
}

# ---------- randoms ----------

rand_hex() {
  # $1 byte count — openssl rand -hex gives 2*N hex chars
  openssl rand -hex "$1"
}

rand_password() {
  # URL-safe printable: base64 then strip padding/slashes/plus.
  # $1 byte count (resulting string length >= ~$1 chars).
  openssl rand -base64 "$1" | tr -d '=+/' | cut -c1-"$1"
}

# ---------- help ----------

print_help() {
  cat <<'HELP'
xiReactor Brilliant installer

Usage:
  install.sh [flags]

Required:
  --admin-email EMAIL        Admin user email (used for login + bootstrap).

Optional:
  --admin-password PW        Admin password. Random if unset.
  --admin-api-key KEY        Admin API key. Random (bkai_<hex>) if unset.
  --postgres-password PW     Postgres password. Random if unset.
  --anthropic-api-key KEY    Anthropic key for Tier 3 reviewer (opt-in).
  --key-out PATH             Write admin API key to PATH (mode 600).
                             Default: ./brilliant-credentials.txt
  --force                    Overwrite existing .env.
  --no-install-docker        Detect Docker only; fail if missing.
  --dry-run                  Print the 8-phase plan and exit 0.
  --migrate-from-cortex      Upgrade a pre-rename cortex-* stack in place:
                             rename the cortex DB to brilliant, tear down old
                             containers, and bring up the renamed stack with
                             the existing data volume preserved.
  --ref REF                  Git ref (tag, branch, or sha) to clone when the
                             installer is not already inside a brilliant repo.
                             Default: latest release tag, or `main` if the
                             release API is unreachable.
  --dir PATH                 Target directory for the self-clone. Default:
                             ./xireactor-brilliant. Ignored when the installer
                             is already inside a brilliant repo.
  -h, --help                 Show this message.

Examples:
  ./install.sh --admin-email you@example.com
  ./install.sh --admin-email you@example.com --key-out /tmp/key.txt
  ./install.sh --dry-run --admin-email test@x.com

HELP
}

# ---------- flag parsing ----------

parse_flags() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --admin-email)         ADMIN_EMAIL="${2:-}"; shift 2 ;;
      --admin-email=*)       ADMIN_EMAIL="${1#*=}"; shift ;;
      --admin-password)      ADMIN_PASSWORD="${2:-}"; shift 2 ;;
      --admin-password=*)    ADMIN_PASSWORD="${1#*=}"; shift ;;
      --admin-api-key)       ADMIN_API_KEY="${2:-}"; shift 2 ;;
      --admin-api-key=*)     ADMIN_API_KEY="${1#*=}"; shift ;;
      --postgres-password)   POSTGRES_PASSWORD="${2:-}"; shift 2 ;;
      --postgres-password=*) POSTGRES_PASSWORD="${1#*=}"; shift ;;
      --anthropic-api-key)   ANTHROPIC_API_KEY="${2:-}"; shift 2 ;;
      --anthropic-api-key=*) ANTHROPIC_API_KEY="${1#*=}"; shift ;;
      --key-out)             KEY_OUT="${2:-}"; shift 2 ;;
      --key-out=*)           KEY_OUT="${1#*=}"; shift ;;
      --force)               FORCE=1; shift ;;
      --no-install-docker)   NO_INSTALL_DOCKER=1; shift ;;
      --dry-run)             DRY_RUN=1; shift ;;
      --migrate-from-cortex) MIGRATE_FROM_CORTEX=1; shift ;;
      --ref)                 REF="${2:-}"; REF_EXPLICIT=1; shift 2 ;;
      --ref=*)               REF="${1#*=}"; REF_EXPLICIT=1; shift ;;
      --dir)                 CLONE_DIR="${2:-}"; shift 2 ;;
      --dir=*)               CLONE_DIR="${1#*=}"; shift ;;
      -h|--help)             print_help; exit 0 ;;
      *) die 64 "Unknown flag: $1 (try --help)" ;;
    esac
  done
}

# ---------- preflight ----------

require_bash4() {
  if [ -z "${BASH_VERSION:-}" ]; then
    die 65 "Not running under bash."
  fi
  local major="${BASH_VERSION%%.*}"
  if [ "$major" -lt 3 ]; then
    die 65 "bash 3.2+ required (got $BASH_VERSION)."
  fi
}

require_tool() {
  local tool="$1"
  command -v "$tool" >/dev/null 2>&1 || die 66 "Required tool not found: $tool"
}

detect_os() {
  case "$(uname -s)" in
    Darwin) printf 'mac' ;;
    Linux)  printf 'linux' ;;
    *)      die 67 "Unsupported OS: $(uname -s) (Mac and Linux only)" ;;
  esac
}

phase_preflight() {
  log "phase 1" "preflight"
  require_bash4
  require_tool openssl
  require_tool curl
  local os
  os="$(detect_os)"
  log "phase 1" "OS: $os, bash: $BASH_VERSION"
}

# ---------- randoms fill ----------

phase_randoms() {
  log "phase 4" "resolving randoms for unset values"
  : "${POSTGRES_PASSWORD:=$(rand_hex 24)}"
  : "${ADMIN_PASSWORD:=$(rand_password 24)}"
  : "${ADMIN_API_KEY:=bkai_$(rand_hex 24)}"
}

# ---------- phase stubs (filled in by later tasks) ----------

docker_present() {
  command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1
}

docker_compose_v2_present() {
  docker compose version >/dev/null 2>&1
}

install_docker_mac() {
  log "phase 2" "installing Docker on macOS via Colima"
  if ! command -v brew >/dev/null 2>&1; then
    die 70 "Homebrew not found. Install from https://brew.sh and rerun, or install Docker Desktop manually."
  fi
  log "phase 2" "brew install colima docker docker-compose"
  # NONINTERACTIVE=1 suppresses brew's prompts on first run.
  NONINTERACTIVE=1 brew install colima docker docker-compose 2>&1 | tee -a "$LOG_FILE"
  log "phase 2" "colima start --runtime docker"
  colima start --runtime docker 2>&1 | tee -a "$LOG_FILE"
}

install_docker_linux() {
  log "phase 2" "installing Docker on Linux via get.docker.com"
  local script
  script="$(mktemp -t get-docker.XXXXXX.sh)"
  # shellcheck disable=SC2064
  trap "rm -f '$script'" EXIT
  curl -fsSL https://get.docker.com -o "$script"
  sh "$script" 2>&1 | tee -a "$LOG_FILE"
  # Best-effort daemon start.
  if command -v systemctl >/dev/null 2>&1; then
    systemctl start docker 2>/dev/null || \
      log "phase 2" "systemctl start docker failed — may need 'sudo systemctl start docker'"
  else
    log "phase 2" "no systemctl; ensure the docker daemon is running manually"
  fi
}

phase_docker() {
  log "phase 2" "docker detect"
  if docker_present; then
    log "phase 2" "docker already installed and responsive"
  else
    if [ "$NO_INSTALL_DOCKER" -eq 1 ]; then
      die 71 "Docker not found and --no-install-docker is set. Install Docker and rerun, or drop the flag."
    fi
    local os
    os="$(detect_os)"
    case "$os" in
      mac)   install_docker_mac ;;
      linux) install_docker_linux ;;
    esac
    if ! docker_present; then
      die 72 "Docker install completed but 'docker info' still fails. Check $LOG_FILE and rerun."
    fi
  fi

  if ! docker_compose_v2_present; then
    die 73 "docker compose V2 not available. Upgrade Docker (>=20.10) and rerun."
  fi
  log "phase 2" "docker + compose V2 ready"
}

in_brilliant_repo() {
  [ -f "./docker-compose.yml" ] && [ -f "./.env.sample" ]
}

resolve_latest_release_tag() {
  # Fetches "tag_name" from GitHub's latest-release API. Prints the tag on
  # stdout, or nothing on failure. No jq dep — grep + cut parse.
  local api="https://api.github.com/repos/${REPO_SLUG}/releases/latest"
  curl -fsSL --max-time 10 "$api" 2>/dev/null \
    | grep -m1 '"tag_name"' \
    | cut -d'"' -f4 \
    || true
}

phase_self_clone() {
  # Skip entirely when already inside a brilliant repo — preserves in-place
  # behavior for maintainers and pre-cloned users.
  if in_brilliant_repo; then
    log "phase 1b" "already inside a brilliant repo — running in place"
    return 0
  fi

  # Resolve ref. If the user passed --ref, honor it verbatim. Otherwise try
  # the releases API and fall back to `main` on any failure.
  if [ "$REF_EXPLICIT" -eq 0 ]; then
    local resolved
    resolved="$(resolve_latest_release_tag)"
    if [ -n "$resolved" ]; then
      REF="$resolved"
      log "phase 1b" "resolved --ref to ${REF} (latest release)"
    else
      REF="main"
      log "phase 1b" "release API unreachable or empty — falling back to ref 'main'"
    fi
  else
    log "phase 1b" "using user-supplied ref: ${REF}"
  fi

  require_tool git

  # Guard the target directory. If it already exists and is non-empty, abort
  # unless --force was passed (--force is already the opt-in for overwriting).
  if [ -e "$CLONE_DIR" ]; then
    if [ ! -d "$CLONE_DIR" ]; then
      die 81 "--dir target exists and is not a directory: ${CLONE_DIR}"
    fi
    if [ -n "$(ls -A "$CLONE_DIR" 2>/dev/null)" ] && [ "$FORCE" -eq 0 ]; then
      die 81 "--dir target ${CLONE_DIR} is non-empty. Remove it, pick another --dir, or pass --force."
    fi
  fi

  log "phase 1b" "git clone --depth 1 --branch ${REF} https://github.com/${REPO_SLUG}.git ${CLONE_DIR}"
  if [ "$DRY_RUN" -eq 0 ]; then
    # If --force and the dir exists, clear it first so `git clone` doesn't balk.
    if [ -d "$CLONE_DIR" ] && [ "$FORCE" -eq 1 ]; then
      rm -rf "$CLONE_DIR"
    fi
    if ! git clone --depth 1 --branch "$REF" \
        "https://github.com/${REPO_SLUG}.git" "$CLONE_DIR" 2>&1 | tee -a "$LOG_FILE"; then
      die 82 "git clone of ${REPO_SLUG}@${REF} into ${CLONE_DIR} failed. See ${LOG_FILE}."
    fi
    cd "$CLONE_DIR"
    log "phase 1b" "cd $(pwd)"
  fi

  # Verify the cloned tree really is a brilliant repo.
  if [ "$DRY_RUN" -eq 0 ] && ! in_brilliant_repo; then
    die 83 "cloned tree at ${CLONE_DIR} is missing docker-compose.yml or .env.sample — wrong ref?"
  fi
}

phase_repo() {
  log "phase 3" "repo presence (inside $(pwd))"
}

set_env_var() {
  # $1 key, $2 value, $3 file. Replaces an existing KEY= line (commented or
  # not), or appends if absent. Portable across GNU/BSD sed by using pure bash.
  local key="$1" value="$2" file="$3"
  local tmp; tmp="$(mktemp)"
  local found=0
  # Match "KEY=" and "# KEY=" (with optional surrounding whitespace).
  local pattern="^[[:space:]]*#?[[:space:]]*${key}="
  while IFS= read -r line || [ -n "$line" ]; do
    if [[ "$line" =~ $pattern ]]; then
      printf '%s=%s\n' "$key" "$value" >>"$tmp"
      found=1
    else
      printf '%s\n' "$line" >>"$tmp"
    fi
  done <"$file"
  if [ "$found" -eq 0 ]; then
    printf '%s=%s\n' "$key" "$value" >>"$tmp"
  fi
  mv "$tmp" "$file"
}

phase_env() {
  log "phase 5" "env generation"
  if [ -f "./.env" ] && [ "$FORCE" -eq 0 ]; then
    die 2 ".env already exists — rerun with --force to overwrite (a dedicated --upgrade flow is deferred)"
  fi
  if [ ! -f "./.env.sample" ]; then
    die 74 ".env.sample not found at repo root — is this the brilliant repo?"
  fi
  cp "./.env.sample" "./.env"

  set_env_var POSTGRES_PASSWORD "$POSTGRES_PASSWORD" "./.env"
  set_env_var ADMIN_EMAIL       "$ADMIN_EMAIL"       "./.env"
  set_env_var ADMIN_PASSWORD    "$ADMIN_PASSWORD"    "./.env"
  set_env_var ADMIN_API_KEY     "$ADMIN_API_KEY"     "./.env"
  if [ -n "$ANTHROPIC_API_KEY" ]; then
    set_env_var ANTHROPIC_API_KEY "$ANTHROPIC_API_KEY" "./.env"
  fi

  chmod 600 "./.env"
  log "phase 5" ".env generated at ./.env (mode 600)"
}

phase_up() {
  log "phase 6" "docker compose up -d"
  docker compose up -d 2>&1 | tee -a "$LOG_FILE"

  log "phase 6" "polling ${API_URL}/health (timeout ${HEALTH_TIMEOUT_SECONDS}s)"
  local waited=0
  while [ "$waited" -lt "$HEALTH_TIMEOUT_SECONDS" ]; do
    if curl -fsS "${API_URL}/health" >/dev/null 2>&1; then
      log "phase 6" "API healthy at ${API_URL}"
      return 0
    fi
    sleep "$POLL_INTERVAL_SECONDS"
    waited=$((waited + POLL_INTERVAL_SECONDS))
  done

  log "phase 6" "health timeout after ${HEALTH_TIMEOUT_SECONDS}s — dumping api logs"
  docker compose logs api --tail 50 2>&1 | tee -a "$LOG_FILE" || true
  die 3 "API did not become healthy within ${HEALTH_TIMEOUT_SECONDS}s. See ${LOG_FILE}."
}

phase_verify() {
  log "phase 7" "verifying admin API key against ${API_URL}/entries"
  local waited=0
  local http_code
  while [ "$waited" -lt "$VERIFY_TIMEOUT_SECONDS" ]; do
    http_code="$(curl -s -o /dev/null -w '%{http_code}' \
      -H "Authorization: Bearer ${ADMIN_API_KEY}" \
      "${API_URL}/entries" || true)"
    if [ "$http_code" = "200" ]; then
      log "phase 7" "admin key verified (HTTP 200 on /entries)"
      return 0
    fi
    sleep "$POLL_INTERVAL_SECONDS"
    waited=$((waited + POLL_INTERVAL_SECONDS))
  done

  log "phase 7" "admin verify failed after ${VERIFY_TIMEOUT_SECONDS}s (last status: ${http_code:-n/a})"
  docker compose logs api --tail 50 2>&1 | tee -a "$LOG_FILE" || true
  die 4 "Admin API key did not authenticate within ${VERIFY_TIMEOUT_SECONDS}s. See ${LOG_FILE}."
}

write_key_out() {
  local path="$1"
  local dir
  dir="$(dirname "$path")"
  if [ ! -d "$dir" ]; then
    die 75 "key-out directory does not exist: $dir"
  fi
  printf '%s\n' "$ADMIN_API_KEY" >"$path"
  chmod 600 "$path"
}

phase_summary() {
  log "phase 8" "writing admin key to ${KEY_OUT}"
  write_key_out "$KEY_OUT"

  # The banner prints to stdout only (not the log) — this is what Jeremy reads
  # at the end of a successful run.
  cat <<BANNER

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  xiReactor Brilliant installed successfully
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  API:           ${API_URL}
  Health:        ${API_URL}/health
  MCP:           ${MCP_URL}
  Postgres:      localhost:${PG_HOST_PORT}

  Admin email:   ${ADMIN_EMAIL}
  Admin key:     ${ADMIN_API_KEY}
  Key file:      ${KEY_OUT} (mode 600)

  Next steps:
    curl -H "Authorization: Bearer \$(cat ${KEY_OUT})" ${API_URL}/entries
    See README.md → "Connect Claude" to wire up the MCP.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

BANNER
}

# ---------- migration: cortex → brilliant ----------

container_running() {
  # $1 container name. Returns 0 if running, non-zero otherwise.
  local name="$1"
  local state
  state="$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || true)"
  [ "$state" = "true" ]
}

run_cmd() {
  # Print the command; execute only when DRY_RUN=0.
  # Usage: run_cmd docker stop cortex-api
  log "migrate" "\$ $*"
  if [ "$DRY_RUN" -eq 0 ]; then
    "$@"
  fi
}

phase_migrate_from_cortex() {
  log "migrate" "upgrade path: cortex-* → brilliant-*"

  # Step 1: detect. Three cases to disambiguate:
  #   - cortex-db running                  → migrate
  #   - brilliant-db running, no cortex-db → already migrated, exit 0
  #   - neither running                    → nothing to migrate, exit 1
  local has_cortex_db=0
  local has_brilliant_db=0
  if container_running cortex-db; then has_cortex_db=1; fi
  if container_running brilliant-db; then has_brilliant_db=1; fi

  if [ "$has_cortex_db" -eq 0 ] && [ "$has_brilliant_db" -eq 1 ]; then
    log "migrate" "brilliant-db is already running and cortex-db is not — already migrated."
    exit 0
  fi
  if [ "$has_cortex_db" -eq 0 ] && [ "$has_brilliant_db" -eq 0 ]; then
    die 76 "neither cortex-db nor brilliant-db is running — nothing to migrate. Run ./install.sh for a fresh install."
  fi
  if [ "$has_cortex_db" -eq 0 ]; then
    log "migrate" "no cortex-* stack detected; nothing to migrate"
    exit 0
  fi

  log "migrate" "detected running cortex-db — proceeding with migration"

  # Step 2: quiesce writers. Stop cortex-api and cortex-mcp if present so the
  # ALTER DATABASE below doesn't trip on live connections. Ignore failures for
  # containers that don't exist; surface them only if the rename later fails.
  log "migrate" "step 2: stop cortex-api and cortex-mcp (quiesce writers)"
  if container_running cortex-api; then
    run_cmd docker stop cortex-api
  else
    log "migrate" "cortex-api not running — skipping stop"
  fi
  if container_running cortex-mcp; then
    run_cmd docker stop cortex-mcp
  else
    log "migrate" "cortex-mcp not running — skipping stop"
  fi

  # Step 3: rename the database. Any live connection to the `cortex` DB will
  # cause this to fail — we surface a clear error in that case.
  log "migrate" "step 3: ALTER DATABASE cortex RENAME TO brilliant"
  if [ "$DRY_RUN" -eq 0 ]; then
    if ! docker exec cortex-db psql -U postgres -c \
        "ALTER DATABASE cortex RENAME TO brilliant" 2>&1 | tee -a "$LOG_FILE"; then
      log "migrate" "rename failed — checking for lingering connections"
      docker ps --filter "name=cortex-" --format '  still up: {{.Names}}' \
        2>&1 | tee -a "$LOG_FILE" || true
      die 77 "ALTER DATABASE cortex RENAME TO brilliant failed. See ${LOG_FILE}. Migration aborted before teardown; cortex-db is still intact."
    fi
  else
    log "migrate" "\$ docker exec cortex-db psql -U postgres -c 'ALTER DATABASE cortex RENAME TO brilliant'"
  fi

  # Step 4: tear down old cortex containers. Remove by explicit name so we
  # don't depend on a prior compose file being present on disk. The Postgres
  # data volume (pgdata) is unaffected — the renamed `brilliant` database
  # lives inside it and is mounted by brilliant-db in step 5.
  log "migrate" "step 4: remove old cortex containers (data volume preserved)"
  run_cmd docker rm -f cortex-db cortex-api cortex-mcp

  # Step 5: bring up the renamed stack. docker-compose.yml on this branch
  # already names the containers brilliant-*.
  log "migrate" "step 5: docker compose up -d --build (brilliant-*)"
  if [ "$DRY_RUN" -eq 0 ]; then
    docker compose up -d --build 2>&1 | tee -a "$LOG_FILE"
  else
    log "migrate" "\$ docker compose up -d --build"
  fi

  # Step 6: verify health + the renamed DB is visible.
  log "migrate" "step 6: verify brilliant-api /health and brilliant DB exists"
  if [ "$DRY_RUN" -eq 0 ]; then
    local waited=0
    while [ "$waited" -lt "$HEALTH_TIMEOUT_SECONDS" ]; do
      if curl -fsS "${API_URL}/health" >/dev/null 2>&1; then
        log "migrate" "brilliant-api healthy at ${API_URL}"
        break
      fi
      sleep "$POLL_INTERVAL_SECONDS"
      waited=$((waited + POLL_INTERVAL_SECONDS))
    done
    if [ "$waited" -ge "$HEALTH_TIMEOUT_SECONDS" ]; then
      docker compose logs api --tail 50 2>&1 | tee -a "$LOG_FILE" || true
      die 78 "brilliant-api did not become healthy within ${HEALTH_TIMEOUT_SECONDS}s after migration. See ${LOG_FILE}."
    fi
    if ! docker exec brilliant-db psql -U postgres -lqt | grep -q '\bbrilliant\b'; then
      die 79 "brilliant database not found inside brilliant-db after migration. See ${LOG_FILE}."
    fi
    log "migrate" "brilliant database present in brilliant-db"
  else
    log "migrate" "\$ curl -fsS ${API_URL}/health  (poll up to ${HEALTH_TIMEOUT_SECONDS}s)"
    log "migrate" "\$ docker exec brilliant-db psql -U postgres -lqt | grep brilliant"
  fi

  # Step 7: summary.
  log "migrate" "step 7: summary"
  cat <<MIGRATED

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Migrated cortex → brilliant. Data preserved.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  New containers: brilliant-db, brilliant-api, brilliant-mcp
  Database:       brilliant (renamed from cortex, same data volume)
  API:            ${API_URL}

  Your existing .env values (POSTGRES_PASSWORD, ADMIN_*) are unchanged.
  If you previously hard-coded POSTGRES_DB=cortex in .env, update it to
  POSTGRES_DB=brilliant to match the renamed database.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

MIGRATED
}

# ---------- dry-run plan ----------

print_plan() {
  cat <<PLAN
xiReactor Brilliant installer — dry-run plan (v${SCRIPT_VERSION})

Resolved configuration:
  admin-email:        ${ADMIN_EMAIL:-(required — none)}
  admin-password:     $(mask "${ADMIN_PASSWORD}")
  admin-api-key:      $(mask "${ADMIN_API_KEY}")
  postgres-password:  $(mask "${POSTGRES_PASSWORD}")
  anthropic-api-key:  $(mask "${ANTHROPIC_API_KEY}")
  key-out:            ${KEY_OUT}
  force:              ${FORCE}
  no-install-docker:  ${NO_INSTALL_DOCKER}
  ref:                ${REF:-(latest release tag; fallback main)}
  dir:                ${CLONE_DIR}

Planned phases:
  [phase 1]  preflight   — OS detect, bash 3.2+, openssl + curl present
  [phase 1b] self-clone  — if not inside a brilliant repo, git clone --ref into --dir and cd in
  [phase 2]  docker      — detect; install Colima (Mac) or get.docker.com (Linux) if missing
  [phase 3]  repo        — confirm we're inside the brilliant repo (docker-compose.yml present)
  [phase 4]  randoms     — fill unset secrets via openssl rand
  [phase 5]  env         — write ./.env from .env.sample (mode 600); refuse overwrite without --force
  [phase 6]  up          — docker compose up -d, poll ${API_URL}/health for up to ${HEALTH_TIMEOUT_SECONDS}s
  [phase 7]  verify      — GET ${API_URL}/entries with admin key to confirm bootstrap
  [phase 8]  summary     — print banner, write key to ${KEY_OUT} (mode 600)

PLAN
}

# ---------- main ----------

main() {
  parse_flags "$@"

  # --migrate-from-cortex is a dedicated upgrade path and does not require
  # --admin-email — the admin user already exists in the preserved DB.
  if [ "$MIGRATE_FROM_CORTEX" -eq 0 ] && [ -z "$ADMIN_EMAIL" ]; then
    die 64 "--admin-email is required (try --help)"
  fi

  if [ "$MIGRATE_FROM_CORTEX" -eq 1 ]; then
    # Log file: fresh for a real run, left alone for --dry-run.
    if [ "$DRY_RUN" -eq 0 ]; then
      : >"$LOG_FILE"
    fi
    log "phase 0" "install.sh v${SCRIPT_VERSION} --migrate-from-cortex starting"
    phase_preflight
    # Docker must already be present to have a cortex-* stack at all; detect
    # only. We don't want to install or restart Docker as part of migration.
    if ! docker_present; then
      die 80 "docker not available — cannot detect or migrate an existing cortex-* stack"
    fi
    if ! docker_compose_v2_present; then
      die 73 "docker compose V2 not available. Upgrade Docker (>=20.10) and rerun."
    fi
    phase_migrate_from_cortex
    exit 0
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    # Fill resolved randoms in memory so the plan masks sensibly — silently.
    : "${POSTGRES_PASSWORD:=$(rand_hex 24)}"
    : "${ADMIN_PASSWORD:=$(rand_password 24)}"
    : "${ADMIN_API_KEY:=bkai_$(rand_hex 24)}"
    print_plan
    exit 0
  fi

  # Start log
  : >"$LOG_FILE"
  log "phase 0" "install.sh v${SCRIPT_VERSION} starting"

  phase_preflight
  phase_self_clone
  phase_docker
  phase_repo
  phase_randoms
  phase_env
  phase_up
  phase_verify
  phase_summary
}

main "$@"
