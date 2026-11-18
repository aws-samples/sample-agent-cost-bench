#!/usr/bin/env bash
#
# run.sh
#
# Self-contained, HOST-ONLY runner for agent-cost-bench. No AWS, no ECS, no
# Docker socket, no Secrets Manager. Everything happens on this machine:
#
#   1. Preflight  — check/instruct on prerequisites (git, python3, node/npm, curl).
#   2. Install    — (optional) install the benchmark + the vendor coding CLIs locally.
#   3. Configure  — interactively pick CLIs + model, collect API keys, generate
#                   a cli-compare config YAML.
#   4. Run        — run the benchmark on this host and write the HTML/JSON report
#                   into a local results directory (opened at the end unless --no-open).
#
# This mirrors what the ECS entrypoint + generate-config.sh do together, but
# collapsed into one interactive script that runs entirely locally.
#
# Usage:
#   ./run.sh                 # interactive, full flow
#   ./run.sh --yes           # defaults (Kiro + Claude Code, opus)
#   ./run.sh --skip-install  # assume CLIs/benchmark already installed
#   ./run.sh --no-open       # don't open the report at the end
#   ./run.sh --config path.yaml  # use an existing config, skip prompts
#   ./run.sh --no-save-keys  # don't cache entered keys to .env
#   ./run.sh --label "Opus 4.8 shootout"  # set the report's comparison label
#   ./run.sh --effort high   # global reasoning effort: low|medium|high
#
# Effort:
#   A single global effort (low|medium|high, default high) is written as the
#   top-level `effort:` key in the config and applied across ALL CLIs — via the
#   {effort} flag for CLIs that take one (Kiro, Claude Code, Copilot) and by the
#   harness appending it to the model slug for the others (Cursor, Devin,
#   Antigravity). Per-task effort in a task.yaml still overrides this.
#
# API key caching:
#   Keys you enter at the prompt are saved to a .env file (default
#   ./.agent-cost-bench/.env, chmod 600) and auto-loaded on the next run, so you
#   only enter each key once. Pre-exported keys always win over the cache.
#   Use --no-save-keys to disable saving, or ACB_ENV_FILE=path to relocate it.
#   The .env may hold secrets — keep it out of git (add it to .gitignore).
#
# Environment overrides (optional):
#   BENCH_REPO_URL   default https://github.com/aws-samples/sample-agent-cost-bench
#   BENCH_REPO_REF   default main   (branch/tag/40-char SHA)
#   BENCH_HOME       default ./.agent-cost-bench (workspace root)
#   RESULTS_DIR      default ./acb-results/<runId>
#   ACB_ENV_FILE     default ./.agent-cost-bench/.env (cached API keys)
#
# Fresh clone per run:
#   Every run clones the benchmark fresh into a timestamped folder
#   ./.agent-cost-bench/runs/<UTC-timestamp>-<rand>/agent-cost-bench, so you
#   always get the latest from `main` (override the ref with BENCH_REPO_REF).
#   The per-run venv + generated config.yaml live in that same folder.
#   --skip-install reuses the most recent previous run's clone instead.
#   API keys can be pre-exported to skip the prompts, e.g. ANTHROPIC_API_KEY=...
#

# This script uses bash-only features (arrays, process substitution, [[ ]]).
# If it was launched under a non-bash shell (e.g. `sh script.sh` or a POSIX
# /bin/sh), re-exec it under bash so those constructs parse correctly. Without
# this, sh/dash mis-parses a bash line and then tries to run later comment/box
# lines as commands (e.g. "───...: command not found"). This guard is written
# in POSIX syntax so it is safe under any shell.
if [ -z "${BASH_VERSION:-}" ]; then
  if command -v bash >/dev/null 2>&1; then
    exec bash "$0" "$@"
  else
    echo "ERROR: this script requires bash. Please install bash and run: bash $0" >&2
    exit 1
  fi
fi

set -euo pipefail

# ── Pretty logging ───────────────────────────────────────────────────────────
BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; RST=$'\033[0m'
log()  { echo "${DIM}[acb]${RST} $*"; }
info() { echo "${GRN}[acb]${RST} $*"; }
warn() { echo "${YLW}[acb] WARN:${RST} $*" >&2; }
err()  { echo "${RED}[acb] ERROR:${RST} $*" >&2; }
die()  { err "$*"; exit 1; }

# ── Args ─────────────────────────────────────────────────────────────────────
ASSUME_YES=0
SKIP_INSTALL=0
OPEN_REPORT=1
SAVE_KEYS=1
EXPLICIT_CONFIG=""
COMPARISON_LABEL=""   # --label; auto-generated from the selected CLIs if unset
EFFORT=""             # --effort low|medium|high; global effort for all runners (prompted if unset)
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y)        ASSUME_YES=1; shift ;;
    --skip-install)  SKIP_INSTALL=1; shift ;;
    --no-open)       OPEN_REPORT=0; shift ;;
    --no-save-keys)  SAVE_KEYS=0; shift ;;
    --label|-l)      COMPARISON_LABEL="${2:?--label needs a value}"; shift 2 ;;
    --effort|-e)     EFFORT="${2:?--effort needs a value (low|medium|high)}"; shift 2 ;;
    --config|-c)     EXPLICIT_CONFIG="${2:?--config needs a path}"; shift 2 ;;
    -h|--help)       sed -n '2,52p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

# Validate --effort if given up front.
if [ -n "$EFFORT" ]; then
  case "$EFFORT" in
    low|medium|high) : ;;
    *) die "--effort must be one of: low, medium, high (got '$EFFORT')" ;;
  esac
fi

