#!/usr/bin/env bash
# xiReactor Brilliant — one-shot installer
# Zero-to-working-API on a fresh Mac or Linux box.
# See README.md for the canonical usage examples.

set -euo pipefail

# ---------- constants ----------

# Constants are consumed across phases; shellcheck can't see forward into
# function bodies that arrive in later tasks (T-0176/T-0177). Suppress the
# false-positive unused warnings at the declaration site.
readonly SCRIPT_VERSION="0.5.1"
readonly DEFAULT_CLONE_DIR="./xireactor-brilliant"
readonly REPO_SLUG="thejeremyhodge/xireactor-brilliant"
# Default host ports — overridden by phase_port_probe if occupied. These
# are the ports advertised in README/docs; single-install flows on a clean
# machine preserve them verbatim. Sprint 0043 T-0257.
readonly DEFAULT_DB_PORT=5442
readonly DEFAULT_API_PORT=8010
readonly DEFAULT_MCP_PORT=8011
# Chosen ports (populated by phase_port_probe). Kept as non-readonly so
# the probe can mutate them in place. URLs below are derived from these.
DB_HOST_PORT="${DEFAULT_DB_PORT}"
API_HOST_PORT="${DEFAULT_API_PORT}"
MCP_HOST_PORT="${DEFAULT_MCP_PORT}"
# URLs re-derived after phase_port_probe. (Legacy PG_HOST_PORT alias
# was removed in T-0257 — every call site now reads DB_HOST_PORT.)
API_URL="http://localhost:${API_HOST_PORT}"
MCP_URL="http://localhost:${MCP_HOST_PORT}"
readonly LOG_FILE="./install.log"
# Sprint 0044 T-0260 — installer no longer writes a credentials file by
# default. The path below is only used by the headless-with-admin branch
# (`--admin-email` + `--admin-password`), where `fetch_credentials_file`
# curls `GET /credentials` after admin bootstrap and writes the six-field
# block alongside the installer.
readonly CREDENTIALS_FILE="./brilliant-credentials.txt"
readonly HEALTH_TIMEOUT_SECONDS=60
readonly POLL_INTERVAL_SECONDS=2
# Window for the post-health admin-bootstrap to flush credentials into the
# DB. `phase_up` returns as soon as `/health` is green, but the bootstrap
# task can land a couple of seconds later — `fetch_credentials_file`
# retries `GET /credentials` over this window before giving up.
readonly CREDENTIALS_FETCH_TIMEOUT_SECONDS=15
# Port-probe tuning: step size and maximum number of attempts per port.
# 5 attempts at +10 each → 5442→5452→5462→5472→5482, then error out.
readonly PORT_PROBE_STEP=10
readonly PORT_PROBE_MAX_TRIES=5

# ---------- flag defaults ----------

ADMIN_EMAIL=""
ADMIN_PASSWORD=""
ADMIN_PASSWORD_FROM_ARGV=0
ADMIN_API_KEY=""
POSTGRES_PASSWORD=""
ANTHROPIC_API_KEY=""
FORCE=0
NO_INSTALL_DOCKER=0
DRY_RUN=0
MIGRATE_FROM_CORTEX=0
SEED_DEMO=0
HEADLESS=0
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

# ---------- compose project naming (Sprint 0043 T-0259) ----------

compute_compose_project_name() {
  # Deterministic compose project name derived from the install dir's
  # absolute path. Fixes the T-0259 collision: compose's default project
  # name is the parent-dir basename, so every self-clone into the default
  # `./xireactor-brilliant` dir resolved to the same project `xireactor-
  # brilliant`. Two such installs on one host would share container names
  # and the `pgdata` volume prefix, causing `docker compose up` to
  # recreate the sibling stack against a mismatched `.env` password.
  #
  # The hash is sha256 truncated to 8 hex chars, derived via openssl
  # (already a hard dep) so we don't rely on `sha1sum` / `md5sum` (GNU-
  # only) or `shasum` (mac-only).
  local path hash
  path="$(pwd)"
  hash="$(printf '%s' "$path" | openssl dgst -sha256 | awk '{print $NF}' | cut -c1-8)"
  printf 'brilliant-%s' "$hash"
}

