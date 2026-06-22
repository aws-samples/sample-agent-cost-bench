# kirobench Helm verification image — Helm CLI + Python (pytest/PyYAML).
#
# Helm template rendering is fully local (no network, no Tiller, no cluster),
# so this image runs with --network=none at verify time. The Helm binary is the
# only runtime dependency beyond Python.
FROM public.ecr.aws/docker/library/python:3.12-slim

ARG HELM_VERSION=3.16.3

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Install Helm for the image's architecture.
RUN arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) helmarch=amd64 ;; \
      arm64) helmarch=arm64 ;; \
      *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://get.helm.sh/helm-v${HELM_VERSION}-linux-${helmarch}.tar.gz" \
      | tar xz -C /usr/local/bin --strip-components=1 "linux-${helmarch}/helm" \
 && helm version --short

# Python deps for the verification suite.
RUN pip install --no-cache-dir pytest pytest-json-report "pyyaml>=6.0"

WORKDIR /work