BENCH_REPO_URL="${BENCH_REPO_URL:-https://github.com/aws-samples/sample-agent-cost-bench}"
BENCH_REPO_REF="${BENCH_REPO_REF:-main}"
# BENCH_HOME is the persistent workspace root (holds the cached .env and the
# per-run subfolders). Each run gets its OWN timestamped folder under runs/ with
# a FRESH clone of the benchmark, so every run picks up the latest from main.
BENCH_HOME="${BENCH_HOME:-$(pwd)/.agent-cost-bench}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_ID="${RUN_ID:-${TS}-$(head -c4 /dev/urandom | od -An -tx1 | tr -d ' \n')}"
# Per-run directory: fresh clone + venv live here, isolated from other runs.
RUN_DIR="${RUN_DIR:-${BENCH_HOME}/runs/${RUN_ID}}"
CLONE_DIR="${RUN_DIR}/agent-cost-bench"   # fresh git clone of BENCH_REPO_REF
VENV_DIR="${RUN_DIR}/.venv"               # per-run virtualenv
# Cached API keys persist across runs at BENCH_HOME level (chmod 600).
ENV_FILE="${ACB_ENV_FILE:-${BENCH_HOME}/.env}"
RESULTS_DIR="${RESULTS_DIR:-$(pwd)/acb-results/${RUN_ID}}"

# ── Supported CLIs ────────────────────────────────────────────────────────────
# Format: "name|display|cli_path|cost|auth_env|install_kind|install_arg|default_model|list_cmd"
#   cost:         credit | claude_json | copilot_json | codex_json | cursor_json | antigravity_json | devin_export
#   install_kind: npm | curl | none
#   default_model: each CLI has its OWN model namespace — there is no single
#                  model id that works across all of them. Used as the fallback
#                  when the CLI can't list models (not logged in / no list cmd).
#   list_cmd:      command that prints the CLI's available models, one per line,
#                  with the model slug as the FIRST whitespace-delimited token
#                  (e.g. `agy models` → "gemini-3.8-flash-high  Gemini 3.8 Flash (High)").
#                  Empty = this CLI has no machine-listable model set; the script
#                  falls back to a free-text prompt with default_model.
#                  NOTE: some list commands require the CLI to be logged in first
#                  (Cursor, Devin); if they error, the script silently falls back.
#   auth_mode:     how this CLI authenticates (verified against vendor docs, 2026):
#                    apikey — an env-var API key works for headless use
#                             (Kiro KIRO_API_KEY, Claude Code ANTHROPIC_API_KEY,
#                              Copilot GITHUB_TOKEN, Codex OPENAI_API_KEY).
#                    login  — NO env-var API key for the CLI; requires an
#                             interactive login + a signed-up account
#                             (Antigravity `agy login`, Devin `devin auth login`).
#                    hybrid — supports an API key AND a browser login, but login
#                             is recommended / often required in practice
#                             (Cursor: CURSOR_API_KEY exists but `cursor login`
#                              via Keychain is the reliable path, esp. in containers).
#   login_cmd:     the interactive login command to run (for login/hybrid modes).
SUPPORTED=(
  "kiro|Kiro|kiro-cli|credit|KIRO_API_KEY|curl|https://cli.kiro.dev/install|claude-opus-4.8|kiro-cli chat --list-models|apikey|kiro-cli login"
  "claude-code|Claude Code|claude|claude_json|ANTHROPIC_API_KEY|npm|@anthropic-ai/claude-code|us.anthropic.claude-opus-4-8||apikey|claude login"
  "copilot|GitHub Copilot|copilot|copilot_json|GITHUB_TOKEN|npm|@github/copilot|claude-opus-4.8||apikey|copilot"
  "cursor|Cursor|agent|cursor_json|CURSOR_API_KEY|curl|https://cursor.com/install|claude-opus-4-8|agent --list-models|hybrid|agent login"
  "codex|OpenAI Codex|codex|codex_json|OPENAI_API_KEY|npm|@openai/codex|gpt-5.5||apikey|codex auth login"
  "devin|Devin|devin|devin_export|DEVIN_API_KEY|curl|https://cli.devin.ai/install.sh|claude-opus-4-8|devin models list|login|devin auth login"
  "antigravity|Antigravity|agy|antigravity_json|GOOGLE_API_KEY|curl|https://antigravity.google/cli/install.sh|gemini-3.8-flash|agy models|login|agy login"
)

entry_field() { # $1=entry $2=index(1-11)
  printf '%s' "$1" | cut -d'|' -f"$2"
}

# ─────────────────────────────────────────────────────────────────────────────
# 1. Preflight
# ─────────────────────────────────────────────────────────────────────────────
preflight() {
  info "Step 1/4 — preflight"
  local missing=0
  for tool in git curl python3; do
    if command -v "$tool" >/dev/null 2>&1; then
      log "found $tool: $(command -v "$tool")"
    else
      warn "missing required tool: $tool"
      missing=1
    fi
  done
  # pip / venv
  if ! python3 -m venv --help >/dev/null 2>&1; then
    warn "python3 venv module not available (install python3-venv)"
    missing=1
  fi
  # node/npm only strictly needed if an npm-based CLI is selected; warn softly.
  if command -v npm >/dev/null 2>&1; then
    log "found npm: $(command -v npm)"
  else
    warn "npm not found — needed for Claude Code / Copilot / Codex installs. Install Node.js 18+ if you pick those."
  fi
  [ "$missing" -eq 0 ] || die "install the missing prerequisites above and re-run."
  info "preflight OK"
}

