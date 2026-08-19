# Module 2: Your First Run

Time: ~15 minutes

## Objectives

- Run a minimal benchmark (2 tasks, 2 CLIs)
- Understand the output structure
- View your first HTML report

---

## Step 1: Create a minimal config

Create `config.first-run.yaml`:

```yaml
comparison_label: "Workshop First Run (Kiro vs Claude Code)"

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

# Only run 2 quick tasks
tasks_dir: tasks
task_ids:
  - rest-api
  - terraform-s3
modes: ["vibe"]

concurrency: per_target
timeout_minutes: 10
repeats: 1
functional_pass_threshold: 0.99
workspace_base: /tmp/agent-cost-bench-workshop

output_dir: results
report_title: "Workshop First Run"
open_report: true
```

> **Adapt the runners** to match the CLIs you installed in Module 1. If you have Copilot instead of Claude Code, swap that runner in.

## Step 2: Run the benchmark

```bash
agent-cost-bench cli-compare run config.first-run.yaml
```

You'll see output like:

```
[14:32:01] Starting cli-compare run 20260817_143201_a1b2c3
[14:32:01] Runners: Kiro (claude-sonnet-4.6), Claude Code (claude-sonnet-4.6)
[14:32:01] Tasks: rest-api, terraform-s3
[14:32:01] Total runs: 4 (2 tasks × 2 CLIs × 1 repeat)
...
[14:34:15] Run complete. Results: results/20260817_143201_a1b2c3.json
[14:34:15] Report: results/20260817_143201_a1b2c3.html
```

The run takes ~2–4 minutes for 2 tasks with 2 CLIs.

## Step 3: Understand the output

The framework produces three files per run:

| File | Purpose |
|------|---------|
| `results/<run_id>.html` | Self-contained HTML report (opens in browser) |
| `results/<run_id>.json` | Machine-readable results with all metrics |
| `results/<run_id>.log` | Detailed execution log for debugging |

## Step 4: Read the report

The HTML report opens automatically. If it didn't, open it manually:

```bash
open results/20260817_*.html    # macOS
# or
xdg-open results/20260817_*.html  # Linux
```

The report shows:

1. **Summary table** — pass rate, avg cost, avg duration per CLI
2. **Per-task breakdown** — which tasks each CLI passed/failed, individual costs
3. **Cost comparison** — multipliers showing how much cheaper/expensive each CLI is relative to the others

---

## What just happened?

For each task × CLI combination:

1. The harness created a fresh workspace directory
2. It sent the task prompt to the CLI in headless mode
3. The CLI produced code in the workspace
4. The harness ran the task's verification tests (pytest for these two tasks)
5. It parsed cost from each CLI's native output format
6. It compiled everything into the report

## Expected costs for this run

| CLI | rest-api | terraform-s3 | Total |
|-----|----------|--------------|-------|
| Kiro | ~$0.03–0.05 | ~$0.02–0.03 | ~$0.05–0.08 |
| Claude Code | ~$0.20–0.30 | ~$0.15–0.22 | ~$0.35–0.52 |

---

## Checkpoint ✓

- [ ] The run completed without errors
- [ ] Both CLIs passed both tasks (or you understand why one failed)
- [ ] You can open and read the HTML report
- [ ] You see cost and duration data for each CLI

→ Continue to [Module 3: Full CLI Comparison](./03-full-cli-compare.md)