# ---------- port probing (Sprint 0043 T-0257) ----------

port_in_use() {
  # Returns 0 (true) if TCP port $1 on localhost is occupied, 1 otherwise.
  # Primary: `lsof -i :$port` (standard on Mac + most Linux distros).
  # Fallback: `nc -z localhost $port` (BusyBox / slim containers).
  # Both are silent on success/failure; we only care about the exit code.
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -i ":${port}" -sTCP:LISTEN >/dev/null 2>&1
    return $?
  fi
  if command -v nc >/dev/null 2>&1; then
    nc -z localhost "${port}" >/dev/null 2>&1
    return $?
  fi
  # No probe tool available — assume the port is free. Better to try the
  # bind than to spuriously fail before docker compose ever runs.
  return 1
}

probe_port() {
  # Given a "friendly name" $1 and a starting port $2, return the first
  # free port in the sequence start, start+10, start+20, ... up to
  # PORT_PROBE_MAX_TRIES attempts. Prints the chosen port to stdout.
  # Fails (exit non-zero) if every candidate in range is occupied.
  local label="$1"
  local start="$2"
  local attempt=0
  local port="$start"
  while [ "$attempt" -lt "$PORT_PROBE_MAX_TRIES" ]; do
    if ! port_in_use "$port"; then
      printf '%d' "$port"
      return 0
    fi
    attempt=$((attempt + 1))
    port=$((start + attempt * PORT_PROBE_STEP))
  done
  die 86 "port-probe: all ${PORT_PROBE_MAX_TRIES} candidate ${label} ports occupied starting at ${start} (step ${PORT_PROBE_STEP}). Free a port or re-run after closing the conflicting service."
}

phase_port_probe() {
  # Pick host ports for db/api/mcp — default values unless occupied. On a
  # clean machine with nothing else bound, this is a no-op (each probe
  # returns the default immediately) so single-install flows stay
  # byte-identical to pre-0043 behavior.
  log "phase 4b" "probing host ports (db=${DEFAULT_DB_PORT}, api=${DEFAULT_API_PORT}, mcp=${DEFAULT_MCP_PORT})"
  DB_HOST_PORT="$(probe_port db "$DEFAULT_DB_PORT")"
  API_HOST_PORT="$(probe_port api "$DEFAULT_API_PORT")"
  MCP_HOST_PORT="$(probe_port mcp "$DEFAULT_MCP_PORT")"

  # Re-derive downstream URLs so everything (health poll, verify,
  # banner, credentials file) reads the chosen values.
  API_URL="http://localhost:${API_HOST_PORT}"
  MCP_URL="http://localhost:${MCP_HOST_PORT}"

  if [ "$DB_HOST_PORT" != "$DEFAULT_DB_PORT" ] \
      || [ "$API_HOST_PORT" != "$DEFAULT_API_PORT" ] \
      || [ "$MCP_HOST_PORT" != "$DEFAULT_MCP_PORT" ]; then
    log "phase 4b" "port conflict resolved: db=${DB_HOST_PORT}, api=${API_HOST_PORT}, mcp=${MCP_HOST_PORT}"
  else
    log "phase 4b" "all default ports free"
  fi
}

# ---------- help ----------