# ─────────────────────────────────────────────────────────────────────────────
# 2. Install benchmark + selected vendor CLIs
# ─────────────────────────────────────────────────────────────────────────────
install_benchmark() {
  info "Step 2/4 — clone benchmark fresh (${BENCH_REPO_URL} @ ${BENCH_REPO_REF})"
  # Always clone into a fresh, timestamped per-run folder so every run uses the
  # latest code from the ref (default: main). This deliberately does NOT reuse a
  # previous checkout.
  mkdir -p "${RUN_DIR}"
  if [ -e "${CLONE_DIR}" ]; then
    # Extremely unlikely (timestamped dir), but be safe: remove a stale clone.
    warn "clone dir already exists (${CLONE_DIR}); removing for a clean clone"
    rm -rf "${CLONE_DIR}"
  fi
  info "cloning into ${CLONE_DIR}"
  if git clone --depth 1 --branch "${BENCH_REPO_REF}" "${BENCH_REPO_URL}" "${CLONE_DIR}" 2>/dev/null; then
    log "cloned ${BENCH_REPO_REF} (shallow, latest)"
  else
    log "shallow clone of ref failed; full clone + checkout (works for a commit SHA)"
    git clone "${BENCH_REPO_URL}" "${CLONE_DIR}"
    git -C "${CLONE_DIR}" checkout "${BENCH_REPO_REF}"
  fi
  # Record exactly what we got, for reproducibility.
  local sha
  sha="$(git -C "${CLONE_DIR}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  info "checked out ${BENCH_REPO_REF} @ ${sha}"

  log "creating virtualenv at ${VENV_DIR}"
  python3 -m venv "${VENV_DIR}"
  # shellcheck disable=SC1091
  . "${VENV_DIR}/bin/activate"
  python -m pip install --quiet --upgrade pip
  log "pip install the benchmark (this can take a minute)"
  python -m pip install --quiet "${CLONE_DIR}"
  command -v agent-cost-bench >/dev/null 2>&1 \
    || die "agent-cost-bench not on PATH after install — check the repo ref / package."
  info "benchmark installed: $(command -v agent-cost-bench)"
}

install_selected_clis() {
  info "installing vendor CLIs for the selected runners"
  local entry name display cli_path cost auth kind arg
  for entry in "${SELECTED[@]}"; do
    name="$(entry_field "$entry" 1)"
    display="$(entry_field "$entry" 2)"
    cli_path="$(entry_field "$entry" 3)"
    kind="$(entry_field "$entry" 6)"
    arg="$(entry_field "$entry" 7)"

    if command -v "$cli_path" >/dev/null 2>&1; then
      log "$display CLI already present ($cli_path); skipping install"
      continue
    fi
    log "installing $display CLI ..."
    case "$kind" in
      npm)
        command -v npm >/dev/null 2>&1 || { warn "npm missing; cannot install $display. Skipping."; continue; }
        npm install -g "$arg" || warn "npm install of $display failed — install manually and re-run with --skip-install."
        ;;
      curl)
        # Best-effort vendor installers. Network/permission failures are warned, not fatal.
        curl -fsSL "$arg" | bash || warn "installer for $display failed ($arg) — verify the vendor's current install docs."
        # Cursor installs `cursor-agent`; the benchmark expects `agent`.
        if [ "$name" = "cursor" ] && command -v cursor-agent >/dev/null 2>&1 && ! command -v agent >/dev/null 2>&1; then
          local tgt; tgt="$(dirname "$(command -v cursor-agent)")/agent"
          ln -sf "$(command -v cursor-agent)" "$tgt" 2>/dev/null || warn "could not symlink cursor-agent -> agent; add it to PATH as 'agent' manually."
        fi
        ;;
      none) : ;;
    esac
    if command -v "$cli_path" >/dev/null 2>&1; then
      info "$display CLI ready: $(command -v "$cli_path")"
    else
      warn "$display CLI ($cli_path) still not on PATH — the run may fail for this runner."
    fi
  done
}

# After installing the selected CLIs, print a boxed notice telling the user how
# each one authenticates: enter an API key at the prompt (apikey), or run the
# CLI's interactive `login` because it has no env-var API key (login/hybrid).
# Auth modes are from vendor docs (verified 2026) — see the SUPPORTED table.
print_auth_notice() {
  # Build the content lines first so we can size the box to the widest line.
  local lines=() entry display auth_env auth_mode login_cmd line
  lines+=("BEFORE THE RUN -- authenticate each CLI:")
  lines+=("")
  for entry in "${SELECTED[@]}"; do
    display="$(entry_field "$entry" 2)"
    auth_env="$(entry_field "$entry" 5)"
    auth_mode="$(entry_field "$entry" 10)"
    login_cmd="$(entry_field "$entry" 11)"
    # ASCII markers only, so column width == character count in every terminal.
    case "$auth_mode" in
      apikey)
        line="[OK]    ${display}: API key -- set ${auth_env} (this script prompts & caches it)"
        ;;
      login)
        line="[LOGIN] ${display}: NO API key -- you MUST run:  ${login_cmd}"
        ;;
      hybrid)
        line="[!]     ${display}: API key (${auth_env}) OR login -- prefer:  ${login_cmd}"
        ;;
      *)
        line="[?]     ${display}: see the vendor docs for authentication"
        ;;
    esac
    lines+=("$line")
  done
  lines+=("")
  lines+=("[LOGIN] CLIs (e.g. Antigravity, Devin) have no env-var API key -- the")
  lines+=("run fails for them until you complete their login in this same shell.")

  # All lines are pure ASCII now, so ${#l} is the exact display width.
  local maxw=0 l
  for l in "${lines[@]}"; do
    [ "${#l}" -gt "$maxw" ] && maxw="${#l}"
  done
  local inner=$((maxw + 2))
  local bar; bar="$(printf '=%.0s' $(seq 1 $((inner + 2))))"
  echo ""
  echo "${BOLD}+${bar}+${RST}"
  for l in "${lines[@]}"; do
    printf "${BOLD}|${RST} %-*s ${BOLD}|${RST}\n" "$inner" "$l"
  done
  echo "${BOLD}+${bar}+${RST}"
  echo ""
}

