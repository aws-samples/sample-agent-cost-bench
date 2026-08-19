#!/usr/bin/env bash
# ============================================================================
# agent-cost-bench — setup.sh
#
# Prepares the environment for running benchmarks:
#   1. Checks Python version
#   2. Creates a virtual environment
#   3. Installs the framework
#   4. Detects which coding CLIs are installed and authenticated
#   5. Auto-generates a config for detected CLIs
#   6. Optionally builds Docker verification images
#   7. Runs validation
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/aws-samples/sample-agent-cost-bench/main/setup.sh | bash
#   # or after cloning:
#   ./setup.sh
# ============================================================================

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No color

info()  { echo -e "${BLUE}▸${NC} $*"; }
ok()    { echo -e "${GREEN}✓${NC} $*"; }
warn()  { echo -e "${YELLOW}⚠${NC} $*"; }
fail()  { echo -e "${RED}✗${NC} $*"; }

echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}  agent-cost-bench — Setup${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Check Python
# ---------------------------------------------------------------------------

info "Checking Python..."
if command -v python3 &>/dev/null; then
    PY=$(python3 --version 2>&1 | awk '{print $2}')
    PY_MAJOR=$(echo "$PY" | cut -d. -f1)
    PY_MINOR=$(echo "$PY" | cut -d. -f2)
    if [[ "$PY_MAJOR" -ge 3 && "$PY_MINOR" -ge 10 ]]; then
        ok "Python $PY"
    else
        fail "Python 3.10+ required (found $PY)"
        exit 1
    fi
else
    fail "Python 3 not found. Install Python 3.10+ and try again."
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 2: Virtual environment
# ---------------------------------------------------------------------------

VENV_DIR=".venv"

if [[ -d "$VENV_DIR" && -f "$VENV_DIR/bin/activate" ]]; then
    info "Using existing virtual environment"
else
    info "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
    ok "Virtual environment created at $VENV_DIR/"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# ---------------------------------------------------------------------------
# Step 3: Install the framework
# ---------------------------------------------------------------------------

info "Installing agent-cost-bench..."
pip install -e . --quiet 2>/dev/null
ok "agent-cost-bench installed"

# ---------------------------------------------------------------------------
# Step 4: Detect CLIs
# ---------------------------------------------------------------------------

echo ""
info "Detecting installed coding CLIs..."
echo ""

declare -a DETECTED_CLIS=()

# Kiro
if command -v kiro-cli &>/dev/null; then
    ok "Kiro CLI found (kiro-cli)"
    DETECTED_CLIS+=("kiro")
else
    warn "Kiro CLI not found (kiro-cli)"
fi

# Claude Code
if command -v claude &>/dev/null; then
    ok "Claude Code found (claude)"
    DETECTED_CLIS+=("claude-code")
else
    warn "Claude Code not found (claude)"
fi

# GitHub Copilot
if command -v copilot &>/dev/null; then
    ok "GitHub Copilot CLI found (copilot)"
    DETECTED_CLIS+=("copilot")
else
    warn "GitHub Copilot CLI not found (copilot)"
fi

# Cursor
if command -v agent &>/dev/null; then
    ok "Cursor CLI found (agent)"
    DETECTED_CLIS+=("cursor")
else
    warn "Cursor CLI not found (agent)"
fi

# Devin
if command -v devin &>/dev/null; then
    ok "Devin CLI found (devin)"
    DETECTED_CLIS+=("devin")
else
    warn "Devin CLI not found (devin)"
fi

echo ""

if [[ ${#DETECTED_CLIS[@]} -lt 2 ]]; then
    fail "At least 2 CLIs are needed for a comparison. Found: ${#DETECTED_CLIS[@]}"
    echo ""
    echo "  Install and authenticate your CLIs, then re-run ./setup.sh"
    echo "  See: workshop/01-setup.md for installation instructions"
    echo ""
    exit 1
fi

ok "Found ${#DETECTED_CLIS[@]} CLIs: ${DETECTED_CLIS[*]}"

# ---------------------------------------------------------------------------
# Step 5: Generate config
# ---------------------------------------------------------------------------

echo ""
info "Generating benchmark config..."

CONFIG_FILE="config.generated.yaml"

# Ask for model (minimal interaction)
echo ""
echo "  Which model should all CLIs use?"
echo "    1) claude-opus-4.8  (most capable, higher cost)"
echo "    2) claude-sonnet-4.6  (balanced, lower cost)"
echo "    3) claude-sonnet-5  (latest sonnet)"
echo ""
read -r -p "  Choose [1/2/3, default=2]: " MODEL_CHOICE
echo ""

case "${MODEL_CHOICE:-2}" in
    1) MODEL_ID="claude-opus-4.8" ; MODEL_SLUG="claude-opus-4-8" ;;
    3) MODEL_ID="claude-sonnet-5" ; MODEL_SLUG="claude-sonnet-5" ;;
    *) MODEL_ID="claude-sonnet-4.6" ; MODEL_SLUG="claude-sonnet-4-6" ;;
esac

# Build runners YAML
RUNNERS=""

for cli in "${DETECTED_CLIS[@]}"; do
    case "$cli" in
        kiro)
            RUNNERS+="
  - name: kiro
    display_name: \"Kiro ($MODEL_ID)\"
    cli_path: kiro-cli
    model_id: $MODEL_ID
    pricing:
      usd_per_credit: 0.04
    cli_base_args: [chat, --no-interactive, --trust-all-tools,
                    \"--model={model}\", \"--effort={effort}\"]
"
            ;;
        claude-code)
            RUNNERS+="
  - name: claude-code
    display_name: \"Claude Code ($MODEL_ID)\"
    cli_path: claude
    model_id: $MODEL_SLUG
    cli_base_args: [\"-p\", \"{prompt}\", \"--output-format\", \"json\",
                    \"--model\", \"{model}\", \"--dangerously-skip-permissions\",
                    \"--effort\", \"{effort}\"]
"
            ;;
        copilot)
            RUNNERS+="
  - name: copilot
    display_name: \"GitHub Copilot ($MODEL_ID)\"
    cli_path: copilot
    model_id: $MODEL_ID
    pricing:
      usd_per_premium_request: 0.04
    cli_base_args: [\"-p\", \"{prompt}\", \"--model\", \"{model}\",
                    \"--allow-all-tools\", \"--output-format\", \"json\",
                    \"--effort\", \"{effort}\"]
"
            ;;
        cursor)
            RUNNERS+="
  - name: cursor
    display_name: \"Cursor ($MODEL_ID)\"
    cli_path: agent
    model_id: $MODEL_SLUG
    pricing:
      usd_per_input_token: 0.000005
      usd_per_cache_write_token: 0.00000625
      usd_per_cached_input_token: 0.0000005
      usd_per_output_token: 0.000025
    cli_base_args: [\"-p\", \"{prompt}\", \"--trust\", \"--yolo\",
                    \"--output-format\", \"json\", \"--model\", \"{model}\"]
"
            ;;
        devin)
            RUNNERS+="
  - name: devin
    display_name: \"Devin ($MODEL_ID)\"
    cli_path: devin
    model_id: $MODEL_SLUG
    pricing:
      usd_per_input_token: 0.000005
      usd_per_cached_input_token: 0.0000005
      usd_per_output_token: 0.000025
      devin_export_file: devin-usage.json
    cli_base_args: [\"-p\", \"{prompt}\", \"--model\", \"{model}\",
                    \"--export\", \"devin-usage.json\"]
"
            ;;
    esac
done

CLI_NAMES=$(printf '%s / ' "${DETECTED_CLIS[@]}" | sed 's/ \/ $//')

cat > "$CONFIG_FILE" <<EOF
# Auto-generated by setup.sh — $(date -u +"%Y-%m-%d %H:%M UTC")
# CLIs: $CLI_NAMES
# Model: $MODEL_ID

comparison_label: "$MODEL_ID ($CLI_NAMES)"

runners:$RUNNERS
tasks_dir: tasks
task_ids:
modes: ["vibe"]

concurrency: per_target
timeout_minutes: 20
repeats: 1
functional_pass_threshold: 0.99
workspace_base: /tmp/agent-cost-bench-runs

output_dir: results
report_title: "Benchmark: $MODEL_ID ($CLI_NAMES)"
open_report: true
EOF

ok "Config written to $CONFIG_FILE"

# ---------------------------------------------------------------------------
# Step 6: Docker images (optional)
# ---------------------------------------------------------------------------

echo ""
RUNTIME="${CONTAINER_RUNTIME:-docker}"

if command -v "$RUNTIME" &>/dev/null; then
    info "Docker/Finch detected ($RUNTIME). Build verification images?"
    read -r -p "  Build now? [y/N]: " BUILD_DOCKER
    if [[ "${BUILD_DOCKER:-n}" =~ ^[Yy] ]]; then
        info "Building Docker verification images (this takes a few minutes)..."
        ./tasks/docker/build-images.sh
        ok "Docker images built"
    else
        warn "Skipping Docker images. Tasks requiring Docker will fail."
        echo "  Run later with: ./tasks/docker/build-images.sh"
    fi
else
    warn "Docker not found. Tasks requiring containers (dotnet, java, typescript, terraform, helm) will be skipped."
    echo "  Install Docker and run: ./tasks/docker/build-images.sh"
fi

# ---------------------------------------------------------------------------
# Step 7: Validate
# ---------------------------------------------------------------------------

echo ""
info "Running validation..."
echo ""

if agent-cost-bench cli-compare validate "$CONFIG_FILE" 2>/dev/null; then
    ok "Validation passed"
else
    warn "Validation had warnings (some CLIs may not be fully authenticated)"
    echo "  Check the output above and ensure each CLI is logged in."
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  Setup complete!${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  Detected CLIs:  ${DETECTED_CLIS[*]}"
echo "  Model:          $MODEL_ID"
echo "  Config:         $CONFIG_FILE"
echo ""
echo "  Next steps:"
echo "    source .venv/bin/activate"
echo "    ./quickstart.sh            # Run a quick 2-task benchmark"
echo "    # or"
echo "    agent-cost-bench cli-compare run $CONFIG_FILE  # Full run"
echo ""
