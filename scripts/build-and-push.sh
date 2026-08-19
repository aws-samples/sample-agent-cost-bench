#!/usr/bin/env bash
# ============================================================================
# Build the sandbox image and push to ECR Public
#
# Prerequisites:
#   - AWS CLI configured with credentials that have ecr-public:* permissions
#   - Docker running locally
#   - ECR Public repository already created:
#     aws ecr-public create-repository --repository-name agent-cost-bench --region us-east-1
#
# Usage:
#   ./scripts/build-and-push.sh           # build + push :latest
#   ./scripts/build-and-push.sh v1.0.0    # build + push :v1.0.0 + :latest
# ============================================================================

set -euo pipefail

REGISTRY="public.ecr.aws/s8y1a6d4"
REPO="agent-cost-bench"
TAG="${1:-latest}"
FULL_IMAGE="$REGISTRY/$REPO"

echo "▸ Logging in to ECR Public..."
aws ecr-public get-login-password --region us-east-1 | \
    docker login --username AWS --password-stdin public.ecr.aws

echo "▸ Building image..."
docker build -t "$FULL_IMAGE:$TAG" -f Dockerfile .

if [[ "$TAG" != "latest" ]]; then
    docker tag "$FULL_IMAGE:$TAG" "$FULL_IMAGE:latest"
fi

echo "▸ Pushing $FULL_IMAGE:$TAG..."
docker push "$FULL_IMAGE:$TAG"

if [[ "$TAG" != "latest" ]]; then
    echo "▸ Pushing $FULL_IMAGE:latest..."
    docker push "$FULL_IMAGE:latest"
fi

echo ""
echo "✓ Published: $FULL_IMAGE:$TAG"
echo ""
echo "  Users can now run:"
echo "    docker pull $FULL_IMAGE:$TAG"
echo "    docker run -it --privileged \\"
echo "      -e KIRO_API_KEY=... \\"
echo "      -e ANTHROPIC_API_KEY=... \\"
echo "      -e GITHUB_TOKEN=... \\"
echo "      $FULL_IMAGE:$TAG"
echo ""
