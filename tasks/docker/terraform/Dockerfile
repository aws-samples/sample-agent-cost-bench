# kirobench Terraform verification image — Terraform + Python (pytest) with the
# AWS provider pre-warmed into a local filesystem mirror so `terraform validate`
# runs FULLY OFFLINE (the verify step uses --network=none).
#
# Base: Python slim from the Amazon ECR Public mirror of Docker Hub official
# (avoids Docker Hub rate limits, consistent with the node image). Python is
# here because the verification suite is pytest-based.
FROM public.ecr.aws/docker/library/python:3.12-slim

# Terraform version is easy to bump; the AWS provider major version is pinned in
# warm.tf and tasks should use a compatible constraint (~> 5.0).
ARG TERRAFORM_VERSION=1.9.8

ENV TF_CLI_CONFIG_FILE=/opt/terraformrc \
    TF_IN_AUTOMATION=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl unzip ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Install Terraform for the image's architecture (amd64 or arm64).
RUN arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) tfarch=amd64 ;; \
      arm64) tfarch=arm64 ;; \
      *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${tfarch}.zip" -o /tmp/tf.zip \
 && unzip -o /tmp/tf.zip -d /usr/local/bin \
 && rm /tmp/tf.zip \
 && terraform version

# Python deps for the verification suite.
RUN pip install --no-cache-dir pytest pytest-json-report "python-hcl2==4.3.5"

# Pre-warm: download the AWS (plus common helper) providers into a filesystem
# mirror for the image's native platform. This is the only step that needs
# network, and it happens at BUILD time — verification at run time is offline.
WORKDIR /warm
COPY warm.tf ./warm.tf
RUN terraform providers mirror /opt/tf-mirror

# CLI config: resolve these providers ONLY from the offline mirror.
COPY terraformrc /opt/terraformrc

WORKDIR /work