# Offer to run the interactive login command for each CLI entry in NEEDS_LOGIN
# (populated by collect_keys: pure login CLIs always; hybrid CLIs only when the
# user left the API key blank). Called AFTER collect_keys.
offer_logins() {
  [ "${#NEEDS_LOGIN[@]}" -gt 0 ] || return 0
  if [ "$ASSUME_YES" -eq 1 ]; then
    warn "CLIs needing interactive login: run their login command before the benchmark (see box above)."
    return 0
  fi
  local ans entry display login_cmd
  for entry in "${NEEDS_LOGIN[@]}"; do
    display="$(entry_field "$entry" 2)"
    login_cmd="$(entry_field "$entry" 11)"
    command -v "$(entry_field "$entry" 3)" >/dev/null 2>&1 || continue
    read -r -p "Run '${login_cmd}' for ${display} now? [y/N]: " ans
    case "$ans" in
      y|Y|yes|YES)
        log "launching: ${login_cmd}"
        # shellcheck disable=SC2086
        eval "$login_cmd" || warn "${login_cmd} did not complete; ${display} may fail to authenticate."
        ;;
      *) log "skipped ${login_cmd}; remember to run it before the benchmark." ;;
    esac
  done
}

# ─────────────────────────────────────────────────────────────────────────────
# 3. Select CLIs + model, collect keys, generate config
# ─────────────────────────────────────────────────────────────────────────────
select_runners() {
  echo ""
  echo "${BOLD}Supported CLIs:${RST}"
  local i=1 entry name display
  for entry in "${SUPPORTED[@]}"; do
    name="$(entry_field "$entry" 1)"; display="$(entry_field "$entry" 2)"
    printf "  %d) %-14s %s\n" "$i" "$name" "$display"
    i=$((i + 1))
  done

  local choices
  if [ "$ASSUME_YES" -eq 1 ]; then
    choices="1 2"  # Kiro + Claude Code
    log "defaults selected: Kiro + Claude Code"
  else
    echo ""
    read -r -p "Select CLIs to compare (space-separated numbers, e.g. '1 2 4'): " choices
  fi

  SELECTED=()
  local n idx
  for n in $choices; do
    idx=$((n - 1))
    if [ "$idx" -lt 0 ] || [ "$idx" -ge "${#SUPPORTED[@]}" ]; then
      die "invalid selection '$n'"
    fi
    SELECTED+=("${SUPPORTED[$idx]}")
  done
  [ "${#SELECTED[@]}" -ge 2 ] || die "pick at least 2 CLIs to compare."
}

# Query a CLI's available model slugs by running its list command. Prints one
# slug per line on stdout. Returns non-zero (and prints nothing) if the CLI
# can't list — no list command, not installed, not authenticated, or errored.
# Parsing rule: take the FIRST whitespace-delimited token of each output line,
# skip blank lines, lines that look like errors/status, and obvious headers.
list_cli_models() {
  local list_cmd="$1" cli_path="$2" raw
  [ -n "$list_cmd" ] || return 1
  command -v "$cli_path" >/dev/null 2>&1 || return 1
  # Run the list command; capture stdout+stderr (some print to stderr).
  raw="$(eval "$list_cmd" 2>&1)" || true
  [ -n "$raw" ] || return 1
  # If the whole output smells like an auth/error message, bail to fallback.
  if printf '%s' "$raw" | grep -qiE 'error|not logged in|authentication required|login|usage:'; then
    return 1
  fi
  # Drop obvious status/progress/header lines (e.g. "Fetching available
  # models...", "Available models (* = default):", trailing "..."). Strip a
  # leading default marker ("* " or bullet) that some CLIs (e.g. Kiro) put in
  # front of the default row, so the model slug becomes the first token. Then
  # take the first whitespace-delimited token per remaining line and keep only
  # plausible model slugs (lowercase alphanumerics containing a digit or
  # hyphen, so prose words like "Fetching" are rejected).
  printf '%s\n' "$raw" \
    | grep -viE '^(fetching|available|loading|models|listing)\b' \
    | grep -viE '\.\.\.[[:space:]]*$' \
    | sed -E 's/^[[:space:]]*[*•-][[:space:]]+//' \
    | awk '{print $1}' \
    | grep -E '^[a-z0-9][a-z0-9._-]*$' || true
}

