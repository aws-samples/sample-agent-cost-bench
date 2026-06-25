# kirobench

**How much does the same model cost across different coding CLIs? Which model delivers the best quality for your actual codebase?** kirobench answers both questions in a single run.

Bring any model, any CLI, and any use case — a real GitHub repo with your own verification tests — and kirobench will measure cost, quality, and latency side by side.

## What you can do

| Question | Mode | Example |
|----------|------|---------|
| How does Sonnet 4.6 compare in Kiro vs Claude Code vs Copilot vs Codex? | `cli-compare` | Compare USD cost, latency, and pass rate for the same tasks |
| How do Opus, Sonnet, and other models stack up inside the Kiro CLI? | `model-compare` | Compare quality scores + cost across models |
| Does GPT-5.5 through Codex beat Sonnet 4.6 through Kiro on *my* brownfield repo? | `cli-compare` | Clone your repo into the task workspace; verify with your own tests |

The framework is designed to be flexible:

- **Any CLI** — Kiro, Claude Code, GitHub Copilot, OpenAI Codex - Currently supported CLI's.
- **Any model** — Anthropic (Claude), OpenAI (o-series, GPT-5.x) or anything your CLI exposes.
- **Any use case** — greenfield tasks included out of the box, or bring your own GitHub repo (public or private). The framework clones it, hands it to the model, and verifies the result.
- **Multiple verification options** — pytest, Docker containers, custom scorers, or LLM-judge rubrics. Pick the one that fits; no verification code is required for rubric-graded tasks.

Cost is always reported two ways: USD and native units (credits / AI Credits / tokens).

## Prerequisites

- **Python 3.10+**
- **The coding CLI(s) you want to benchmark**, installed and logged in:
  - `cli-compare`: the CLIs you list as runners (e.g. `kiro-cli`, `claude`, `copilot`, `codex`)
  - `model-compare`: the Kiro CLI
- **Docker** — only if you run the multi-language tasks (C#/.NET, Java,
  TypeScript, Terraform, Helm). Build images once with `./tasks/docker/build-images.sh`.

## Install

```bash
cd kiro-benchmark-framework
pip install -e .            # installs the `kirobench` command
pip install -e ".[dev]"     # optional: dev/test extras
```

## Quick start

### cli-compare — same tasks, different CLIs

*"How much does Sonnet 4.6 cost through Kiro vs Claude Code vs Copilot? How does GPT-5.5 via Codex compare?"*

```bash
kirobench cli-compare run config.cli-compare.example.yaml
```

The example config defines four runners: Kiro, Claude Code, Copilot (all on Sonnet 4.6), and Codex (on GPT-5.5). Cost is auto-detected from the binary name — you just provide the CLI path, model id, and pricing rates:

```yaml
runners:
  - name: kiro
    cli_path: kiro-cli
    model_id: claude-sonnet-4.6
    pricing: { usd_per_credit: 0.04 }
    cli_base_args: [chat, --no-interactive, --trust-all-tools,
                    "--model={model}", "--effort={effort}"]

  - name: codex
    cli_path: codex
    model_id: gpt-5.5
    pricing:
      usd_per_input_token:        0.000005    # $5.00/1M
      usd_per_cached_input_token: 0.0000005   # $0.50/1M
      usd_per_output_token:       0.000030    # $30.00/1M
    cli_base_args: [exec, --json, --ephemeral, --skip-git-repo-check,
                    --dangerously-bypass-approvals-and-sandbox, -m, "{model}", "{prompt}"]
```

### model-compare — same CLI, different models

*"Which model gives the best quality inside the Kiro CLI?"*

```bash
kirobench model-compare run config.model-compare.example.yaml
```

```yaml
models:
  - claude-opus-4.8
  - claude-sonnet-4.6
pricing: { usd_per_credit: 0.04 }
judge_model: claude-opus-4.8    # grades rubric + spec quality tasks
modes: ["vibe"]                 # or ["vibe", "spec-driven"]
```

### Bring your own repo

Any task can reference a GitHub repository. The framework clones it (cached across models), places it in the workspace, and the model works against your real code:

```yaml
# task.yaml
id: fix-my-auth-bug
mode: vibe
prompt: "Fix the failing test in tests/test_auth.py"
effort: medium            # low / medium / high — per-task, based on complexity
repo:
  url: https://github.com/my-org/my-service.git
  ref: a1b2c3d4e5f6...   # pin to a commit SHA for reproducibility
  token_env: GITHUB_TOKEN # for private repos
verify:
  runner: pytest
  deps: [pytest, httpx]
```

### Effort level

Set `effort` in each task's `task.yaml` to control how much reasoning the model applies:

```yaml
# Simple formatting task — low reasoning is fine
effort: low

# Complex multi-file refactor — give the model time to think
effort: high
```

Valid values: `low`, `medium`, `high` (default: `high`). A run-level fallback (`effort:` in the main config) still works for backward compatibility — per-task settings override it.

### Useful commands

```bash
kirobench cli-compare validate config.cli-compare.example.yaml        # check setup
kirobench model-compare list-tasks config.model-compare.example.yaml  # see tasks
kirobench report results/<run_id>.json                                # rebuild HTML
kirobench new-task my-task                                            # scaffold (rubric)
kirobench new-task my-task --with-tests                               # scaffold (pytest)
```

