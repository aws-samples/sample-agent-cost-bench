#!/usr/bin/env bash
# ============================================================================
# agent-cost-bench container entrypoint
#
# Installs/updates coding CLIs on first run, starts Docker daemon if running
# in privileged mode, detects available CLIs, and drops into the user's command.
# ============================================================================

set -euo pipefail

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}▸${NC} $*"; }
ok()    { echo -e "${GREEN}✓${NC} $*"; }
warn()  { echo -e "${YELLOW}⚠${NC} $*"; }

MARKER="/opt/.clis-installed"

# ---------------------------------------------------------------------------
# Install CLIs (first run only — takes ~30s)
# ---------------------------------------------------------------------------

if [[ ! -f "$MARKER" ]]; then
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}  First run — installing coding CLIs...${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""

    # Kiro CLI
    info "Installing Kiro CLI..."
    if curl -fsSL https://cli.kiro.dev/install 2>/dev/null | bash 2>/dev/null; then
        ok "Kiro CLI installed"
    else
        warn "Kiro CLI install failed (install manually or check https://kiro.dev/docs/getting-started/installation)"
    fi

    # Claude Code
    info "Installing Claude Code..."
    if curl -fsSL https://claude.ai/install.sh 2>/dev/null | bash 2>/dev/null; then
        ok "Claude Code installed"
    else
        # Fallback to npm
        if npm install -g @anthropic-ai/claude-code 2>/dev/null; then
            ok "Claude Code installed (via npm)"
        else
            warn "Claude Code install failed"
        fi
    fi

    # GitHub Copilot CLI
    info "Installing GitHub Copilot CLI..."
    if curl -fsSL https://gh.io/copilot-install 2>/dev/null | bash 2>/dev/null; then
        ok "Copilot CLI installed"
    else
        if npm install -g @github/copilot 2>/dev/null; then
            ok "Copilot CLI installed (via npm)"
        else
            warn "Copilot CLI install failed"
        fi
    fi

    # Cursor CLI
    info "Installing Cursor CLI..."
    if curl -fsSL https://cursor.com/install 2>/dev/null | bash 2>/dev/null; then
        ok "Cursor CLI installed"
    else
        warn "Cursor CLI install failed"
    fi

    # Devin CLI
    info "Installing Devin CLI..."
    if curl -fsSL https://cli.devin.ai/install.sh 2>/dev/null | bash 2>/dev/null; then
        ok "Devin CLI installed"
    else
        warn "Devin CLI install failed"
    fi

    touch "$MARKER"
    echo ""
fi

# ---------------------------------------------------------------------------
# Start Docker daemon (if privileged)
# ---------------------------------------------------------------------------

if [[ -e /var/run/docker.sock ]] || [[ -w /var/run ]]; then
    if ! docker info &>/dev/null 2>&1; then
        info "Starting Docker daemon..."
        dockerd &>/var/log/dockerd.log &
        # Wait for Docker to be ready (max 15s)
        for i in $(seq 1 30); do
            if docker info &>/dev/null 2>&1; then
                ok "Docker daemon ready"
                break
            fi
            sleep 0.5
        done
    fi
fi

# ---------------------------------------------------------------------------
# Ensure PATH includes CLI install locations
# ---------------------------------------------------------------------------

export PATH="$HOME/.local/bin:$HOME/.kiro/bin:/usr/local/bin:$PATH"

# ---------------------------------------------------------------------------
# Print status
# ---------------------------------------------------------------------------

echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}  agent-cost-bench sandbox${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  CLIs:"
for cli in kiro-cli claude copilot agent devin; do
    if command -v "$cli" &>/dev/null; then
        echo -e "    ${GREEN}✓${NC} $cli"
    else
        echo -e "    ${YELLOW}✗${NC} $cli (not installed)"
    fi
done
echo ""
echo "  API Keys:"
[[ -n "${KIRO_API_KEY:-}" ]]      && echo -e "    ${GREEN}✓${NC} KIRO_API_KEY"      || echo -e "    ${YELLOW}✗${NC} KIRO_API_KEY"
[[ -n "${ANTHROPIC_API_KEY:-}" ]] && echo -e "    ${GREEN}✓${NC} ANTHROPIC_API_KEY" || echo -e "    ${YELLOW}✗${NC} ANTHROPIC_API_KEY"
[[ -n "${GITHUB_TOKEN:-}" ]]      && echo -e "    ${GREEN}✓${NC} GITHUB_TOKEN"      || echo -e "    ${YELLOW}✗${NC} GITHUB_TOKEN"
echo ""

# Check Docker
if docker info &>/dev/null 2>&1; then
    IMAGES=$(docker images --format '{{.Repository}}' 2>/dev/null | grep -c "agent-cost-bench" || echo "0")
    echo -e "  Docker: ${GREEN}✓${NC} running ($IMAGES verification images)"
    if [[ "$IMAGES" -eq 0 ]]; then
        echo "    Run: ./tasks/docker/build-images.sh  (for Docker-verified tasks)"
    fi
else
    echo -e "  Docker: ${YELLOW}✗${NC} not available (run with --privileged for Docker tasks)"
fi

echo ""
echo "  Commands:"
echo "    ./setup.sh           # Auto-detect CLIs, generate config"
echo "    ./quickstart.sh      # Run a 2-task benchmark"
echo "    bash                 # Interactive shell"
echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# ---------------------------------------------------------------------------
# Execute the user's command (default: bash)
# ---------------------------------------------------------------------------

exec "$@"