print_help() {
  cat <<'HELP'
xiReactor Brilliant installer

Usage:
  install.sh [flags]

Default (browser ceremony, no flags):
  Stand up the stack, open /setup in your browser, exit. You complete
  the form in the browser and download brilliant-credentials.txt from
  the response page. No credentials file is written by the installer.

Optional:
  --headless, --no-browser   Skip the browser auto-open. Print the /setup
                             URL with an SSH-tunnel hint so a VPS operator
                             can complete the ceremony from their workstation
                             over a forwarded port.
  --admin-email EMAIL        Headless install (VPS / CI / scripted). When set,
                             the installer writes ADMIN_EMAIL to .env, the
                             admin user is bootstrapped on API boot, and after
                             health-check the installer auto-writes
                             ./brilliant-credentials.txt by curling
                             /credentials with the minted admin key.
                             Implies --headless. Pair with --admin-password.
  --admin-password PW        Admin password for the headless path. When omitted
                             alongside --admin-email on a TTY, the installer
                             prompts for it interactively (read -s, double-entry
                             confirm). Passing it on argv triggers a stderr
                             warning — the value is visible in `ps` and shell
                             history. Use the interactive form unless scripting.
  --admin-api-key KEY        Admin API key. Random (bkai_<hex>) if unset.
                             Only consumed on the headless-with-admin path.
  --postgres-password PW     Postgres password. Random if unset.
  --anthropic-api-key KEY    Anthropic key for Tier 3 reviewer (opt-in).
  --force                    Overwrite existing .env.
  --no-install-docker        Detect Docker only; fail if missing.
  --dry-run                  Print the install plan and exit 0.
  --migrate-from-cortex      Upgrade a pre-rename cortex-* stack in place:
                             rename the cortex DB to brilliant, tear down old
                             containers, and bring up the renamed stack with
                             the existing data volume preserved.
  --seed-demo                Opt in to the demo dataset (4 demo users, 12
                             entries, links/versions/staging/audit rows).
                             Applied via `docker exec` after the stack is
                             healthy. Every seeded entry carries the
                             `demo:seed` tag; remove later with
                             `python tools/remove_demo_data.py --yes`.
  --ref REF                  Git ref (tag, branch, or sha) to clone when the
                             installer is not already inside a brilliant repo.
                             Default: latest release tag, or `main` if the
                             release API is unreachable.
  --dir PATH                 Target directory for the self-clone. Default:
                             ./xireactor-brilliant. Ignored when the installer
                             is already inside a brilliant repo.
  -h, --help                 Show this message.

Examples:
  # Default — browser opens to /setup.
  ./install.sh

  # Headless VPS, SSH-tunnel persona — installer prints the /setup URL.
  ./install.sh --headless

  # Headless scripted — installer writes brilliant-credentials.txt on its own.
  ./install.sh --admin-email you@example.com --admin-password 's3cret!'

  # Headless interactive — same as above but prompts for the password.
  ./install.sh --admin-email you@example.com

HELP
}

# ---------- flag parsing ----------

parse_flags() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --admin-email)         ADMIN_EMAIL="${2:-}"; shift 2 ;;
      --admin-email=*)       ADMIN_EMAIL="${1#*=}"; shift ;;
      --admin-password)      ADMIN_PASSWORD="${2:-}"; ADMIN_PASSWORD_FROM_ARGV=1; shift 2 ;;
      --admin-password=*)    ADMIN_PASSWORD="${1#*=}"; ADMIN_PASSWORD_FROM_ARGV=1; shift ;;
      --admin-api-key)       ADMIN_API_KEY="${2:-}"; shift 2 ;;
      --admin-api-key=*)     ADMIN_API_KEY="${1#*=}"; shift ;;
      --postgres-password)   POSTGRES_PASSWORD="${2:-}"; shift 2 ;;
      --postgres-password=*) POSTGRES_PASSWORD="${1#*=}"; shift ;;
      --anthropic-api-key)   ANTHROPIC_API_KEY="${2:-}"; shift 2 ;;
      --anthropic-api-key=*) ANTHROPIC_API_KEY="${1#*=}"; shift ;;
      --headless|--no-browser) HEADLESS=1; shift ;;
      --force)               FORCE=1; shift ;;
      --no-install-docker)   NO_INSTALL_DOCKER=1; shift ;;
      --dry-run)             DRY_RUN=1; shift ;;
      --migrate-from-cortex) MIGRATE_FROM_CORTEX=1; shift ;;
      --seed-demo)           SEED_DEMO=1; shift ;;
      --ref)                 REF="${2:-}"; REF_EXPLICIT=1; shift 2 ;;
      --ref=*)               REF="${1#*=}"; REF_EXPLICIT=1; shift ;;
      --dir)                 CLONE_DIR="${2:-}"; shift 2 ;;
      --dir=*)               CLONE_DIR="${1#*=}"; shift ;;
      -h|--help)             print_help; exit 0 ;;
      *) die 64 "Unknown flag: $1 (try --help)" ;;
    esac
  done
}