# Pick a model PER selected CLI. For each runner we try its list command and, if
# it returns real slugs, show a numbered picker (default = the CLI's known-good
# default if present in the list, else the first entry). If the CLI can't list
# (no command / not authed / errored), fall back to a free-text prompt seeded
# with the default. Chosen models go into MODELS, index-aligned with SELECTED.
select_models() {
  MODELS=()
  local i entry name display cli_path def list_cmd chosen
  echo ""
  echo "${BOLD}Model per CLI${RST} ${DIM}(queried from each CLI's own model list where possible)${RST}"
  for i in "${!SELECTED[@]}"; do
    entry="${SELECTED[$i]}"
    name="$(entry_field "$entry" 1)"
    display="$(entry_field "$entry" 2)"
    cli_path="$(entry_field "$entry" 3)"
    def="$(entry_field "$entry" 8)"
    list_cmd="$(entry_field "$entry" 9)"

    # Try to fetch the live model list for this CLI. Use a temp file rather than
    # process substitution (< <(...)) so the script also parses under a POSIX
    # /bin/sh (bash-in-sh-mode disables process substitution at parse time).
    local models_list=()
    if [ -n "$list_cmd" ]; then
      log "querying models for ${display}: ${list_cmd}"
      local _ml_tmp
      _ml_tmp="$(mktemp)"
      list_cli_models "$list_cmd" "$cli_path" > "$_ml_tmp" 2>/dev/null || true
      while IFS= read -r m; do [ -n "$m" ] && models_list+=("$m"); done < "$_ml_tmp"
      rm -f "$_ml_tmp"
    fi

    if [ "${#models_list[@]}" -gt 0 ]; then
      # We have a real list. Pick a sensible default index (match def if listed).
      local default_idx=1 j=1
      for m in "${models_list[@]}"; do
        [ "$m" = "$def" ] && default_idx="$j"
        j=$((j + 1))
      done
      if [ "$ASSUME_YES" -eq 1 ]; then
        chosen="${models_list[$((default_idx - 1))]}"
      else
        echo "  ${BOLD}${display}${RST} — available models:"
        j=1
        for m in "${models_list[@]}"; do
          if [ "$j" -eq "$default_idx" ]; then printf "    %d) %s ${DIM}(default)${RST}\n" "$j" "$m"
          else printf "    %d) %s\n" "$j" "$m"; fi
          j=$((j + 1))
        done
        local pick
        read -r -p "    choose 1-${#models_list[@]}, a custom slug, or Enter for default: " pick
        if [ -z "$pick" ]; then
          chosen="${models_list[$((default_idx - 1))]}"
        elif printf '%s' "$pick" | grep -qE '^[0-9]+$'; then
          if [ "$pick" -ge 1 ] && [ "$pick" -le "${#models_list[@]}" ]; then
            chosen="${models_list[$((pick - 1))]}"
          else
            die "invalid model number '$pick' for ${display}"
          fi
        else
          chosen="$pick"   # user typed a custom slug
        fi
      fi
    else
      # No live list — fall back to free-text with the known-good default.
      [ -n "$list_cmd" ] && log "could not list models for ${display} (not authed / unavailable); using prompt"
      if [ "$ASSUME_YES" -eq 1 ]; then
        chosen="$def"
      else
        read -r -p "  ${display} model [${def}]: " chosen
        chosen="${chosen:-$def}"
      fi
    fi

    MODELS+=("$chosen")
    log "${display} → model: ${chosen}"
  done
}

# Choose the single GLOBAL reasoning effort applied across all CLIs. Uses
# --effort if given, else prompts (default: high, matching the benchmark's own
# default). Stored in EFFORT and written as the top-level `effort:` config key.
select_effort() {
  local default_effort="high"
  if [ -n "$EFFORT" ]; then
    :  # already set (and validated) via --effort
  elif [ "$ASSUME_YES" -eq 1 ]; then
    EFFORT="$default_effort"
  else
    local pick
    read -r -p "Global reasoning effort for all CLIs — low|medium|high [${default_effort}]: " pick
    pick="${pick:-$default_effort}"
    case "$pick" in
      low|medium|high) EFFORT="$pick" ;;
      *) warn "invalid effort '${pick}', using '${default_effort}'"; EFFORT="$default_effort" ;;
    esac
  fi
  log "global effort: ${EFFORT}"
}

# Return a redacted preview of a secret: keeps a few leading/trailing chars and
# replaces the middle with x's, so logs confirm what was captured without
# leaking the value. Short secrets (<= 8 chars) are fully masked.
mask_secret() {
  local s="$1" len keep=4 head tail
  len=${#s}
  if [ "$len" -le 8 ]; then
    printf 'xxxx'
    return
  fi
  head="${s:0:keep}"
  tail="${s:$((len - keep)):keep}"
  printf '%sxxxx%s' "$head" "$tail"
}

# Load cached API keys from the .env file into the environment, WITHOUT
# overriding anything already exported in the current shell (explicit env wins).
# The file is a simple KEY=value list (one per line, no `export`, no quotes).
# Loaded keys are tracked in ENV_LOADED_KEYS so collect_keys can label them.
ENV_LOADED_KEYS=""
load_env_file() {
  [ -f "$ENV_FILE" ] || return 0
  # Warn if the file is group/world readable — it holds secrets.
  local perms
  perms="$(stat -f '%Lp' "$ENV_FILE" 2>/dev/null || stat -c '%a' "$ENV_FILE" 2>/dev/null || echo '')"
  case "$perms" in
    600|400|"") : ;;
    *) warn "${ENV_FILE} is not private (mode ${perms}); tightening to 600."; chmod 600 "$ENV_FILE" 2>/dev/null || true ;;
  esac
  local line k v
  while IFS= read -r line || [ -n "$line" ]; do
    # skip blanks and comments
    case "$line" in ''|\#*) continue ;; esac
    k="${line%%=*}"
    v="${line#*=}"
    # only accept KEY=... where KEY is a valid env name
    printf '%s' "$k" | grep -qE '^[A-Za-z_][A-Za-z0-9_]*$' || continue
    if [ -z "${!k:-}" ]; then
      export "${k}=${v}"
      ENV_LOADED_KEYS="${ENV_LOADED_KEYS} ${k}"
    fi
  done < "$ENV_FILE"
  [ -n "$ENV_LOADED_KEYS" ] && log "loaded cached keys from ${ENV_FILE}:${ENV_LOADED_KEYS}"
  return 0
}

