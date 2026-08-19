# Module 3: Full CLI Comparison

Time: ~30 minutes (includes 20 min run time)

## Objectives

- Run all 14 benchmark tasks across your CLIs
- Compare cost, duration, and pass rates at scale
- Understand the different task categories

---

## Step 1: Review the task suite

The benchmark includes 14 tasks across 5 categories:

| Category | Tasks | What they test |
|----------|-------|---------------|
| Development (4) | rest-api, dashboard, log-analyzer-cli, note-cli | Greenfield code generation |
| Bug fixing (4) | dotnet-invoicing, java-ratelimiter, typescript-circuit-breaker, geotrack-duplicate-device | Brownfield fixes in existing code |
| DevOps (3) | dockerize-flask, harden-k8s, helm-chart | Containerization & K8s hardening |
| IaC (2) | terraform-s3, terraform-serverless-spa | Terraform infrastructure definitions |
| Migration (1) | bedrock-sentiment | AWS service migration |

## Step 2: Create the full config

Create `config.full-run.yaml`:

```yaml
comparison_label: "Workshop Full Run — Sonnet 4.6"

runners:
  - name: kiro
    display_name: "Kiro (claude-sonnet-4.6)"
    cli_path: kiro-cli
    model_id: claude-sonnet-4.6
    pricing:
      usd_per_credit: 0.04
    cli_base_args: [chat, --no-interactive, --trust-all-tools,
                    "--model={model}", "--effort={effort}"]

  - name: claude-code
    display_name: "Claude Code (claude-sonnet-4.6)"
    cli_path: claude
    model_id: claude-sonnet-4-6
    cli_base_args: ["-p", "{prompt}", "--output-format", "json",
                    "--model", "{model}", "--dangerously-skip-permissions",
                    "--effort", "{effort}"]

  - name: copilot
    display_name: "GitHub Copilot (claude-sonnet-4.6)"
    cli_path: copilot
    model_id: claude-sonnet-4.6
    pricing:
      usd_per_premium_request: 0.04
    cli_base_args: ["-p", "{prompt}", "--model", "{model}",
                    "--allow-all-tools", "--output-format", "json",
                    "--effort", "{effort}"]

# Run all tasks (leave task_ids empty or omit it)
tasks_dir: tasks
task_ids:
modes: ["vibe"]

# LLM-as-judge for rubric-graded tasks (note-cli, bedrock-sentiment, geotrack-duplicate-device)
judge_cli_path: kiro-cli
judge_model: claude-opus-4.8
judge_weight: 0.6

concurrency: per_target
timeout_minutes: 20
repeats: 1
functional_pass_threshold: 0.99
workspace_base: /tmp/agent-cost-bench-full-run

output_dir: results
report_title: "Workshop Full Run — Sonnet 4.6"
open_report: true
```

> **Note:** Remove any runners for CLIs you don't have. The comparison works with 2+ CLIs.

> **Note:** The `judge_model` and `judge_cli_path` are needed for rubric-graded tasks (note-cli, bedrock-sentiment, geotrack-duplicate-device). If you skip those tasks, you can omit the judge config.

## Step 3: Check which tasks need Docker

Some tasks verify results inside Docker containers:

| Task | Docker image required |
|------|---------------------|
| dotnet-invoicing | `agent-cost-bench-dotnet:8.0` |
| java-ratelimiter | `agent-cost-bench-java:21` |
| typescript-circuit-breaker | `agent-cost-bench-node:20` |
| terraform-serverless-spa | `agent-cost-bench-terraform:1.9` |
| helm-chart | `agent-cost-bench-helm:3.16` |

If you don't have Docker, exclude these by adding to your config:

```yaml
task_ids:
  - rest-api
  - dashboard
  - log-analyzer-cli
  - note-cli
  - dockerize-flask
  - harden-k8s
  - terraform-s3
  - bedrock-sentiment
  - geotrack-duplicate-device
```

If you do have Docker, see [Module 5](./05-docker-tasks.md) to build the images first.

## Step 4: Run it

```bash
agent-cost-bench cli-compare run config.full-run.yaml
```

This takes ~15–25 minutes depending on the number of CLIs and whether Docker tasks are included.

## Step 5: Analyze the results

Open the HTML report. You'll see:

### Summary table

```
CLI              Pass Rate   Avg Cost/Task   Avg Duration
Kiro             100%        $0.0251         31.2s
Claude Code      100%        $0.2064         59.4s
Copilot          100%        $0.1575         57.3s
```

### Key metrics to look at

1. **Cost/Success** — the real per-task cost normalized by pass rate. If a CLI fails some tasks, this is higher than avg cost.
2. **Cost multiplier** — how many × cheaper/expensive versus the cheapest CLI.
3. **Per-task breakdown** — identify which task categories each CLI is strongest/weakest on.

## Step 6: Try a different model

Run the same comparison on Opus 4.8 to see how cost scales with model capability:

```bash
# Quick way: sed the model in your config
sed 's/sonnet-4.6/opus-4.8/g; s/sonnet-4-6/opus-4-8/g' config.full-run.yaml > config.full-run-opus.yaml
agent-cost-bench cli-compare run config.full-run-opus.yaml
```

Compare the two reports side by side to see how model choice affects cost per CLI.

---

## Checkpoint ✓

- [ ] A full 14-task (or 9-task without Docker) run completed
- [ ] You can read the summary table and identify the cheapest/fastest CLI
- [ ] You understand which task categories each CLI handles best

→ Continue to [Module 4: Model Comparison](./04-model-compare.md)
