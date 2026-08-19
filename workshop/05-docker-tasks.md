# Module 5: Docker Tasks

Time: ~20 minutes

## Objectives

- Build Docker images for multi-language verification
- Run the full task suite including C#, Java, TypeScript, Terraform, and Helm tasks
- Understand how Docker verification works

---

## Step 1: Why Docker tasks exist

Five tasks verify their results inside Docker containers because they need language runtimes that you may not have installed locally:

| Task | Language | Docker Image | What it verifies |
|------|----------|-------------|-----------------|
| dotnet-invoicing | C# / .NET 8 | `agent-cost-bench-dotnet:8.0` | `dotnet test` JUnit XML |
| java-ratelimiter | Java 21 / Maven | `agent-cost-bench-java:21` | `mvn test` JUnit XML |
| typescript-circuit-breaker | TypeScript / Node 20 | `agent-cost-bench-node:20` | `vitest run` JSON |
| terraform-serverless-spa | Terraform 1.9 | `agent-cost-bench-terraform:1.9` | `terraform validate` + pytest |
| helm-chart | Helm 3.16 | `agent-cost-bench-helm:3.16` | `helm lint --strict` + pytest |

The model generates code locally; only the verification runs inside Docker.

## Step 2: Install Docker

If you don't have Docker:

```bash
# macOS
brew install --cask docker
open /Applications/Docker.app   # Start Docker Desktop

# Or use Finch (lighter alternative):
brew install finch
finch vm start
export CONTAINER_RUNTIME=finch   # Tell the harness to use Finch
```

## Step 3: Build the verification images

```bash
./tasks/docker/build-images.sh
```

This builds all 5 images. Takes 3–5 minutes on first run (downloads base images).

To build a single image:

```bash
./tasks/docker/build-images.sh dotnet    # Just .NET
./tasks/docker/build-images.sh java      # Just Java
```

## Step 4: Verify images are ready

```bash
docker images | grep agent-cost-bench
```

You should see:

```
agent-cost-bench-dotnet      8.0     ...
agent-cost-bench-java        21      ...
agent-cost-bench-node        20      ...
agent-cost-bench-terraform   1.9     ...
agent-cost-bench-helm        3.16    ...
```

## Step 5: Validate Docker integration

```bash
agent-cost-bench cli-compare validate config.full-run.yaml
```

The validation checks that each required Docker image exists. If any are missing, it tells you which ones to build.

## Step 6: Run Docker tasks

Now you can run the full 14-task suite from Module 3 without excluding any tasks:

```bash
agent-cost-bench cli-compare run config.full-run.yaml
```

## How Docker verification works

1. The model writes code into the workspace (e.g., `src/`)
2. The harness mounts the workspace **read-only** into the container as `/src-ro`
3. The container copies source to a writable location, installs dependencies, runs tests
4. Test results (JUnit XML, vitest JSON) are written to a mounted results directory
5. The harness reads the results and computes a graduated score (0.0–1.0)

The model never interacts with Docker. It just writes code files and the harness handles verification separately.

## Using Finch instead of Docker

If you prefer Finch:

```bash
export CONTAINER_RUNTIME=finch

# Build images with Finch
./tasks/docker/build-images.sh

# Set workspace_base under your home dir (Finch macOS volume mount limitation)
# In your config.yaml:
workspace_base: ~/agent-cost-bench-runs
```

---

## Checkpoint ✓

- [ ] Docker (or Finch) is running
- [ ] All 5 verification images are built
- [ ] Validation passes for Docker tasks
- [ ] You can run the full 14-task suite

→ Continue to [Module 6: Bring Your Own Task](./06-custom-task.md)
