# agent-cost-bench Node/TypeScript verification image — Node 20 + baked node_modules.
# Base: Node 20 from the Amazon ECR Public Gallery mirror of Docker Hub official
# images (public.ecr.aws/docker/library/node). No Docker Hub rate limits.
FROM public.ecr.aws/docker/library/node:20

# Install the test toolchain into a stable location baked into the image.
WORKDIR /opt/deps
COPY warm-package.json ./package.json
RUN npm install --no-audit --no-fund

# Make the toolchain binaries (vitest, tsc) available on PATH.
ENV PATH=/opt/deps/node_modules/.bin:$PATH

WORKDIR /work
