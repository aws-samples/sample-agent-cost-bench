# kirobench

A benchmark framework for coding agents with two modes that share one
execution, evaluation, and reporting core:

- **`cli-compare`** — run the *same* model through different coding CLIs (Kiro,
  Claude Code, GitHub Copilot) on the same tasks and compare **cost**.
- **`model-compare`** — run *different* models inside the Kiro CLI and compare
  **quality** (functional correctness, spec quality, task completion, steering)
  alongside **cost**.

Cost is always reported two ways: USD and native units (credits / premium
requests).

## Prerequisites

- **Python 3.10+**
- **The coding CLI(s) you want to benchmark**, installed and logged in:
  - `cli-compare`: the CLIs you list as runners (e.g. `kiro`, `claude`, `copilot`)
  - `model-compare`: the Kiro CLI
- **Docker** — only if you run the multi-language tasks (C#/.NET, Java,
  TypeScript, Terraform, Helm). The host needs Docker only; the toolchains live
  in prebuilt images. Build them once with `./tasks/docker/build-images.sh`.

## Install

```bash
cd kiro-benchmark-framework
pip install -e .            # installs the `kirobench` command
pip install -e ".[dev]"     # optional: dev/test extras
```

## Run a benchmark

Each mode has a matching example config you can copy and edit.

### model-compare (quality + cost across models)

```bash
kirobench model-compare validate config.model-compare.example.yaml   # check setup
kirobench model-compare run      config.model-compare.example.yaml
```

Edit `config.model-compare.example.yaml` to set the models you want to compare
and (optionally) an LLM judge:

```yaml
kiro_cli_path: kiro-cli
models:
  - claude-opus-4.8
  - claude-sonnet-4.6
  - { id: claude-haiku-4.5, display_name: Haiku 4.5 }
pricing: { usd_per_credit: 0.04 }
judge_model: claude-opus-4.8   # required to grade rubric + spec-quality tasks
modes: ["vibe"]                # or ["vibe", "spec-driven"]
```

### cli-compare (cost across CLIs)

```bash
kirobench cli-compare validate config.cli-compare.example.yaml
kirobench cli-compare run      config.cli-compare.example.yaml
```

Edit `config.cli-compare.example.yaml` to list each CLI as a runner. Each runner
has its own `cli_path`, `model_id`, and `cost_source`. `cli-compare` runs vibe
tasks only (spec tasks are skipped).

### Useful commands

```bash
kirobench model-compare list-tasks config.model-compare.example.yaml   # see tasks a config will run
kirobench report results/<run_id>.json                                 # rebuild an HTML report
kirobench new-task my-task                                             # scaffold a new task (rubric)
kirobench new-task my-task --with-tests                                # scaffold with a test scorer
```

Reports are written to `results/` (HTML + JSON) and open automatically when
`open_report: true`.

## Included tasks

Tasks live under `tasks/` (one folder per task, each with a `task.yaml`). There
are two kinds:

- **vibe** — a single prompt; the model produces code that is then verified.
  Run by both modes.
- **spec-driven** — runs through the CLI's native spec mode (requirements →
  design → tasks → implementation). Run by `model-compare` only.

| Task | Type | Language / Domain | What it tests |
|------|------|-------------------|---------------|
| `rest-api` | vibe | Python / FastAPI | Greenfield: CRUD Todo REST API |
| `dashboard` | vibe | Python + HTML/JS | Greenfield: full-stack Todo dashboard |
| `log-analyzer-cli` | vibe | Python | Greenfield: parse access logs into a JSON summary |
| `note-cli` | vibe | Python | Greenfield: note-taking CLI (rubric graded) |
| `dockerize-flask` | vibe | Docker | Brownfield: add Dockerfile + docker-compose to a Flask app |
| `terraform-s3` | vibe | Terraform / AWS | Provision a secure S3 bucket |
| `terraform-serverless-spa` | vibe | Terraform / AWS | Serverless SPA stack; `terraform validate` + structural checks |
| `helm-chart` | vibe | Helm / Kubernetes | Production-ready Helm chart (Deployment, Service, Ingress, HPA…) |
| `harden-k8s` | vibe | Kubernetes | Brownfield: security-harden insecure manifests |
| `dotnet-invoicing` | vibe | C#/.NET (Docker) | Brownfield: fix four invoice-pricing bugs |
| `java-ratelimiter` | vibe | Java/Maven (Docker) | Brownfield: fix four rate-limiter bugs |
| `typescript-circuit-breaker` | vibe | TypeScript (Docker) | Brownfield: fix four circuit-breaker bugs |
| `bedrock-sentiment` | vibe | AWS / Python | Migrate sentiment analysis from Comprehend to Bedrock (rubric graded) |
| `geotrack-duplicate-device` | vibe | Vue.js / AWS | Prevent assigning one IoT device to two routes (rubric graded) |
| `auth-feature` | spec-driven | Python | JWT auth: login, logout, refresh endpoints |