# Persist one KEY=value to the .env file (create at 600, replace any existing
# line for that key). No-op when --no-save-keys is set.
save_key_to_env() {
  [ "$SAVE_KEYS" -eq 1 ] || return 0
  local key="$1" val="$2" tmp
  mkdir -p "$(dirname "$ENV_FILE")"
  if [ ! -f "$ENV_FILE" ]; then
    : > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    printf '# run.sh cached API keys — do NOT commit.\n' >> "$ENV_FILE"
  fi
  # Drop any existing line for this key, then append the new value.
  tmp="$(mktemp)"
  grep -vE "^${key}=" "$ENV_FILE" > "$tmp" 2>/dev/null || true
  printf '%s=%s\n' "$key" "$val" >> "$tmp"
  cat "$tmp" > "$ENV_FILE"
  rm -f "$tmp"
  chmod 600 "$ENV_FILE"
}

# Was this key loaded from the .env cache (vs. exported in the shell)?
key_from_cache() {
  case " ${ENV_LOADED_KEYS} " in *" $1 "*) return 0 ;; *) return 1 ;; esac
}

collect_keys() {
  info "collecting API keys for the selected runners (input hidden; leave blank to skip)"
  [ "$SAVE_KEYS" -eq 1 ] && log "entered keys are cached to ${ENV_FILE} (chmod 600) for next time; --no-save-keys to disable"
  NEEDS_LOGIN=()   # CLIs that still need an interactive login (offer_logins uses this)
  local entry display auth auth_mode login_cmd val
  for entry in "${SELECTED[@]}"; do
    display="$(entry_field "$entry" 2)"
    auth="$(entry_field "$entry" 5)"
    auth_mode="$(entry_field "$entry" 10)"
    login_cmd="$(entry_field "$entry" 11)"

    # login-only CLIs (Antigravity, Devin) have NO env-var API key. Don't prompt
    # — queue them for the interactive login (offer_logins).
    if [ "$auth_mode" = "login" ]; then
      log "${display}: uses '${login_cmd}' (no API key prompt)"
      NEEDS_LOGIN+=("$entry")
      continue
    fi

    # If a key is already present (exported or cached), use it.
    if [ -n "${!auth:-}" ]; then
      if key_from_cache "$auth"; then
        log "${auth} loaded from cache; using it ($(mask_secret "${!auth}"))"
      else
        log "${auth} already set in environment; using it ($(mask_secret "${!auth}"))"
      fi
      continue
    fi

    # hybrid CLIs (Cursor): ask for the API key FIRST; if left blank, fall back
    # to the interactive login. If a key is given, use/cache it (key auth).
    if [ "$auth_mode" = "hybrid" ]; then
      if [ "$ASSUME_YES" -eq 1 ]; then
        log "${display}: no ${auth} and --yes given; will rely on an existing '${login_cmd}' session."
        NEEDS_LOGIN+=("$entry")
        continue
      fi
      read -r -s -p "  ${display} — ${auth} (leave blank to log in with '${login_cmd}' instead): " val; echo ""
      if [ -n "$val" ]; then
        export "${auth}=${val}"
        save_key_to_env "$auth" "$val"
        if [ "$SAVE_KEYS" -eq 1 ]; then
          log "exported ${auth} = $(mask_secret "$val") (saved to cache)"
        else
          log "exported ${auth} = $(mask_secret "$val")"
        fi
      else
        log "${display}: no API key entered — will offer '${login_cmd}'."
        NEEDS_LOGIN+=("$entry")
      fi
      continue
    fi

    # apikey CLIs: prompt for and cache the env-var key.
    if [ "$ASSUME_YES" -eq 1 ]; then
      warn "${auth} not set and --yes given; ${display} may fail to authenticate."
      continue
    fi
    read -r -s -p "  ${display} — ${auth}: " val; echo ""
    if [ -n "$val" ]; then
      export "${auth}=${val}"
      save_key_to_env "$auth" "$val"
      if [ "$SAVE_KEYS" -eq 1 ]; then
        log "exported ${auth} = $(mask_secret "$val") (saved to cache)"
      else
        log "exported ${auth} = $(mask_secret "$val")"
      fi
    else
      warn "no value for ${auth}; ${display} may fail to authenticate."
    fi
  done
}

