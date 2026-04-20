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
readonly DEFAULT_KEY_OUT="./brilliant-credentials.txt"
readonly HEALTH_TIMEOUT_SECONDS=60
# shellcheck disable=SC2034
readonly VERIFY_TIMEOUT_SECONDS=30
# shellcheck disable=SC2034
readonly POLL_INTERVAL_SECONDS=2
# Port-probe tuning: step size and maximum number of attempts per port.
# 5 attempts at +10 each → 5442→5452→5462→5472→5482, then error out.
readonly PORT_PROBE_STEP=10
readonly PORT_PROBE_MAX_TRIES=5

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
SEED_DEMO=0
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

# ---------- path helpers ----------

absolutize_path() {
  # Convert a (potentially relative) path to an absolute path anchored at
  # the current working directory. Does NOT require the path to exist yet
  # — we may be resolving $KEY_OUT before it has been written. We resolve
  # the parent directory (which does exist, since write_key_out checks for
  # it) and append the basename. Works on Mac + Linux without realpath.
  # $1 path
  local path="$1"
  case "$path" in
    /*) printf '%s' "$path" ;;  # already absolute
    *)
      local dir base
      dir="$(dirname "$path")"
      base="$(basename "$path")"
      # cd into the dir in a subshell so we don't pollute the caller's cwd.
      if [ -d "$dir" ]; then
        printf '%s/%s' "$(cd "$dir" && pwd)" "$base"
      else
        # Parent doesn't exist (yet). Fall back to $PWD join — caller
        # will later fail the dir-check in write_key_out with a clear
        # error, which is the right behavior.
        printf '%s/%s' "$(pwd)" "$path"
      fi
      ;;
  esac
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

extract_credentials_block() {
  # Scrape the machine-parseable credential block emitted by
  # api/admin_bootstrap.py between ===BRILLIANT_CREDENTIALS_BEGIN=== and
  # ===BRILLIANT_CREDENTIALS_END=== markers from the api container logs.
  # Prints the inner block (six `key=value` lines) to stdout on success,
  # or nothing on failure.
  #
  # Note: admin bootstrap runs exactly once at first boot, so the block
  # will be present in the full container log history. We use `--no-color`
  # to avoid ANSI escape noise when future loggers add color.
  docker compose logs --no-color api 2>/dev/null \
    | awk '
        /===BRILLIANT_CREDENTIALS_BEGIN===/ { inblk=1; next }
        /===BRILLIANT_CREDENTIALS_END===/   { inblk=0 }
        inblk {
          # docker-compose log lines look like:
          #   brilliant-api  | admin_email=foo@bar.com
          # Strip the "containername ... | " prefix (the pipe is
          # reliable). Fall back to the whole line if no pipe is found
          # (older compose formats or custom formatters).
          pipe = index($0, "|")
          if (pipe > 0) {
            line = substr($0, pipe + 1)
            sub(/^[[:space:]]+/, "", line)
          } else {
            line = $0
          }
          if (line ~ /^[a-z_]+=/) print line
        }
      ' \
    | tail -n 6
}

write_key_out() {
  # Write the six-field credential block to $1 (mode 600). Fields are
  # extracted from `docker compose logs api` via extract_credentials_block;
  # when the scrape fails (e.g. logs rolled, DRY_RUN, or bootstrap hit the
  # latch on a re-run), fall back to the in-memory ADMIN_API_KEY + email so
  # the install still produces a credentials file — just a thinner one.
  local path="$1"
  local dir
  dir="$(dirname "$path")"
  if [ ! -d "$dir" ]; then
    die 75 "key-out directory does not exist: $dir"
  fi

  local block
  block="$(extract_credentials_block || true)"
  # Count non-empty key=value lines.
  local line_count=0
  if [ -n "$block" ]; then
    line_count="$(printf '%s\n' "$block" | grep -c '=')"
  fi

  {
    if [ "$line_count" -ge 6 ]; then
      printf '%s\n' "$block"
    else
      # Fallback path — bootstrap block not found (log rolled, already
      # bootstrapped, etc). Emit what we have; downstream tooling can
      # still read admin_email + admin_api_key.
      printf 'admin_email=%s\n'     "$ADMIN_EMAIL"
      printf 'admin_api_key=%s\n'   "$ADMIN_API_KEY"
      printf 'oauth_client_id=%s\n'     ""
      printf 'oauth_client_secret=%s\n' ""
      printf 'mcp_url=%s\n'         "${MCP_URL}"
      printf 'login_url=%s\n'       "${API_URL}/auth/login"
    fi
  } >"$path"
  chmod 600 "$path"
}

phase_summary() {
  log "phase 8" "writing credentials to ${KEY_OUT}"
  write_key_out "$KEY_OUT"

  # The banner prints to stdout only (not the log) — this is what the
  # operator reads at the end of a successful run. Sprint 0043 #42 reshape:
  # lead with /setup + /import/vault (the URLs that actually matter to a
  # first-run user), show the absolute Key file path so it resolves after
  # the installer exits from any CWD, and document the stop / full-reset
  # off-ramps so there's no "how do I turn this off?" guesswork.
  cat <<BANNER

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  xiReactor Brilliant installed successfully
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Next steps — open these in a browser:
    Setup:        ${API_URL}/setup
    Import vault: ${API_URL}/import/vault

  Credentials file (contains 6 fields — keep it safe):
    ${KEY_OUT} (mode 600)

  Services:
    API:          ${API_URL}
    Health:       ${API_URL}/health
    MCP:          ${MCP_URL}
    Postgres:     localhost:${DB_HOST_PORT}
    Admin email:  ${ADMIN_EMAIL}

  Stop / reset:
    docker compose down           # stop containers (preserves data)
    docker compose down -v        # full reset (drops Postgres volume)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

BANNER
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

  # Pipe the SQL into the running brilliant-db container. We do NOT cd into
  # the container or bind-mount the file — `docker exec -i` + STDIN redirect
  # is portable and does not need any compose-file changes.
  local pg_user pg_db
  pg_user="${POSTGRES_USER:-postgres}"
  pg_db="${POSTGRES_DB:-brilliant}"

  if ! docker exec -i brilliant-db psql -U "$pg_user" -d "$pg_db" \
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
  seed-demo:          ${SEED_DEMO}
  ref:                ${REF:-(latest release tag; fallback main)}
  dir:                ${CLONE_DIR}

Planned phases:
  [phase 1]  preflight   — OS detect, bash 3.2+, openssl + curl present
  [phase 1b] self-clone  — if not inside a brilliant repo, git clone --ref into --dir and cd in
  [phase 2]  docker      — detect; install Colima (Mac) or get.docker.com (Linux) if missing
  [phase 3]  repo        — confirm we're inside the brilliant repo (docker-compose.yml present)
  [phase 4]  randoms     — fill unset secrets via openssl rand
  [phase 4b] port-probe  — probe db/api/mcp host ports; shift by ${PORT_PROBE_STEP} up to ${PORT_PROBE_MAX_TRIES} tries on conflict
  [phase 5]  env         — write ./.env from .env.sample (mode 600); refuse overwrite without --force
  [phase 6]  up          — docker compose up -d, poll ${API_URL}/health for up to ${HEALTH_TIMEOUT_SECONDS}s
  [phase 6b] seed        — if --seed-demo: pipe db/seed/demo.sql through docker exec psql (skipped otherwise)
  [phase 7]  verify      — GET ${API_URL}/entries with admin key to confirm bootstrap
  [phase 8]  summary     — print banner, write key to ${KEY_OUT} (mode 600)

PLAN
}

# ---------- main ----------

main() {
  parse_flags "$@"

  # Resolve KEY_OUT to an absolute path BEFORE any `cd` into the self-
  # cloned repo. This is load-bearing for two reasons:
  #   1. The final banner prints $KEY_OUT and the operator often `cat`s it
  #      from a different working directory — a relative path would break.
  #   2. phase_self_clone cd's into $CLONE_DIR; a relative KEY_OUT would
  #      silently end up inside the clone dir rather than beside the
  #      installer invocation.
  KEY_OUT="$(absolutize_path "$KEY_OUT")"

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
  phase_port_probe
  phase_env
  phase_up
  phase_verify
  phase_seed
  phase_summary
}

main "$@"