# ---------- admin-flag validation + interactive password (Sprint 0044 T-0260) ----------

validate_admin_flags() {
  # Reject obvious nonsense before we touch the filesystem:
  #   --admin-password without --admin-email is meaningless (no admin to
  #   own the password) and tends to mask a typo.
  if [ -z "$ADMIN_EMAIL" ] && [ -n "$ADMIN_PASSWORD" ]; then
    die 64 "--admin-password requires --admin-email (no admin user to bind it to)"
  fi
}

prompt_admin_password() {
  # Headless-with-admin path. Only fires when --admin-email is set.
  #   - If --admin-password was passed on argv: emit a one-line stderr
  #     warning ("visible in ps / shell history — prefer interactive
  #     entry") and return. CI escape valve.
  #   - Else if stdin is a TTY: read -s twice with a confirm. Mismatch
  #     dies (exit 64) without echoing either attempt.
  #   - Else (non-TTY, no argv password): die with a clear message
  #     telling the caller to pass --admin-password (or run on a TTY).
  if [ -z "$ADMIN_EMAIL" ]; then
    return 0
  fi
  if [ "$ADMIN_PASSWORD_FROM_ARGV" -eq 1 ]; then
    printf 'WARNING: --admin-password on argv is visible in ps/shell history; prefer interactive entry.\n' >&2
    return 0
  fi
  if [ ! -t 0 ]; then
    die 64 "--admin-email passed but --admin-password is missing and stdin is not a TTY. Pass --admin-password explicitly or run interactively."
  fi
  local p1 p2
  printf 'Admin password: ' >&2
  IFS= read -rs p1 || die 64 "failed to read admin password"
  printf '\n' >&2
  printf 'Confirm:        ' >&2
  IFS= read -rs p2 || die 64 "failed to read admin password confirmation"
  printf '\n' >&2
  if [ -z "$p1" ]; then
    die 64 "admin password must not be empty"
  fi
  if [ "$p1" != "$p2" ]; then
    die 64 "admin passwords did not match"
  fi
  ADMIN_PASSWORD="$p1"
}

# ---------- browser-open + credentials fetch (Sprint 0044 T-0260) ----------

open_browser() {
  # Best-effort browser open for the default ceremony path. Never fails the
  # installer — if neither `open` (mac) nor `xdg-open` (linux) is present
  # (e.g. bare-shell VPS that the user forgot to flag --headless on), the
  # caller's URL-print fallback is the recovery surface.
  local url="$1"
  if command -v open >/dev/null 2>&1; then
    open "$url" >/dev/null 2>&1 &
    return 0
  fi
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 &
    return 0
  fi
  return 1
}