generate_config() {
  mkdir -p "${RUN_DIR}"
  CONFIG_FILE="${RUN_DIR}/config.yaml"
  info "Step 3/4 — generating config: ${CONFIG_FILE}"

  # Resolve the comparison label: use --label if given, else build a meaningful
  # default from the selected CLIs and their chosen models, e.g.
  #   "Kiro (auto) vs Claude Code (us.anthropic.claude-opus-4-8)"
  local label="${COMPARISON_LABEL}"
  if [ -z "$label" ]; then
    local parts="" i disp mdl
    for i in "${!SELECTED[@]}"; do
      disp="$(entry_field "${SELECTED[$i]}" 2)"
      mdl="${MODELS[$i]}"
      if [ -z "$parts" ]; then parts="${disp} (${mdl})"
      else parts="${parts} vs ${disp} (${mdl})"; fi
    done
    label="${parts}"
  fi
  # Escape double quotes so the YAML string stays valid.
  local label_yaml="${label//\"/\\\"}"
  log "comparison label: ${label}"

  {
    echo "# Generated by run.sh on ${TS} (host-local run)"
    echo "comparison_label: \"${label_yaml}\""
    echo "runners:"
    local i entry name display cli_path cost model
    for i in "${!SELECTED[@]}"; do
      entry="${SELECTED[$i]}"
      name="$(entry_field "$entry" 1)"
      display="$(entry_field "$entry" 2)"
      cli_path="$(entry_field "$entry" 3)"
      cost="$(entry_field "$entry" 4)"
      model="${MODELS[$i]}"          # per-CLI model chosen in select_models
      echo "  - name: ${name}"
      echo "    display_name: \"${display} (${model})\""
      echo "    cli_path: ${cli_path}"
      echo "    model_id: ${model}"
      # cost_source is auto-inferred by the harness from the binary name, so we
      # don't emit it explicitly except where the example does. The per-CLI
      # blocks below mirror config.cli-compare.example.yaml from the benchmark
      # repo (pricing, capabilities, and the exact arg shapes each CLI expects).
      case "$cost" in
        credit)
          # Kiro: effort passed as a flag; model_id is a plain slug.
          echo "    pricing:"
          echo "      usd_per_credit: 0.04"
          echo "    cli_base_args: [chat, --no-interactive, --trust-all-tools, \"--model={model}\", \"--effort={effort}\"]"
          ;;
        claude_json)
          # Claude Code: cost read from --output-format json (total_cost_usd).
          echo "    cli_base_args: [\"-p\", \"{prompt}\", \"--output-format\", \"json\", \"--model\", \"{model}\", \"--dangerously-skip-permissions\", \"--effort\", \"{effort}\"]"
          ;;
        copilot_json)
          # Copilot: cost from AIU telemetry in the JSON; no pricing block needed.
          echo "    cli_base_args: [\"-p\", \"{prompt}\", \"--model\", \"{model}\", \"--allow-all-tools\", \"--output-format\", \"json\", \"--effort\", \"{effort}\"]"
          ;;
        codex_json)
          # OpenAI Codex: `codex exec --json ... -m {model} {prompt}`.
          echo "    pricing:"
          echo "      usd_per_input_token:        0.000005     # \$5.00  / 1M"
          echo "      usd_per_cached_input_token: 0.0000005    # \$0.50  / 1M"
          echo "      usd_per_output_token:       0.000030     # \$30.00 / 1M"
          echo "    cli_base_args:"
          echo "      - \"exec\""
          echo "      - \"--json\""
          echo "      - \"--ephemeral\""
          echo "      - \"--skip-git-repo-check\""
          echo "      - \"--dangerously-bypass-approvals-and-sandbox\""
          echo "      - \"-m\""
          echo "      - \"{model}\""
          echo "      - \"{prompt}\""
          ;;
        cursor_json)
          # Cursor: effort baked into the model slug (harness appends -high/etc).
          # Needs --trust --yolo or it halts on "Workspace Trust Required".
          echo "    cost_source: cursor_json"
          echo "    pricing:"
          echo "      usd_per_input_token:        0.000005     # \$5.00  / 1M (fresh input)"
          echo "      usd_per_cache_write_token:  0.00000625   # \$6.25  / 1M (cache write)"
          echo "      usd_per_cached_input_token: 0.0000005    # \$0.50  / 1M (cache read)"
          echo "      usd_per_output_token:       0.000025     # \$25.00 / 1M"
          echo "    capabilities:"
          echo "      requires_pty: true"
          echo "    cli_base_args: [\"-p\", \"{prompt}\", \"--trust\", \"--yolo\", \"--output-format\", \"json\", \"--model\", \"{model}\"]"
          ;;
        antigravity_json)
          # Antigravity: base slug + harness-appended effort; NO --effort flag.
          # Needs --add-dir {workspace} (absolute) or it writes to its scratch
          # dir, and --print-timeout > timeout_minutes.
          echo "    cost_source: antigravity_json"
          echo "    pricing:"
          echo "      usd_per_input_token:  0.00000075   # \$0.75 / 1M (fresh input)"
          echo "      usd_per_output_token: 0.00000375   # \$3.75 / 1M"
          echo "    cli_base_args: [\"-p\", \"{prompt}\", \"--output-format\", \"json\", \"--model\", \"{model}\", \"--add-dir\", \"{workspace}\", \"--print-timeout\", \"30m\", \"--dangerously-skip-permissions\"]"
          ;;
        devin_export)
          # Devin: no JSON mode; --export writes an ATIF file with token counts.
          # Effort baked into model slug. devin_export_file must match --export.
          echo "    pricing:"
          echo "      usd_per_input_token:        0.000005     # \$5.00  / 1M (fresh input)"
          echo "      usd_per_cached_input_token: 0.0000005    # \$0.50  / 1M (cache read)"
          echo "      usd_per_output_token:       0.000025     # \$25.00 / 1M"
          echo "      devin_export_file: devin-usage.json"
          echo "    cli_base_args: [\"-p\", \"{prompt}\", \"--model\", \"{model}\", \"--export\", \"devin-usage.json\"]"
          ;;
      esac
    done

    # Execution + reporting tail (mirrors config.cli-compare.example.yaml).
    cat <<'YAML'

# Task discovery (cli-compare is vibe-only). An empty task_ids runs ALL tasks
# bundled in the framework. NOTE: some bundled tasks are Docker-graded (e.g.
# dotnet-invoicing, java-ratelimiter, typescript-circuit-breaker) and require a
# local Docker daemon; those will fail on a host without Docker. To restrict to
# a subset, list specific ids here instead.
tasks_dir: tasks
task_ids:
modes: ["vibe"]
YAML

    # Global reasoning effort — top-level run-level key applied across ALL CLIs.
    # For CLIs with an --effort flag it fills the {effort} placeholder; for
    # Cursor/Devin/Antigravity the harness appends it to the model slug. A
    # per-task effort in task.yaml overrides this. Emitted outside the quoted
    # heredoc so ${EFFORT} interpolates.
    echo ""
    echo "# Global reasoning effort for all runners (low|medium|high)."
    echo "# Per-task effort in a task.yaml overrides this run-level default."
    echo "effort: ${EFFORT}"

    cat <<'YAML'

