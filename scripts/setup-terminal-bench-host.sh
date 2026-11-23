#!/usr/bin/env bash
# setup-terminal-bench-host.sh — prepare an Ubuntu 24.04 x86_64 EC2 host to run
# Terminal-Bench 4.x tasks through agent-cost-bench.
#
# Why this exists: agent-cost-bench runs the coding CLI on the HOST and only
# grades inside Docker. Many Terminal-Bench tasks expect the agent to run tools
# that the task's own image provides (Coq, Lean, Sage, FreeCAD, VEP, Verilator,
# QEMU, PyTorch, ...). This script installs those tools on the host so the agent
# can find them. Versions are taken from the task Dockerfiles / lock files at
# the Terminal-Bench ref below wherever the task pins them.
#
# What it does NOT fix (see the end-of-run summary):
#   * docker-compose tasks (11) and GPU tasks (3) still need harness changes.
#   * Tasks that edit code baked into their image (sglang-qwen-burst,
#     vllm-deepseek-streaming, vpp-loss-divergence) still need the importer to
#     copy that code out of the image.
#   * One shared Python environment cannot match every task's pins (e.g.
#     numpy 1.26 vs 2.4); it is "close enough", not identical to each image.
#
# Usage (run as the user that will run the benchmark, NOT as root):
#   bash scripts/setup-terminal-bench-host.sh
#
# Options (environment variables):
#   TB_REF=v4.1.0            Terminal-Bench tag to read pins/lock files from
#   TB_HOME=/opt/tb          install prefix for everything this script adds
#   INSTALL_CLIS="kiro"      space-separated: kiro devin claude-code copilot
#                            codex cursor antigravity
#   REPO_DIR=<path>          existing agent-cost-bench checkout to install
#                            (default: clone REPO_URL into $TB_HOME/agent-cost-bench)
#   REPO_URL=https://github.com/aws-samples/sample-agent-cost-bench.git
#   DOCKER_DATA_ROOT=<path>  optional: put Docker images on a separate volume
#   SKIP_DOCKER=1 SKIP_RUNTIMES=1 SKIP_SYSTEM_TOOLS=1 SKIP_PYTHON=1
#   SKIP_CONDA=1 SKIP_COQ=1 SKIP_LEAN=1 SKIP_CLIS=1   skip a section
#
# The script is idempotent: re-running it skips anything already installed.

set -euo pipefail

TB_REF="${TB_REF:-v4.1.0}"
TB_HOME="${TB_HOME:-/opt/tb}"
TB_REPO_URL="https://github.com/harbor-framework/terminal-bench.git"
TB_DIR="${TB_HOME}/terminal-bench-${TB_REF}"
INSTALL_CLIS="${INSTALL_CLIS:-kiro}"
REPO_URL="${REPO_URL:-https://github.com/aws-samples/sample-agent-cost-bench.git}"
REPO_DIR="${REPO_DIR:-}"
LOG_FILE="${TB_HOME}/setup.log"

# Pins copied from Terminal-Bench 4.1 task Dockerfiles / lock files.
UV_VERSION="0.9.7"                                    # atrx-vep-crispr
BUN_VERSION="1.2.15"                                  # bun-sourcemap-leak
SBT_VERSION="1.9.6"                                   # distributed-dedup
SBT_SHA256="923d7917ccb99a9fd985f4abfd81caacaed42284e67d3f7696cc5239e7c595cb"
OPAM_VERSION="2.3.0"                                  # coq-block-bound
OPAM_SHA256="324e78e3f33efeba279aacf9f9610cfec7b2df7d7e0e1640f75f09de85f96cc9"
OPAM_REPO_COMMIT="b0f29298c11859482484f6237c75de5d6d4682a7"
COQ_REPO_COMMIT="564ab90f5874c0c91cee746c4706bc2ba686a5a9"
OPAM_SWITCH="4.13.1+flambda"
MINIFORGE_VERSION="26.1.1-3"                          # formal-crypto
LEAN_TOOLCHAIN="leanprover/lean4:v4.28.0"             # takens-embedding-lean
NODE_MAJOR="22"                                       # most Node tasks use 22

PROFILE_FILE="/etc/profile.d/terminal-bench-tools.sh"

# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────
c_info=$'\033[1;34m'; c_warn=$'\033[1;33m'; c_err=$'\033[1;31m'; c_off=$'\033[0m'
info() { printf '%s==>%s %s\n' "$c_info" "$c_off" "$*"; }
warn() { printf '%sWARN%s %s\n' "$c_warn" "$c_off" "$*" >&2; }
die()  { printf '%sERROR%s %s\n' "$c_err" "$c_off" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
skip() { local v="SKIP_$1"; [[ "${!v:-0}" == "1" ]]; }

# Install only the apt packages this release knows about, so one renamed
# package doesn't abort the whole run.
apt_install() {
  local available=() missing=() p
  for p in "$@"; do
    if apt-cache show "$p" >/dev/null 2>&1; then available+=("$p"); else missing+=("$p"); fi
  done
  ((${#missing[@]})) && warn "apt packages not available, skipped: ${missing[*]}"
  ((${#available[@]})) && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${available[@]}"
  return 0
}

# Download $1 to $2 and check it against sha256 $3.
fetch_verified() {
  local url="$1" out="$2" sha="$3"
  curl -fsSL --retry 3 "$url" -o "$out"
  echo "${sha}  ${out}" | sha256sum -c - >/dev/null || die "checksum mismatch for $url"
}

# ─────────────────────────────────────────────────────────────────────────────
# 0. preflight
# ─────────────────────────────────────────────────────────────────────────────
preflight() {
  [[ "$EUID" -ne 0 ]] || die "run as the benchmark user (with sudo rights), not as root"
  have sudo || die "sudo is required"
  # Passwordless sudo first: `sudo -v` asks for a password whenever any sudoers
  # rule needs one (e.g. %sudo on stock Ubuntu AMIs, even with NOPASSWD for the
  # user), which fails in non-interactive runs such as cloud-init or SSM.
  sudo -n true 2>/dev/null || sudo -v || die "sudo authentication failed"

  . /etc/os-release
  [[ "${ID:-}" == "ubuntu" ]] || die "this script targets Ubuntu 24.04 (found ${PRETTY_NAME:-unknown})"
  [[ "${VERSION_ID:-}" == "24.04" ]] || warn "tested target is Ubuntu 24.04; found ${VERSION_ID:-unknown}"
  [[ "$(uname -m)" == "x86_64" ]] || die "x86_64 required (QEMU, vLLM CPU image and some reference binaries assume it)"

  local free_gb
  free_gb=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
  ((free_gb >= 300)) || warn "only ${free_gb} GB free on /; plan for 500 GB+ (Docker images, conda envs, Lean caches)"

  sudo mkdir -p "$TB_HOME"
  sudo chown "$(id -u):$(id -g)" "$TB_HOME"
  mkdir -p "$TB_HOME/bin"
  exec > >(tee -a "$LOG_FILE") 2>&1
  info "logging to $LOG_FILE"
}

# ─────────────────────────────────────────────────────────────────────────────
# 1. base packages
# ─────────────────────────────────────────────────────────────────────────────
install_base() {
  info "base packages"
  sudo apt-get update -y
  apt_install ca-certificates curl wget git gnupg jq unzip zip xz-utils bzip2 \
    build-essential cmake pkg-config autoconf automake m4 libssl-dev libgmp-dev \
    python3 python3-pip python3-venv python3-dev tmux rsync rlwrap time less \
    procps psmisc net-tools iproute2 netcat-openbsd strace ltrace gdb binutils \
    sqlite3 vim-tiny
}

# ─────────────────────────────────────────────────────────────────────────────
# 2. Docker Engine + buildx + compose (unix socket only; no TCP listener)
# ─────────────────────────────────────────────────────────────────────────────
install_docker() {
  if have docker && docker buildx version >/dev/null 2>&1; then
    info "docker already installed: $(docker --version)"
  else
    info "Docker Engine (official apt repo)"
    sudo install -m 0755 -d /etc/apt/keyrings
    sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    sudo chmod a+r /etc/apt/keyrings/docker.asc
    . /etc/os-release
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" \
      | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
    sudo apt-get update -y
    apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  fi

  if [[ -n "${DOCKER_DATA_ROOT:-}" ]]; then
    if [[ -f /etc/docker/daemon.json ]]; then
      warn "/etc/docker/daemon.json exists; not changing data-root (set it to ${DOCKER_DATA_ROOT} by hand)"
    else
      sudo mkdir -p "$DOCKER_DATA_ROOT" /etc/docker
      printf '{\n  "data-root": "%s"\n}\n' "$DOCKER_DATA_ROOT" | sudo tee /etc/docker/daemon.json >/dev/null
      info "Docker data-root -> $DOCKER_DATA_ROOT"
    fi
  fi

  sudo systemctl enable --now docker
  sudo systemctl restart docker
  if ! id -nG "$USER" | grep -qw docker; then
    sudo usermod -aG docker "$USER"
    warn "added $USER to the docker group (root-equivalent); log out and back in for it to apply"
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
# 3. language runtimes: uv + Python 3.11/3.13, Node, Bun, JDK 17 + sbt, Rust
# ─────────────────────────────────────────────────────────────────────────────
install_runtimes() {
  info "language runtimes"

  if ! have uv; then
    curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" \
      | env UV_INSTALL_DIR="$TB_HOME/bin" UV_NO_MODIFY_PATH=1 sh
  fi
  export PATH="$TB_HOME/bin:$PATH"
  # Tasks span Python 3.11-3.13; 3.12 is the Ubuntu system Python. This puts
  # python3.11 / python3.13 executables in $TB_HOME/bin.
  UV_PYTHON_INSTALL_DIR="$TB_HOME/python" UV_PYTHON_BIN_DIR="$TB_HOME/bin" uv python install 3.11 3.13

  if [[ ! -x "$TB_HOME/node/bin/node" ]]; then
    local base="https://nodejs.org/dist/latest-v${NODE_MAJOR}.x" tarball sha tmp
    tmp=$(mktemp -d)
    curl -fsSL "$base/SHASUMS256.txt" -o "$tmp/SHASUMS256.txt"
    tarball=$(grep -oE "node-v[0-9.]+-linux-x64\.tar\.xz" "$tmp/SHASUMS256.txt" | head -1)
    sha=$(grep " ${tarball}\$" "$tmp/SHASUMS256.txt" | cut -d' ' -f1)
    fetch_verified "$base/$tarball" "$tmp/$tarball" "$sha"
    rm -rf "$TB_HOME/node" && mkdir -p "$TB_HOME/node"
    tar -xJf "$tmp/$tarball" -C "$TB_HOME/node" --strip-components=1
    rm -rf "$tmp"
  fi

  if [[ ! -x "$TB_HOME/bun/bin/bun" ]]; then
    curl -fsSL https://bun.sh/install | env BUN_INSTALL="$TB_HOME/bun" bash -s "bun-v${BUN_VERSION}"
  fi

  apt_install openjdk-17-jdk-headless
  if [[ ! -x "$TB_HOME/sbt/bin/sbt" ]]; then
    local tmp; tmp=$(mktemp -d)
    fetch_verified "https://github.com/sbt/sbt/releases/download/v${SBT_VERSION}/sbt-${SBT_VERSION}.tgz" \
      "$tmp/sbt.tgz" "$SBT_SHA256"
    tar -xzf "$tmp/sbt.tgz" -C "$TB_HOME"
    rm -rf "$tmp"
  fi

  if [[ ! -x "$TB_HOME/cargo/bin/cargo" ]]; then
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
      | env RUSTUP_HOME="$TB_HOME/rustup" CARGO_HOME="$TB_HOME/cargo" \
        sh -s -- -y --profile minimal --no-modify-path
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
# 4. system tools used by specific tasks
# ─────────────────────────────────────────────────────────────────────────────
install_system_tools() {
  info "task tools (apt)"
  # Media / OCR / documents: satb-audio-transcription, music-harmony, OCR tasks
  apt_install ffmpeg libsndfile1 tesseract-ocr tesseract-ocr-deu imagemagick \
    poppler-utils libgl1 libglib2.0-0t64 libx11-6 libsm6
  # uefi-bootkit: QEMU + OVMF firmware
  apt_install qemu-system-x86 qemu-utils ovmf
  # retro-console-soc: Verilog simulation and ECP5 place-and-route
  apt_install verilator yosys nextpnr-ecp5 fpga-trellis fpga-trellis-database
  # atrx-vep-crispr: Ensembl VEP runtime (VEP itself ships in the task data)
  apt_install perl cpanminus libdbi-perl libdbd-mysql-perl libbio-perl-perl \
    libbio-db-hts-perl libset-intervaltree-perl libjson-perl \
    libfile-copy-recursive-perl libtry-tiny-perl libcgi-pm-perl \
    libtest-warnings-perl libtest-exception-perl libtest-deep-perl samtools tabix

  # QEMU is much faster with KVM; /dev/kvm exists only on metal or when
  # nested virtualization is enabled on the instance.
  if [[ -e /dev/kvm ]]; then
    id -nG "$USER" | grep -qw kvm || sudo usermod -aG kvm "$USER"
  else
    warn "/dev/kvm not present: uefi-bootkit will run QEMU without KVM (slow). Enable nested virtualization on the instance."
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
# 5. shared Python environment (also hosts the agent-cost-bench harness)
# ─────────────────────────────────────────────────────────────────────────────
install_python_env() {
  info "shared Python env at $TB_HOME/py"
  export PATH="$TB_HOME/bin:$PATH"
  have uv || die "uv not found; run without SKIP_RUNTIMES=1 first"
  local py="$TB_HOME/py/bin/python" pkg
  [[ -x "$py" ]] || uv venv "$TB_HOME/py" --python 3.12

  # CPU-only PyTorch first (lake-temp-glm, mp-checkpoint-consolidation,
  # pretrain-shard-corruption); the default PyPI wheel pulls in CUDA.
  uv pip install --python "$py" --index-url https://download.pytorch.org/whl/cpu torch
  # Union of the agent-image dependencies across TB 4.1 tasks (unpinned:
  # tasks disagree, e.g. numpy 1.26.4 vs 2.4.6).
  uv pip install --python "$py" \
    numpy scipy pandas matplotlib networkx openpyxl pyyaml requests \
    beautifulsoup4 lxml rdflib shapely rtree pillow pycryptodome \
    safetensors sentencepiece autograd playwright pytest
  # Heavier or wheel-sensitive packages, one at a time so one failure doesn't
  # stop the rest: rdkit (roy-polymorph-cn), drake (biped-contact-dynamics),
  # litdata (pretrain-shard-corruption), nlopt (wdm-design), music21 (music tasks).
  for pkg in rdkit drake litdata nlopt music21; do
    uv pip install --python "$py" "$pkg" || warn "could not install $pkg"
  done

  # Playwright browser + its system libraries (layout-config-recreation).
  sudo "$TB_HOME/py/bin/playwright" install-deps chromium
  PLAYWRIGHT_BROWSERS_PATH="$TB_HOME/ms-playwright" "$TB_HOME/py/bin/playwright" install chromium

  # The harness itself.
  if [[ -z "$REPO_DIR" ]]; then
    REPO_DIR="$TB_HOME/agent-cost-bench"
    [[ -d "$REPO_DIR/.git" ]] || git clone "$REPO_URL" "$REPO_DIR"
  fi
  [[ -f "$REPO_DIR/pyproject.toml" ]] || die "REPO_DIR=$REPO_DIR is not an agent-cost-bench checkout"
  uv pip install --python "$py" -e "$REPO_DIR"
}

# ─────────────────────────────────────────────────────────────────────────────
# 6. Terminal-Bench checkout (source of the conda/opam lock files)
# ─────────────────────────────────────────────────────────────────────────────
fetch_terminal_bench() {
  if [[ ! -d "$TB_DIR/tasks" ]]; then
    info "cloning Terminal-Bench $TB_REF"
    git -c advice.detachedHead=false clone -q --depth 1 --branch "$TB_REF" "$TB_REPO_URL" "$TB_DIR"
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
# 7. conda envs from the tasks' own lock files: Sage, FreeCAD, Meep
# ─────────────────────────────────────────────────────────────────────────────
install_conda_envs() {
  info "conda envs (Sage, FreeCAD, Meep)"
  local conda="$TB_HOME/conda/bin/conda"
  if [[ ! -x "$conda" ]]; then
    local f="Miniforge3-${MINIFORGE_VERSION}-Linux-x86_64.sh" tmp sha
    local base="https://github.com/conda-forge/miniforge/releases/download/${MINIFORGE_VERSION}"
    tmp=$(mktemp -d)
    sha=$(curl -fsSL "$base/$f.sha256" | cut -d' ' -f1)
    fetch_verified "$base/$f" "$tmp/$f" "$sha"
    bash "$tmp/$f" -b -p "$TB_HOME/conda"
    rm -rf "$tmp"
  fi

  local name lockdir
  for pair in "sage:formal-crypto" "freecad:freecad-impeller" "meep:wdm-design"; do
    name="${pair%%:*}"; lockdir="$TB_DIR/tasks/${pair#*:}/environment"
    if [[ -d "$TB_HOME/conda/envs/$name" ]]; then continue; fi
    [[ -f "$lockdir/conda-linux-64.lock" ]] || { warn "no lock file in $lockdir; skipping $name"; continue; }
    "$conda" create -y -n "$name" --file "$lockdir/conda-linux-64.lock"
  done
  "$conda" clean -afy >/dev/null

  # Wrappers so the agent finds the tools by their usual names.
  local envs="$TB_HOME/conda/envs"
  [[ -x "$envs/sage/bin/sage" ]] && ln -sf "$envs/sage/bin/sage" "$TB_HOME/bin/sage"
  [[ -x "$envs/freecad/bin/freecadcmd" ]] && ln -sf "$envs/freecad/bin/freecadcmd" "$TB_HOME/bin/freecadcmd"
  [[ -x "$envs/meep/bin/python" ]] && ln -sf "$envs/meep/bin/python" "$TB_HOME/bin/python-meep"
  return 0
}

# ─────────────────────────────────────────────────────────────────────────────
# 8. Coq 8.18 via opam (coq-block-bound's locked toolchain)
# ─────────────────────────────────────────────────────────────────────────────
install_coq() {
  info "Coq via opam (takes 20-40 minutes)"
  export OPAMROOT="$TB_HOME/opam" OPAMYES=1
  if [[ ! -x "$TB_HOME/bin/opam" ]]; then
    fetch_verified "https://github.com/ocaml/opam/releases/download/${OPAM_VERSION}/opam-${OPAM_VERSION}-x86_64-linux" \
      "$TB_HOME/bin/opam" "$OPAM_SHA256"
    chmod 755 "$TB_HOME/bin/opam"
  fi
  local opam="$TB_HOME/bin/opam"
  if [[ ! -x "$OPAMROOT/$OPAM_SWITCH/bin/coqc" ]]; then
    [[ -d "$OPAMROOT" ]] || "$opam" init --bare --disable-sandboxing -y \
      default "https://github.com/ocaml/opam-repository.git#${OPAM_REPO_COMMIT}"
    "$opam" switch list --short | grep -qx "$OPAM_SWITCH" || "$opam" switch create "$OPAM_SWITCH" --empty -y
    local tmp; tmp=$(mktemp -d)
    curl -fsSL "https://github.com/rocq-prover/opam/archive/${COQ_REPO_COMMIT}.tar.gz" | tar -xz -C "$tmp"
    # Fails harmlessly when a previous run already added it.
    "$opam" repository add coq-released "$tmp/opam-${COQ_REPO_COMMIT}/released" --switch "$OPAM_SWITCH" -y || true
    mkdir -p "$tmp/coq-deps"
    cp "$TB_DIR/tasks/coq-block-bound/environment/coq-toolchain.opam"* "$tmp/coq-deps/"
    "$opam" install "$tmp/coq-deps" --switch "$OPAM_SWITCH" --deps-only --locked -y --jobs="$(nproc)"
    "$opam" clean -a -c -s --logs
    rm -rf "$tmp"
  fi
  for b in coqc coqtop coqdep coqchk; do
    ln -sf "$OPAMROOT/$OPAM_SWITCH/bin/$b" "$TB_HOME/bin/$b"
  done
}

# ─────────────────────────────────────────────────────────────────────────────
# 9. Lean 4 via elan (Mathlib is fetched per project with `lake exe cache get`)
# ─────────────────────────────────────────────────────────────────────────────
install_lean() {
  info "Lean 4 ($LEAN_TOOLCHAIN)"
  if [[ ! -x "$TB_HOME/elan/bin/lean" ]]; then
    curl -sSf https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh \
      | env ELAN_HOME="$TB_HOME/elan" sh -s -- -y --no-modify-path --default-toolchain "$LEAN_TOOLCHAIN"
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
# 10. coding CLIs (login is interactive and left to you)
# ─────────────────────────────────────────────────────────────────────────────
install_clis() {
  info "coding CLIs: $INSTALL_CLIS"
  export PATH="$HOME/.local/bin:$TB_HOME/npm-global/bin:$TB_HOME/node/bin:$PATH"
  export NPM_CONFIG_PREFIX="$TB_HOME/npm-global"
  local cli
  for cli in $INSTALL_CLIS; do
    case "$cli" in
      kiro)        have kiro-cli || curl -fsSL https://cli.kiro.dev/install | bash ;;
      devin)       have devin    || curl -fsSL https://cli.devin.ai/install.sh | bash ;;
      cursor)      have agent    || curl -fsSL https://cursor.com/install | bash ;;
      antigravity) have agy      || curl -fsSL https://antigravity.google/cli/install.sh | bash ;;
      claude-code) npm install -g @anthropic-ai/claude-code ;;
      copilot)     npm install -g @github/copilot ;;
      codex)       npm install -g @openai/codex ;;
      *) warn "unknown CLI '$cli' (skipped)" ;;
    esac
  done
}

# ─────────────────────────────────────────────────────────────────────────────
# 11. PATH and env for every login shell
# ─────────────────────────────────────────────────────────────────────────────
write_profile() {
  info "writing $PROFILE_FILE"
  sudo tee "$PROFILE_FILE" >/dev/null <<EOF
# Added by agent-cost-bench scripts/setup-terminal-bench-host.sh
export TB_HOME="$TB_HOME"
export RUSTUP_HOME="$TB_HOME/rustup" CARGO_HOME="$TB_HOME/cargo"
export ELAN_HOME="$TB_HOME/elan" OPAMROOT="$TB_HOME/opam" BUN_INSTALL="$TB_HOME/bun"
export PLAYWRIGHT_BROWSERS_PATH="$TB_HOME/ms-playwright"
export NPM_CONFIG_PREFIX="$TB_HOME/npm-global"
export JAVA_HOME="/usr/lib/jvm/java-17-openjdk-amd64"
# Shared Python env first so 'python3' has the task libraries and the harness.
export PATH="$TB_HOME/py/bin:$TB_HOME/bin:$TB_HOME/node/bin:$TB_HOME/npm-global/bin:$TB_HOME/bun/bin:$TB_HOME/sbt/bin:$TB_HOME/cargo/bin:$TB_HOME/elan/bin:\$HOME/.local/bin:\$PATH"
EOF
}

# ─────────────────────────────────────────────────────────────────────────────
# 12. summary
# ─────────────────────────────────────────────────────────────────────────────
summary() {
  # shellcheck disable=SC1090
  . "$PROFILE_FILE"
  info "installed tools"
  local t
  for t in docker python3 uv node bun java sbt cargo lean coqc sage freecadcmd \
           verilator yosys nextpnr-ecp5 qemu-system-x86_64 ffmpeg tesseract samtools \
           agent-cost-bench kiro-cli devin claude copilot codex agy agent; do
    if have "$t"; then printf '  %-20s %s\n' "$t" "$(command -v "$t")"; else printf '  %-20s -\n' "$t"; fi
  done
  cat <<EOF

Next steps:
  1. Log out and back in (docker/kvm group membership and PATH).
  2. Log in to each CLI (e.g. 'kiro-cli login', 'devin auth login').
  3. Check: docker run --rm hello-world && python3 -c 'import torch, rdkit, pydrake'

Still not covered by this host setup:
  - docker-compose tasks (11) and GPU tasks (3): need harness support.
  - sglang-qwen-burst, vllm-deepseek-streaming, vpp-loss-divergence: the code
    to fix lives inside the task image; the importer must copy it out.
  - The shared Python env is unpinned; a task may expect a different version.
EOF
}

main() {
  preflight
  install_base
  skip DOCKER       || install_docker
  skip RUNTIMES     || install_runtimes
  skip SYSTEM_TOOLS || install_system_tools
  write_profile
  skip PYTHON       || install_python_env
  if ! skip CONDA || ! skip COQ; then fetch_terminal_bench; fi
  skip CONDA        || install_conda_envs
  skip COQ          || install_coq
  skip LEAN         || install_lean
  skip CLIS         || install_clis
  summary
}

main "$@"