fetch_credentials_file() {
  # Headless-with-admin only. After admin bootstrap has flushed credentials
  # to the DB, curl `GET /credentials` with the minted admin key and write
  # the six-field shape to ./brilliant-credentials.txt (mode 600). Retries
  # over CREDENTIALS_FETCH_TIMEOUT_SECONDS to tolerate slow bootstrap (the
  # /health probe in phase_up is satisfied before lifespan finishes).
  local path="$CREDENTIALS_FILE"
  local waited=0
  local tmp
  tmp="$(mktemp)"
  # shellcheck disable=SC2064
  trap "rm -f '$tmp'" RETURN
  while [ "$waited" -lt "$CREDENTIALS_FETCH_TIMEOUT_SECONDS" ]; do
    if curl -fsS \
        -H "Authorization: Bearer ${ADMIN_API_KEY}" \
        -H "Accept: application/json" \
        "${API_URL}/credentials" -o "$tmp" 2>/dev/null; then
      # Re-format JSON → six `key=value` lines. No jq dep — grep + sed
      # parse works because the response body is a flat object emitted by
      # FastAPI's JSONResponse.
      {
        for k in admin_email admin_api_key oauth_client_id oauth_client_secret mcp_url login_url; do
          local v
          v="$(grep -oE "\"${k}\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" "$tmp" | sed -E "s/.*\"${k}\"[[:space:]]*:[[:space:]]*\"([^\"]*)\".*/\1/")"
          printf '%s=%s\n' "$k" "$v"
        done
      } >"$path"
      chmod 600 "$path"
      log "phase 8" "credentials written to ${path} (mode 600, six fields)"
      return 0
    fi
    sleep "$POLL_INTERVAL_SECONDS"
    waited=$((waited + POLL_INTERVAL_SECONDS))
  done

  # Don't fail the installer — admin is up, the user just has to recover
  # the file manually. Print the recovery snippet so they have it to hand.
  log "phase 8" "WARN: /credentials fetch did not succeed within ${CREDENTIALS_FETCH_TIMEOUT_SECONDS}s"
  cat >&2 <<RECOVER
WARNING: failed to write ${CREDENTIALS_FILE}. The stack is healthy and the
admin user is bootstrapped. To re-fetch the credentials block manually:

  curl -H "Authorization: Bearer ${ADMIN_API_KEY}" \\
       ${API_URL}/credentials > ${CREDENTIALS_FILE}
  chmod 600 ${CREDENTIALS_FILE}

RECOVER
  return 1
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
  # POSTGRES_PASSWORD is always randomized — compose needs it on every path
  # and it's not user-facing. ADMIN_API_KEY is minted only on the headless-
  # with-admin path so `fetch_credentials_file` can curl /credentials with
  # a known Bearer; on the default browser-ceremony path the key is minted
  # by the API at /setup time and delivered to the browser. ADMIN_PASSWORD
  # is never auto-generated — `prompt_admin_password` already required the
  # operator to provide one if they're on the headless-with-admin path.
  log "phase 4" "resolving randoms for unset values"
  : "${POSTGRES_PASSWORD:=$(rand_hex 24)}"
  if [ -n "$ADMIN_EMAIL" ]; then
    : "${ADMIN_API_KEY:=bkai_$(rand_hex 24)}"
  fi
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
  # Sprint 0044 T-0260 — ADMIN_* are only written when the operator opted
  # into the headless-with-admin path (--admin-email + --admin-password).
  # On the default path these stay empty and `ensure_admin_user` no-ops,
  # leaving the `/setup` web ceremony as the single admin-claim path.
  if [ -n "$ADMIN_EMAIL" ]; then
    set_env_var ADMIN_EMAIL    "$ADMIN_EMAIL"    "./.env"
    set_env_var ADMIN_PASSWORD "$ADMIN_PASSWORD" "./.env"
    set_env_var ADMIN_API_KEY  "$ADMIN_API_KEY"  "./.env"
  fi
  if [ -n "$ANTHROPIC_API_KEY" ]; then
    set_env_var ANTHROPIC_API_KEY "$ANTHROPIC_API_KEY" "./.env"
  fi

  # Sprint 0043 T-0257 — thread chosen host ports + derived URLs into
  # `.env`. docker-compose.yml reads BRILLIANT_{DB,API,MCP}_PORT via
  # ${…:-default} so a clean install (no conflict) preserves the
  # historical 5442/8010/8011 binding exactly. MCP_BASE_URL +
  # API_BASE_URL are also written so admin_bootstrap's credential-block
  # emitter picks up the chosen URLs (it reads these env vars and falls
  # back to localhost:8010/8011 otherwise — see api/admin_bootstrap.py).
  set_env_var BRILLIANT_DB_PORT  "$DB_HOST_PORT"  "./.env"
  set_env_var BRILLIANT_API_PORT "$API_HOST_PORT" "./.env"
  set_env_var BRILLIANT_MCP_PORT "$MCP_HOST_PORT" "./.env"
  set_env_var MCP_BASE_URL       "$MCP_URL"       "./.env"
  set_env_var API_BASE_URL       "$API_URL"       "./.env"
  # Canonical names read by api/routes/setup.py::_mcp_url_for_display and
  # mcp/client.py::_resolve_api_base_url. Without these, the /credentials
  # page (and stdio MCP client) fall back to the hardcoded localhost:8011 /
  # localhost:8010 defaults — which don't match the probed ports when the
  # installer bumps to :8020/:8021 or higher.
  set_env_var BRILLIANT_MCP_PUBLIC_URL "$MCP_URL" "./.env"
  set_env_var BRILLIANT_API_PUBLIC_URL "$API_URL" "./.env"

  # Sprint 0043 T-0259 — mint a pwd-derived COMPOSE_PROJECT_NAME so two
  # installs on the same host land in distinct compose projects (distinct
  # container names, distinct `pgdata` volume). Without this, `docker
  # compose up` in a second install RECREATES the first install's
  # containers against a mismatched `.env`.
  local project_name
  project_name="$(compute_compose_project_name)"
  set_env_var COMPOSE_PROJECT_NAME "$project_name" "./.env"
  log "phase 5" "compose project: ${project_name} (derived from $(pwd))"

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