# LLM-as-judge (only needed for rubric tasks). Uses the Kiro CLI by default;
# comment these out if Kiro isn't installed/authenticated.
judge_cli_path: kiro-cli
judge_model: claude-opus-4.8
judge_weight: 0.6

# Execution
concurrency: per_target
timeout_minutes: 20
repeats: 1
functional_pass_threshold: 0.99
workspace_base: /tmp/agent-cost-bench-cli-compare

# Devin permission policy copied into every workspace as `.devin/config.json`
# (other CLIs ignore it). Empty string skips the copy.
devin_permissions_file: tasks/devin/config.json
YAML

    # Reporting block — report_title reuses the comparison label so the report
    # headline matches. Emitted outside the quoted heredoc so it interpolates.
    echo ""
    echo "# Reporting"
    echo "output_dir: results"
    echo "report_title: \"${label_yaml}\""
    echo "open_report: false"
  } > "${CONFIG_FILE}"

  echo "${DIM}---- generated config ----${RST}"
  sed 's/^/    /' "${CONFIG_FILE}"
  echo "${DIM}---------------------------${RST}"
}

# ─────────────────────────────────────────────────────────────────────────────
# 4. Run the benchmark locally
# ─────────────────────────────────────────────────────────────────────────────
run_benchmark() {
  info "Step 4/4 — running the benchmark on this host"
  mkdir -p "${RESULTS_DIR}"
  # Ensure the venv + benchmark are active in this shell.
  if ! command -v agent-cost-bench >/dev/null 2>&1; then
    # shellcheck disable=SC1091
    [ -f "${VENV_DIR}/bin/activate" ] && . "${VENV_DIR}/bin/activate"
  fi
  command -v agent-cost-bench >/dev/null 2>&1 \
    || die "agent-cost-bench not available. Re-run without --skip-install, or activate the venv at ${VENV_DIR}."

  log "config:  ${CONFIG_FILE}"
  log "results: ${RESULTS_DIR}"
  log "tasks run from the benchmark checkout: ${CLONE_DIR}"

  # NOTE: task_ids/tasks_dir resolve relative to the working directory, so run
  # from the benchmark checkout (that's where the bundled tasks/ live).
  set +e
  ( cd "${CLONE_DIR}" && agent-cost-bench cli-compare run "${CONFIG_FILE}" \
      --output-dir "${RESULTS_DIR}" \
      --no-open )
  BENCH_EXIT=$?
  set -e
  log "benchmark exit code: ${BENCH_EXIT}"

  if [ -d "${RESULTS_DIR}" ] && [ -n "$(ls -A "${RESULTS_DIR}" 2>/dev/null)" ]; then
    info "report written to: ${RESULTS_DIR}"
    # Try to locate an HTML report to open.
    local html
    html="$(find "${RESULTS_DIR}" -maxdepth 2 -name '*.html' 2>/dev/null | head -1 || true)"
    if [ -n "$html" ]; then
      info "HTML report: ${html}"
      if [ "$OPEN_REPORT" -eq 1 ]; then
        if command -v open >/dev/null 2>&1; then open "$html" >/dev/null 2>&1 || true       # macOS
        elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$html" >/dev/null 2>&1 || true  # Linux
        fi
      fi
    fi
  else
    warn "no results were produced. Check the benchmark output above for errors."
  fi

  return "${BENCH_EXIT}"
}

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
main() {
  echo "${BOLD}agent-cost-bench run.sh — host-only local run${RST}"
  echo "${DIM}No AWS / ECS / Docker socket involved. Everything runs on this machine.${RST}"
  echo ""

  # Load any cached API keys before anything that needs them (model listing,
  # key collection). Explicit shell env still takes precedence.
  load_env_file

  preflight

  if [ "$SKIP_INSTALL" -eq 0 ]; then
    install_benchmark
  else
    # Each run normally gets its own fresh clone. With --skip-install we reuse
    # the MOST RECENT previous run's clone + venv instead of cloning again.
    log "--skip-install: reusing the most recent previous clone (no fresh clone)"
    local prev_run
    prev_run="$(ls -1dt "${BENCH_HOME}"/runs/*/ 2>/dev/null | head -1 || true)"
    if [ -n "$prev_run" ] && [ -d "${prev_run%/}/agent-cost-bench" ]; then
      RUN_DIR="${prev_run%/}"
      CLONE_DIR="${RUN_DIR}/agent-cost-bench"
      VENV_DIR="${RUN_DIR}/.venv"
      log "reusing ${CLONE_DIR}"
      # shellcheck disable=SC1091
      [ -f "${VENV_DIR}/bin/activate" ] && . "${VENV_DIR}/bin/activate"
    else
      warn "--skip-install set but no previous clone found under ${BENCH_HOME}/runs/."
      warn "re-run without --skip-install to clone the benchmark first."
    fi
  fi

  if [ -n "${EXPLICIT_CONFIG}" ]; then
    [ -f "${EXPLICIT_CONFIG}" ] || die "--config file not found: ${EXPLICIT_CONFIG}"
    CONFIG_FILE="${EXPLICIT_CONFIG}"
    info "using existing config, skipping selection/keys: ${CONFIG_FILE}"
    warn "make sure the API keys for its runners are exported in your environment."
  else
    select_runners
    # Install CLIs, then show the auth notice and (optionally) run logins, then
    # collect API keys — all BEFORE listing models, since some CLIs (Cursor,
    # Devin) only list models once installed and authenticated.
    if [ "$SKIP_INSTALL" -eq 0 ]; then
      install_selected_clis
    fi
    print_auth_notice
    collect_keys
    offer_logins
    select_models
    select_effort
    generate_config
  fi

  run_benchmark
}

main "$@"
