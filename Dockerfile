# ============================================================================
# agent-cost-bench — Standalone sandbox image
#
# A ready-to-run container with the framework, Python, Node.js, and Docker CLI
# pre-installed. Coding CLIs are installed on first run via the entrypoint
# script (avoids redistribution/license concerns and ensures latest versions).
#
# Build:
#   docker build -t agent-cost-bench .
#
# Run (interactive):
#   docker run -it --privileged \
#     -e KIRO_API_KEY=... \
#     -e ANTHROPIC_API_KEY=... \
#     -e GITHUB_TOKEN=... \
#     agent-cost-bench
#
# Run (quickstart, non-interactive):
#   docker run -it --privileged \
#     -e KIRO_API_KEY=... \
#     -e ANTHROPIC_API_KEY=... \
#     agent-cost-bench ./quickstart.sh
#
# Published at:
#   public.ecr.aws/s8y1a6d4/agent-cost-bench:latest
# ============================================================================

FROM public.ecr.aws/amazonlinux/amazonlinux:2023

# ---------------------------------------------------------------------------
# System packages (minimal base — no docs, no weak deps)
# ---------------------------------------------------------------------------

# Remove curl-minimal first to avoid conflict, then install full package set
RUN dnf install -y \
    python3.12 \
    python3.12-pip \
    nodejs \
    nodejs-npm \
    git \
    wget \
    unzip \
    jq \
    tar \
    gzip \
    which \
    procps-ng \
    docker \
    && dnf clean all \
    && rm -rf /var/cache/dnf \
    && ln -sf /usr/bin/python3.12 /usr/bin/python3 \
    && ln -sf /usr/bin/pip3.12 /usr/bin/pip3

# ---------------------------------------------------------------------------
# Framework installation
# ---------------------------------------------------------------------------

WORKDIR /opt/agent-cost-bench

# Copy only what's needed for pip install first (layer caching)
COPY pyproject.toml ./
COPY agent_cost_bench/ ./agent_cost_bench/

# Install the framework
RUN pip3 install --no-cache-dir -e .

# Copy remaining files
COPY setup.sh quickstart.sh entrypoint.sh ./
COPY tasks/ ./tasks/
COPY config.cli-compare.example.yaml config.model-compare.example.yaml ./

# Make scripts executable
RUN chmod +x setup.sh quickstart.sh entrypoint.sh

# ---------------------------------------------------------------------------
# Workspace directory
# ---------------------------------------------------------------------------

RUN mkdir -p /workspace /results
ENV WORKSPACE_BASE=/workspace

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

ENTRYPOINT ["/opt/agent-cost-bench/entrypoint.sh"]
CMD ["bash"]