Pick which tasks run with `task_ids` in your config (omit it to run all
discovered tasks), and which kinds run with `modes`.

> Rubric-graded tasks (`note-cli`, `bedrock-sentiment`, `geotrack-duplicate-device`)
> need `judge_model` set in your config. Docker-verified tasks need Docker and
> the prebuilt images.

## How verification works

After a model finishes a task, the framework scores its output. You choose how a
task is scored by adding a block to its `task.yaml`. There are four options. Pick
the one that fits your task.

### 1. Run Python tests (`verify: { runner: pytest }`)

Use this when you can write tests to check the output. Put your test files in the
task's `verify/` folder (the model never sees them, so it can't cheat). List any
pip packages the tests need under `deps`. The framework creates a clean virtual
environment, installs `deps`, and runs the tests.

```yaml
verify:
  runner: pytest
  deps: ["fastapi==0.104.1", "httpx==0.27.2", "pytest==9.0.3"]
```

### 2. Run a custom scorer (`verify: { runner: local }`)

Use this when "pass/fail" isn't enough and you want to inspect files yourself.
Write a `verify/score.py` that looks at the workspace and prints a score line
(see "Partial credit" below). Same isolated venv + `deps` as above.

```yaml
verify:
  runner: local
  deps: ["python-hcl2==4.3.5"]
  score: verify/score.py
```

### 3. Run tests inside Docker (`verify: { image: ... }`)

Use this for non-Python tasks (C#/.NET, Java, TypeScript, etc.). Tests run in a
prebuilt image so the toolchain is consistent and offline. Build the images once
with `./tasks/docker/build-images.sh`. Put the authoritative tests in
`verify/tests/`, then describe how to run them:

```yaml
verify:
  image: kirobench-node:20        # prebuilt toolchain image
  parser: vitest-json             # how to read the test report
  workdir: src
  tests_subdir: verify/tests      # hidden tests, copied in at run time
  test_cmd: 'vitest run --reporter=json --outputFile="$RESULTS_DIR/vitest.json"'
  network: none
```

### 4. Grade with the LLM judge (`quality.rubric`)

Use this when you don't want to write any verification code. List plain-English
criteria and the judge checks each one against the model's output. The score is
the fraction of criteria met. This requires `judge_model` in your config.

```yaml
quality:
  rubric:
    - "notes_cli.py is created in the workspace"
    - "'add <text>' appends the note as a new line to notes.txt"
    - "'search' is case-insensitive"
```

### Partial credit

Tests and scorers can report a score between 0 and 1 (not just pass/fail) by
printing this line. The framework reads it from the output:

```
KIROBENCH_RESULT: {"score": 0.7, "checkpoints": {...}, "summary": "..."}
```

### Setting the pass bar

`functional_pass_threshold` in a `task.yaml` sets the score needed to count as a
pass (e.g. `0.75` means 75% of criteria/tests). It defaults to a near-perfect
score, so set it lower for rubric tasks that rarely need 100%.

## Run the test suite

```bash
pytest    # unit + integration; uses a MockCLI, no network or real CLI needed
```

## Troubleshooting

- **Spec runs hang with no output** — native spec mode needs an interactive
  terminal. The harness runs spec tasks under a PTY by default
  (`spec_use_pty: true`). If your CLI reads the request from stdin, set
  `spec_prompt_via_stdin: true`. Confirm behavior with a manual run first.
- **Docker task fails to set up** — run `kirobench <mode> validate <config>` to
  check the daemon and required images; build missing ones with
  `./tasks/docker/build-images.sh`. Missing Docker/images are reported as a
  setup error, not a model failure.
- **Offline toolchain restore fails** — allow network for verification with
  `KIROBENCH_VERIFY_NETWORK=bridge kirobench <mode> run <config>`.
