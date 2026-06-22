# kirobench

A unified benchmark framework that merges two harnesses into one tool with two
modes that share a single execution, evaluation, and reporting core:

- **`cli-compare`** — run the *same* model through multiple coding CLIs (Kiro,
  Claude Code, GitHub Copilot) on the same vibe tasks and compare **cost** in
  USD (and native units) per task and per successful task.
- **`model-compare`** — run multiple *models* inside the Kiro CLI across vibe
  **and** spec-driven tasks and compare **quality** (functional + spec quality +
  task completion + steering, with an optional LLM judge and pass@k) alongside
  **cost** (credits + USD).

Both modes desugar their config into one internal `Target` abstraction, so the
runner, evaluators, and reporters are shared. **Cost is always reported both
ways: USD and native units (credits / premium requests).**

## Install

```bash
cd kiro-benchmark-framework
pip install -e .
# optional dev/test extras
pip install -e ".[dev]"
```

This installs the `kirobench` command.

## Quick start

### cli-compare (cost across CLIs)

```bash
kirobench cli-compare validate   config.cli-compare.example.yaml
kirobench cli-compare list-tasks config.cli-compare.example.yaml
kirobench cli-compare run        config.cli-compare.example.yaml
```

The report ranks CLIs by **cost per success** (cheapest first), with a per-CLI
Model column, USD + native credit columns, latency, and per-task charts.

### model-compare (quality + cost across models)

```bash
kirobench model-compare validate   config.model-compare.example.yaml
kirobench model-compare run        config.model-compare.example.yaml
# enable the LLM-as-judge:
kirobench model-compare run config.model-compare.example.yaml --judge-model claude-sonnet-4
```

The report shows pass rate, the four quality dimensions, cost (credits + USD),
pass@k/consistency (when `repeats > 1`), and a by-mode breakdown.

### Shared commands

```bash
# Rebuild an HTML report from a results or .partial.json checkpoint (mode auto-detected)
kirobench report results/<run_id>.json

# Scaffold a new task fixture (mode-aware)
kirobench new-task my-task --mode spec-driven
```

## Config schemas

Two tailored YAML schemas desugar into one `Target` list. See
`config.cli-compare.example.yaml` and `config.model-compare.example.yaml`.

**cli-compare** uses a `runners:` list. Each runner has its own `cost_source`
and `model_id` (CLIs use different id strings), and `comparison_label` is a
reporting headline only (default `"cross-CLI comparison"`) — runners may even
use different models; the label never sets or overrides a model. The legacy
`shared_model` key is still accepted:

```yaml
comparison_label: claude-sonnet-4.x across CLIs   # optional; reporting label only
runners:
  - name: kiro
    cli_path: kiro
    model_id: claude-sonnet-4
    cost_source: kiro_credits
    pricing: { usd_per_credit: 0.04 }
    cli_base_args: [chat, --no-interactive, --trust-all-tools, "--model={model}"]
  - name: claude-code
    cli_path: claude
    model_id: claude-sonnet-4-5
    cost_source: claude_json
    cli_base_args: ["-p", "{prompt}", "--output-format", "json", "--model", "{model}"]
```

**model-compare** uses a simple `models:` list (bare ids or dicts). Each desugars
to a Kiro target with `cost_source=kiro_credits` and `supports_spec=true`:

```yaml
kiro_cli_path: kiro
models:
  - claude-sonnet-4
  - { id: claude-haiku-4-5, display_name: Haiku }
pricing: { usd_per_credit: 0.04 }
# judge_model: claude-sonnet-4
# judge_weight: 0.6
```

## Tasks

Task fixtures live under `tasks/` (one dir per task with a `task.yaml`). A task
is self-contained: the prompt lives **inline** in `task.yaml` under a `prompt:`
field, so a task is just `task.yaml` plus its `verify/` folder.

```yaml
id: task-002-rest-api
description: "Build a Todo REST API"
timeout_minutes: 10
effort: medium
prompt: |
  Create a REST API using FastAPI with CRUD endpoints for a Todo list.
  Save it as main.py and include a requirements.txt.
```

- **vibe**: an inline `prompt:` + a verification. Verification is one of:
  a centralized **`verify:` block** (`runner: local` runs a `verify/score.py`
  scorer in a framework-managed venv; `runner: docker` runs tests in a
  container), a `verify/test_*.py` pytest suite, or a no-code `quality.rubric`.
  Run by both modes.
