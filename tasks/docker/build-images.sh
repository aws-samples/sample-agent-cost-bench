#!/usr/bin/env bash
# Build the agent-cost-bench verification images (one per language) with warmed
# dependency caches so per-task verification can run offline (--network=none).
#
#   ./tasks/docker/build-images.sh            # build all
#   ./tasks/docker/build-images.sh dotnet     # build one
#
# Requires Docker. The host needs NO language SDKs — they live in the images.
# Base images are pulled from their authoritative registries:
#   dotnet  — mcr.microsoft.com (Microsoft's own registry, no rate limits)
#   java    — public.ecr.aws/amazoncorretto/amazoncorretto (Amazon Corretto on ECR Public)
#   node    — public.ecr.aws/docker/library/node (ECR Public mirror of Docker Hub official)
#   terraform — public.ecr.aws/docker/library/python (Terraform CLI installed on top;
#               AWS provider pre-warmed into an offline mirror so verify runs --network=none)
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"

build() {
  local lang="$1" tag="$2"
  echo "==> Building $tag from $DIR/$lang"
  docker build -t "$tag" "$DIR/$lang"
}

targets=("${@:-all}")
want() { [[ "${targets[0]}" == "all" ]] || printf '%s\n' "${targets[@]}" | grep -qx "$1"; }

want dotnet && build dotnet "agent-cost-bench-dotnet:8.0"
want java   && build java   "agent-cost-bench-java:17"
want node   && build node   "agent-cost-bench-node:20"
want terraform && build terraform "agent-cost-bench-terraform:1.9"
want helm   && build helm   "agent-cost-bench-helm:3.16"

echo "Done. Images:"
docker images | grep -E 'agent-cost-bench-(dotnet|java|node|terraform|helm)' || true