phase_summary() {
  # Three branches keyed off (HEADLESS, ADMIN_EMAIL):
  #   - default ceremony (HEADLESS=0, no ADMIN_EMAIL): browser auto-open,
  #     /setup CTA, no creds file written.
  #   - headless no-admin (HEADLESS=1, no ADMIN_EMAIL): URL-print +
  #     SSH-tunnel hint, no browser-open, no creds file.
  #   - headless with admin (ADMIN_EMAIL set; HEADLESS implicit): no
  #     browser-open, no /setup CTA (latch is already claimed by env-
  #     bootstrap), creds file auto-written via fetch_credentials_file.
  #
  # The Services + Stop blocks are identical across branches.

  local mode
  if [ -n "$ADMIN_EMAIL" ]; then
    mode="headless-with-admin"
  elif [ "$HEADLESS" -eq 1 ]; then
    mode="headless"
  else
    mode="default"
  fi
  log "phase 8" "summary mode: ${mode}"

  # Headless-with-admin writes the creds file BEFORE the banner so the
  # banner can reference its presence (or absence on the warning path).
  if [ "$mode" = "headless-with-admin" ]; then
    fetch_credentials_file || true
  fi

  cat <<BANNER

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  xiReactor Brilliant installed successfully
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

BANNER

  case "$mode" in
    default)
      cat <<NEXT
  Next steps:
    Your browser should be opening to:
      ${API_URL}/setup

    If it doesn't open automatically, paste the URL above into your
    browser. Complete the form, then download brilliant-credentials.txt
    from the response page.

NEXT
      ;;
    headless)
      cat <<NEXT
  Next steps (headless / SSH-tunnel):
    Complete setup in your browser at:
      ${API_URL}/setup

    On a remote VPS, forward the API port to your workstation first:
      ssh -L ${API_HOST_PORT}:localhost:${API_HOST_PORT} user@host

    then open the URL above on your workstation.

NEXT
      ;;
    headless-with-admin)
      cat <<NEXT
  Credentials (admin bootstrapped via env):
    File:         ${CREDENTIALS_FILE} (mode 600, six fields)
    Admin email:  ${ADMIN_EMAIL}

    To re-fetch this file later:
      curl -H 'Authorization: Bearer <admin_api_key>' \\
           ${API_URL}/credentials > ${CREDENTIALS_FILE}

NEXT
      ;;
  esac

  cat <<SERVICES
  Services:
    API:          ${API_URL}
    Health:       ${API_URL}/health
    MCP:          ${MCP_URL}
    Postgres:     localhost:${DB_HOST_PORT}

  Stop / reset:
    docker compose down           # stop containers (preserves data)
    docker compose down -v        # full reset (drops Postgres volume)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SERVICES

  # Default-mode browser-open is best-effort and runs after the banner so
  # the user sees the fallback URL even if the open command swallows the
  # event (e.g. no display server but `xdg-open` exits 0).
  if [ "$mode" = "default" ]; then
    open_browser "${API_URL}/setup" || true
  fi
}