- **spec-driven**: an inline `prompt:` (or a `seed/` such as `requirements.md`) +
  verification. Run by `model-compare` only — `cli-compare` skips spec tasks
  (it is vibe-only). Spec tasks run via the CLI's **native spec mode**: a single
  invocation with `spec_mode_args` (default `["--v3", "--mode", "spec"]`, since
  `--mode spec` requires `--v3`) injected, after which the CLI writes `requirements.md`/`design.md`/`tasks.md` under
  `.kiro/specs/`. Those artifacts are scored by `spec_artifact_quality` (LLM
  judge when `judge_model` is set), and the implementation is checked by the
  verification (`functional_tests`).

### No-code quality grading (rubric)

A task can be graded with **zero verification code** by listing plain-English
acceptance criteria. When no code-based verification exists, the LLM judge
checks each criterion against the produced files (met/unmet) and the functional
score is the fraction met. This needs `judge_model` set in your model-compare
config. Scaffold one with `kirobench new-task my-task` (use `--with-tests` for a
code-verification skeleton instead).

```yaml
id: my-task
mode: vibe
prompt: |
  Build a Todo REST API ...
quality:
  rubric:
    - "Exposes GET/POST/PUT/DELETE for /todos"
    - "Returns 404 for a missing todo id"
    - "Includes a requirements.txt with fastapi and uvicorn"
  # reference_solution: ref/   # optional golden solution to anchor grading
```