Reports (HTML + JSON) are written to `results/` and open automatically.

## Included tasks

Tasks live under `tasks/`. Two types:

- **vibe** — a single prompt; the model produces code that is verified. Run by both modes.
- **spec-driven** — full spec workflow (requirements → design → tasks → implementation). Model-compare only.

| Task | Type | Domain | What it tests |
|------|------|--------|---------------|
| `rest-api` | vibe | Python / FastAPI | Greenfield: CRUD Todo REST API |
| `dashboard` | vibe | Python + HTML/JS | Greenfield: full-stack Todo dashboard |
| `log-analyzer-cli` | vibe | Python | Greenfield: parse access logs into JSON |
| `note-cli` | vibe | Python | Greenfield: note-taking CLI (rubric graded) |
| `dockerize-flask` | vibe | Docker | Brownfield: add Dockerfile + compose |
| `terraform-s3` | vibe | Terraform / AWS | Provision a secure S3 bucket |
| `terraform-serverless-spa` | vibe | Terraform / AWS | Serverless SPA stack |
| `helm-chart` | vibe | Helm / K8s | Production-ready Helm chart |
| `harden-k8s` | vibe | Kubernetes | Brownfield: security-harden manifests |
| `dotnet-invoicing` | vibe | C#/.NET (Docker) | Brownfield: fix invoice-pricing bugs |
| `java-ratelimiter` | vibe | Java (Docker) | Brownfield: fix rate-limiter bugs |
| `typescript-circuit-breaker` | vibe | TypeScript (Docker) | Brownfield: fix circuit-breaker bugs |
| `bedrock-sentiment` | vibe | AWS / Python | Migrate Comprehend → Bedrock (rubric graded) |
| `geotrack-duplicate-device` | vibe | Vue.js / AWS | Prevent duplicate IoT device assignment (rubric) |
| `auth-feature` | spec-driven | Python | JWT auth: login, logout, refresh |

Select tasks with `task_ids:` in your config. Omit it to run everything.

> Rubric-graded tasks need `judge_model`. Docker tasks need Docker + prebuilt images.

## How verification works

After a model finishes a task, the framework scores its output. Four options — pick what fits your task:

### 1. Python tests (`verify: { runner: pytest }`)

Put test files in the task's `verify/` folder (the model never sees them). List pip dependencies under `deps`. The framework handles the venv.

```yaml
verify:
  runner: pytest
  deps: ["fastapi==0.104.1", "httpx==0.27.2", "pytest==9.0.3"]
```

### 2. Custom scorer (`verify: { runner: local }`)

Write `verify/score.py` to inspect the workspace and print a graduated score.

```yaml
verify:
  runner: local
  deps: ["python-hcl2==4.3.5"]
  score: verify/score.py
```

### 3. Docker (`verify: { image: ... }`)

For non-Python tasks. Tests run in a prebuilt image — no local toolchain needed.

```yaml
verify:
  image: kirobench-node:20
  parser: vitest-json
  workdir: src
  tests_subdir: verify/tests
  test_cmd: 'vitest run --reporter=json --outputFile="$RESULTS_DIR/vitest.json"'
```

### 4. LLM judge rubric (`quality.rubric`)

No verification code needed. List plain-English criteria and the judge grades each one.

```yaml
quality:
  rubric:
    - "notes_cli.py is created in the workspace"
    - "'add <text>' appends the note as a new line to notes.txt"
    - "'search' is case-insensitive"
```

### Partial credit

Any verifier can report a graduated score (0.0–1.0):

```
KIROBENCH_RESULT: {"score": 0.7, "checkpoints": {...}, "summary": "..."}
```

### Pass threshold

`functional_pass_threshold` in `task.yaml` sets the score needed for a PASS (default: 0.99). Lower it for rubric tasks that rarely need perfection.

## Supported CLIs and cost detection

| Binary name | Detected cost source | What it reads |
|-------------|---------------------|---------------|
| `kiro` / `kiro-cli` | Kiro credits | `Credits: X • Time: Ys` telemetry line |
| `claude` | Claude JSON | `--output-format json` → `total_cost_usd` |
| `copilot` | Copilot session AIU | `~/.copilot/session-state/` `totalNanoAiu` |
| `codex` | Codex JSONL | `codex exec --json` → `turn.completed` token counts |
| Any + per-token pricing | Token regex | Custom regex with `(?P<input>...)` / `(?P<output>...)` groups |


## Run the test suite

```bash
pytest    # unit + integration; uses a MockCLI, no network or real CLI needed
```

## Troubleshooting

- **Spec runs hang** — native spec mode needs a TTY. The harness uses PTY by default (`spec_use_pty: true`). If your CLI reads from stdin, set `spec_prompt_via_stdin: true`.
- **Docker task fails** — run `kirobench <mode> validate <config>` to check images; build missing ones with `./tasks/docker/build-images.sh`.
- **Offline restore fails** — allow network for verification: `KIROBENCH_VERIFY_NETWORK=bridge kirobench <mode> run <config>`.
