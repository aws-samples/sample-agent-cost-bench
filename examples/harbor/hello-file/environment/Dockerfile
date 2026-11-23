FROM node:22-bookworm-slim
# Use the bundled Node.js and npm instead of installing them during agent setup.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends bash ca-certificates curl git procps unzip \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