**What the judge sees.** For a **repo task**, the judge grades the model's
*changeset* — a `git diff` of the workspace against the pristine cached clone
(the baseline lives in the read-only cache, outside the model-controlled
workspace, so it can't be tampered with). This shows the full change regardless
of file size, so large source files are never truncated out of view, and the
judge reasons about what actually changed rather than the whole tree. For
greenfield tasks (no `repo:`), or when git/baseline is unavailable, it falls back
to collecting the produced files (each truncated to a per-file character cap).

A verification can emit a graduated score marker for partial credit:

```
KIROBENCH_RESULT: {"score": 0.7, "checkpoints": {...}, "summary": "..."}
```

The legacy `KIRO_BENCH_RESULT` / `CLI_BENCH_RESULT` markers are also accepted.

### GitHub repo tasks

Point a task at any public git repository and the framework clones it into the
workspace before the model runs — no need to commit source files inside the
benchmark repo.

```yaml
id: task-020-fix-auth-bug
mode: vibe
description: "Fix the JWT refresh-token expiry bug in the auth service"
timeout_minutes: 20
effort: high
prompt: |
  The JWT refresh-token expiry logic in auth/tokens.py has a bug: tokens
  are not invalidated after logout. Fix it so that logging out immediately
  expires the refresh token.

# GitHub repo cloned into the workspace before the model runs.
repo:
  url: https://github.com/your-org/your-project
  # ref: which version of the repo to clone.
  #   Use a full 40-char commit SHA (recommended) — permanently pinned, every
  #   benchmark run gets identical code regardless of when it runs.
  #   A branch name (e.g. main) is NOT recommended: the branch advances over
  #   time so two runs may get different code and produce incomparable scores.
  #   Find the SHA:  git ls-remote <url> HEAD  OR  git log --oneline -1
  ref: a3f8c2d1e9b04756acde1234567890abcdef0123   # pinned SHA
  subdir: auth       # optional: sparse-checkout, copies auth/ → workspace root
  depth: 1           # shallow clone (fastest; use 0 for full history)
  # Private repos (HTTPS): point token_env at the NAME of an environment
  # variable holding a personal access token. NEVER paste the token here.
  # token_env: GITHUB_TOKEN
  # token_user: x-access-token   # GitHub default; 'oauth2' (GitLab), 'x-token-auth' (Bitbucket)

# Verify with hidden pytest tests (authoritative tests not in the repo)
verify:
  runner: pytest
  deps: ["pyjwt==2.8.0", "cryptography==42.0.0"]
```

Key points:

- **Pinned SHA** — strongly recommended. Branch names advance and make runs
  non-reproducible. Pin to a SHA so every model sees identical source.
- **Caching** — the clone is stored in `workspace_base/.repo_cache/` and reused
  across all models in the same benchmark run. Only one network fetch regardless
  of how many models you compare.
- **Workspace layout** — the repo is copied **directly into the workspace root**,
  so the model sees the same directory structure as the GitHub repo (no extra
  `src/` wrapper). Prompts can reference paths exactly as they appear in the repo.
- **`subdir`** — for monorepos, only the specified subdirectory is checked out
  and copied to the workspace root. The model sees just that slice.
- **Private repos** — set `token_env` to the *name* of an environment variable
  holding a personal access token (e.g. export `GITHUB_TOKEN`, then
  `token_env: GITHUB_TOKEN`). The token is read at runtime and passed to git via
  an ephemeral auth header, so it is never written to `task.yaml`, never
  persisted in the clone's `.git/config`, and never visible in the process list.
  Token auth is HTTPS-only; SSH URLs authenticate with your keys.
- **Hidden tests** — authoritative verification tests live in the task's
  `verify/` folder (never in the cloned repo), so the model cannot see or modify
  them (same anti-cheat approach as the Docker tasks).
- **Scaffold** — `kirobench new-task my-task --repo https://github.com/org/repo`
  generates a `task.yaml` with the `repo:` block pre-filled.

### Centralized verification (no per-task `verify.sh`)
Verification mechanics live in the framework, not in each task. A task ships a
small `verify:` block and (for code-based scoring) a `verify/score.py` scorer —
never shell boilerplate.

```yaml
verify:
  runner: local                  # default when no `image` is set
  deps: ["python-hcl2==4.3.5"]   # pip packages the scorer needs (optional)
  score: verify/score.py         # prints a KIROBENCH_RESULT marker
```

For `runner: local`, the framework creates an isolated venv in the workspace,
installs the model's `requirements.txt` plus `deps`, then runs
`python verify/score.py <workspace> <task_dir>` with `WORKSPACE`/`TASK_DIR` in
the environment and the venv's `bin` on `PATH`. The scorer just inspects the
workspace and prints the marker — the venv, dependency install, and result
parsing are all handled centrally. `runner: docker` (used when `image` is set)
runs tests in a container instead; see "Multi-language tasks" below.

Scaffold either style:

```bash
kirobench new-task my-task                # no-code rubric (judge-graded)
kirobench new-task my-task --with-tests   # verify/score.py + verify: local block
```

## Cost model

| cost_source       | native unit         | USD derivation |
|-------------------|---------------------|----------------|
| `kiro_credits`    | Kiro credits        | credits × `usd_per_credit` |
| `claude_json`     | (USD only)          | CLI's `total_cost_usd` |
| `copilot_json`    | AI credits (AIU)    | `totalNanoAiu` from session-state (1 AIU = $0.01); falls back to `premiumRequests × usd_per_premium_request` |
| `tokens`          | tokens              | `in×rate + out×rate` via `token_regex` |
| `premium_request` | premium requests    | `requests_per_run × usd_per_premium_request` |

## Testing

```bash
pytest            # unit + integration (uses a MockCLI; no network/real CLI)
```

The suite covers desugaring, cost parsing (USD + native), pass@k stats, config
loading, the execution core (incl. transient retry and spec phases), the
evaluators, the runner (both modes), the reporters, and the CLI.

## Troubleshooting spec mode

If `model-compare` spec runs all print `· running … (spec-driven) …` and then
hang with no completion lines (and your terminal shows stray `^[[O`/`^[[I`),
the `kiro` CLI's `--mode spec` is not returning. Diagnose with a single manual
run that mimics the harness (stdin from /dev/null, output piped):

```bash
cd "$(mktemp -d)"
kiro chat --no-interactive --trust-all-tools --v3 --mode spec \
  --model claude-sonnet-4.6 --effort high "Build a pricing engine; save pricing.py" </dev/null | cat
```

- **It returns on its own** → the harness will work; runs are just slow. Lower
  `timeout_minutes` and use fewer models while iterating.
- **It hangs / opens a TUI** → spec mode needs an interactive **terminal**.
  The harness runs spec tasks under a pseudo-terminal (PTY) by default
  (`spec_use_pty: true`) so the CLI behaves as it does in your terminal. If your
  CLI instead reads the request from **stdin**, set `spec_prompt_via_stdin: true`.

To clear leftover focus-mode escape codes in your shell: `printf '\033[?1004l\033[?2004l'` (or `reset`).

When a call times out, the harness now **kills the process and keeps whatever it
printed first** — that partial transcript is written to the run log
(`results/<run_id>.log`) and to the result's transcript in the JSON/HTML report,
so you can see exactly where the CLI stalled (e.g. a login prompt, a confirmation,
or a TUI banner).

## Multi-language tasks (Docker verification)

Some brownfield tasks are in C#/.NET, Java, and TypeScript and are verified
inside Docker so the toolchains are reproducible and isolated. The **host needs
only Docker** — the SDKs live in prebuilt images.

Build the images once (per language or all):

```bash
./tasks/docker/build-images.sh          # all four
./tasks/docker/build-images.sh dotnet   # just one
```

This produces `kirobench-dotnet:8.0`, `kirobench-java:17`, `kirobench-node:20`,
and `kirobench-terraform:1.9`, each with its dependency cache warmed (NuGet /
Maven / node_modules / the AWS Terraform provider) so verification runs
**offline**. The Terraform image (used by `task-015-terraform-serverless-spa`)
pre-warms the AWS provider into a filesystem mirror so `terraform validate` runs
with `--network=none` and **no AWS account or credentials** — it only checks
configuration validity, never `apply`.

How it works:
- The model still runs on the host (it needs your CLI login/session). Only the
  verification runs in an ephemeral `docker run --rm --network=none` container.
- Verification is **declarative**: each task's `task.yaml` carries a `verify:`
  block describing the image, the test command, and how to parse results — no
  per-task shell script. A single generic runner
  (`kirobench/verify/runner.py`) executes it.
- The model's `src/` is mounted read-only and copied into the container, so
  no root-owned build artifacts ever land on the host workspace.
- **Anti-cheat:** the authoritative tests live in each task's `verify/tests/`
  (mounted read-only, never in the model's workspace), so a model can't pass by
  editing tests.
- The runner parses the test report with a named **parser** and emits a
  graduated score (fraction of tests passing).

### Declarative `verify:` spec

```yaml
verify:
  image: kirobench-node:20          # prebuilt image (or any image with the toolchain)
  parser: vitest-json               # trx | junit-xml | pytest-json | vitest-json | tap | regex | exit-code
  workdir: src                      # dir under $BUILD to run test_cmd in
  tests_subdir: verify/tests        # authoritative tests, mounted read-only at $TESTS_RO
  setup:                            # shell steps run in $BUILD before the tests
    - mkdir -p "$BUILD/src/tests"
    - cp "$TESTS_RO"/*.test.ts "$BUILD/src/tests/"
    - ln -s /opt/deps/node_modules "$BUILD/src/node_modules"
  test_cmd: 'vitest run --reporter=json --outputFile="$RESULTS_DIR/vitest.json"'
  network: none
```

In-container env the runner provides: `$SRC_RO` (read-only model src), `$TESTS_RO`
(authoritative tests), `$BUILD` (writable scratch; `src/` is pre-copied to
`$BUILD/src`), and `$RESULTS_DIR` (write the report here for the parser).

Adding a new verified task — in any language — is now just: drop the buggy/starter
code in `src/`, put hidden tests in `verify/tests/`, and add a `verify:` block.
No bespoke script. Greenfield prompt tasks work the same way (point `test_cmd` at
hidden tests). To support a new test-output format, add a parser to
`kirobench/verify/parsers.py`.

If a toolchain's offline restore fails on your setup, allow network for verify:

```bash
KIROBENCH_VERIFY_NETWORK=bridge kirobench cli-compare run config.yaml
```

If Docker or an image is missing, the task is reported as a `harness_error`
(setup issue) rather than counting against the model.

**Up-front check.** Before a run, the harness reports Docker status for any
Docker-verified tasks: it confirms the daemon is reachable and that each required
image is present locally, and warns (non-fatally) if not — so you find out at
startup instead of per task. You can also check explicitly:

```bash
kirobench cli-compare validate config.yaml     # includes a Docker section
kirobench model-compare validate config.yaml
```

The `validate` output lists the required images (from each task's `verify.image`)
and flags a missing daemon or missing images with the
`./tasks/docker/build-images.sh` hint.

Current Docker tasks: `task-009-dotnet-invoicing` (C#), `task-010-java-ratelimiter`
(Java), `task-011-typescript-circuit-breaker` (TypeScript) — each a brownfield
bug-fix with four deterministic defects to find and fix.