# ---------- demo seed (opt-in via --seed-demo) ----------

phase_seed() {
  # Apply db/seed/demo.sql via `docker exec` into brilliant-db. This runs
  # AFTER phase_up (stack healthy) and AFTER phase_verify (admin user is
  # bootstrapped) so the seed's ON CONFLICT DO NOTHING on org_demo just
  # accepts the operator-chosen org name instead of overwriting it.
  #
  # We deliberately do NOT route seed SQL through the Postgres init-scripts
  # bind-mount (db/migrations/ → /docker-entrypoint-initdb.d) — see
  # docker-compose.yml comment on that mount. Keeping the seed out of the
  # init path means a clean `docker compose up -d` NEVER re-seeds, and
  # stray files can't auto-run.
  if [ "$SEED_DEMO" -eq 0 ]; then
    log "phase 6b" "demo seed disabled (no --seed-demo) — skipping"
    return 0
  fi

  log "phase 6b" "applying demo seed (--seed-demo) via docker exec"

  local seed_path="./db/seed/demo.sql"
  if [ ! -f "$seed_path" ]; then
    die 84 "demo seed file not found at ${seed_path} — is this the brilliant repo?"
  fi

  # Pipe the SQL into the db service via `docker compose exec -T`. Post-
  # T-0259 we no longer hardcode `container_name: brilliant-db` in
  # docker-compose.yml, so `docker exec brilliant-db` would miss the
  # container (it's now named ${COMPOSE_PROJECT_NAME}-db-1). Compose-exec
  # resolves the service regardless of the project's container-name
  # scheme. `-T` disables TTY allocation so the `<` redirect works.
  local pg_user pg_db
  pg_user="${POSTGRES_USER:-postgres}"
  pg_db="${POSTGRES_DB:-brilliant}"

  if ! docker compose exec -T db psql -U "$pg_user" -d "$pg_db" \
        -v ON_ERROR_STOP=1 < "$seed_path" 2>&1 | tee -a "$LOG_FILE"; then
    log "phase 6b" "demo seed psql failed — dumping db logs"
    docker compose logs db --tail 30 2>&1 | tee -a "$LOG_FILE" || true
    die 85 "demo seed failed to apply. See ${LOG_FILE}. Stack is healthy; remove demo rows (if any applied) with: python tools/remove_demo_data.py --yes"
  fi
  log "phase 6b" "demo seed applied (12 entries tagged 'demo:seed')"
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

  # Step 5: bring up the renamed stack. Post-T-0259 docker-compose.yml no
  # longer hardcodes `container_name:`, so we also mint a pwd-derived
  # COMPOSE_PROJECT_NAME into `.env` before `docker compose up` — keeps
  # migrated installs from colliding with any sibling brilliant checkout
  # on the same host.
  log "migrate" "step 5: docker compose up -d --build"
  if [ "$DRY_RUN" -eq 0 ]; then
    if [ -f "./.env" ]; then
      local project_name
      project_name="$(compute_compose_project_name)"
      set_env_var COMPOSE_PROJECT_NAME "$project_name" "./.env"
      log "migrate" "compose project: ${project_name}"
    fi
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
    # Post-T-0259: use `docker compose exec db` — no literal brilliant-db
    # container name after we dropped `container_name:` from compose.
    if ! docker compose exec -T db psql -U postgres -lqt | grep -q '\bbrilliant\b'; then
      die 79 "brilliant database not found inside db service after migration. See ${LOG_FILE}."
    fi
    log "migrate" "brilliant database present in db service"
  else
    log "migrate" "\$ curl -fsS ${API_URL}/health  (poll up to ${HEALTH_TIMEOUT_SECONDS}s)"
    log "migrate" "\$ docker compose exec -T db psql -U postgres -lqt | grep brilliant"
  fi

  # Step 7: summary.
  log "migrate" "step 7: summary"
  local summary_project
  summary_project="$(compute_compose_project_name)"
  cat <<MIGRATED

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Migrated cortex → brilliant. Data preserved.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  New services:   db, api, mcp (compose project: ${summary_project})
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
  local mode
  if [ -n "$ADMIN_EMAIL" ]; then
    mode="headless-with-admin (env-driven bootstrap + auto-written ${CREDENTIALS_FILE})"
  elif [ "$HEADLESS" -eq 1 ]; then
    mode="headless (URL-print, no browser-open, /setup ceremony in browser)"
  else
    mode="default (browser auto-open to /setup, no installer-written creds file)"
  fi
  cat <<PLAN
xiReactor Brilliant installer — dry-run plan (v${SCRIPT_VERSION})

Install mode: ${mode}

Resolved configuration:
  admin-email:        ${ADMIN_EMAIL:-(unset — default ceremony path)}
  admin-password:     $(mask "${ADMIN_PASSWORD}")
  admin-api-key:      $(mask "${ADMIN_API_KEY}")
  postgres-password:  $(mask "${POSTGRES_PASSWORD}")
  anthropic-api-key:  $(mask "${ANTHROPIC_API_KEY}")
  headless:           ${HEADLESS}
  force:              ${FORCE}
  no-install-docker:  ${NO_INSTALL_DOCKER}
  seed-demo:          ${SEED_DEMO}
  ref:                ${REF:-(latest release tag; fallback main)}
  dir:                ${CLONE_DIR}

Planned phases:
  [phase 1]  preflight   — OS detect, bash 3.2+, openssl + curl present
  [phase 1b] self-clone  — if not inside a brilliant repo, git clone --ref into --dir and cd in
  [phase 2]  docker      — detect; install Colima (Mac) or get.docker.com (Linux) if missing
  [phase 3]  repo        — confirm we're inside the brilliant repo (docker-compose.yml present)
  [phase 4]  randoms     — fill unset secrets via openssl rand (ADMIN_API_KEY only when --admin-email is set)
  [phase 4b] port-probe  — probe db/api/mcp host ports; shift by ${PORT_PROBE_STEP} up to ${PORT_PROBE_MAX_TRIES} tries on conflict
  [phase 5]  env         — write ./.env from .env.sample (mode 600); refuse overwrite without --force
                          ADMIN_EMAIL/PASSWORD/API_KEY written only on the headless-with-admin path
  [phase 6]  up          — docker compose up -d, poll ${API_URL}/health for up to ${HEALTH_TIMEOUT_SECONDS}s
  [phase 6b] seed        — if --seed-demo: pipe db/seed/demo.sql through docker exec psql (skipped otherwise)
  [phase 8]  summary     — print mode-specific banner; on default path open ${API_URL}/setup in the browser;
                          on headless-with-admin path curl ${API_URL}/credentials → ${CREDENTIALS_FILE} (mode 600)

PLAN
}

# ---------- main ----------

main() {
  parse_flags "$@"

  # Sprint 0044 T-0260 — install no longer requires --admin-email. Default
  # path stands the stack up and points the operator at `/setup`. The only
  # required validation is the orphan check (--admin-password without
  # --admin-email) and the headless-with-admin password prompt.
  validate_admin_flags
  if [ "$MIGRATE_FROM_CORTEX" -eq 0 ] && [ -n "$ADMIN_EMAIL" ]; then
    # Implicit: passing --admin-email switches the installer into the
    # headless scripted path (no browser-open, auto-write creds file).
    HEADLESS=1
    prompt_admin_password
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
    if [ -n "$ADMIN_EMAIL" ]; then
      : "${ADMIN_API_KEY:=bkai_$(rand_hex 24)}"
    fi
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
  phase_port_probe
  phase_env
  phase_up
  phase_seed
  phase_summary
}

main "$@"
