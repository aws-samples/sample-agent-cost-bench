# Module 4: Model Comparison

Time: ~20 minutes

## Objectives

- Compare multiple models within a single CLI (Kiro)
- Understand the quality-vs-cost tradeoff across model tiers
- Use the LLM-as-judge for rubric-graded tasks

---

## Step 1: Understand model-compare mode

While `cli-compare` answers "which CLI is cheapest for the same model?", `model-compare` answers "which model gives the best quality-per-dollar inside a single CLI?"

This is useful for:
- Deciding whether to use Opus vs Sonnet for your daily work
- Evaluating new/cheaper models (DeepSeek, Minimax, GLM) against the baseline
- Finding the sweet spot between cost and quality for your task types

## Step 2: Create a model-compare config

Create `config.model-workshop.yaml`:

```yaml
kiro_cli_path: kiro-cli
effort: high

models:
  - claude-opus-4.8
  - claude-sonnet-4.6
  - deepseek-3.2

pricing:
  usd_per_credit: 0.04

# LLM-as-judge grades rubric + spec quality tasks
judge_model: claude-opus-4.8
judge_weight: 0.6

tasks_dir: tasks
task_ids:
  - rest-api
  - terraform-s3
  - log-analyzer-cli
  - note-cli
modes: ["vibe"]

concurrency: per_target
timeout_minutes: 20
repeats: 1
functional_pass_threshold: 0.99
workspace_base: /tmp/agent-cost-bench-model-compare

output_dir: results
report_title: "Workshop Model Compare"
open_report: true
```

## Step 3: Run the model comparison

```bash
agent-cost-bench model-compare run config.model-workshop.yaml
```

## Step 4: Read the model-compare report

The report shows:

1. **Quality scores per model** — functional correctness (0–100%)
2. **Cost per model** — credits consumed × price per credit
3. **Speed** — time to complete each task
4. **Quality vs Cost scatter** — which models give the best value

Key question the report answers: "Does Opus deliver enough extra quality over Sonnet to justify the ~3× cost increase for my task types?"

## Step 5: Interpret the scores

Each task is scored on multiple dimensions:

| Dimension | Source | Description |
|-----------|--------|-------------|
| Functional | Test suite or rubric | Does the code actually work? (0.0–1.0) |
| Task Completion | Heuristic | Were all requirements addressed? |
| Steering Adherence | Rule-based | Did it follow constraints (file names, APIs, etc.)? |

The final score is a weighted combination. A model with 100% functional but 60% steering might have cut corners that wouldn't fly in production.

---

## Checkpoint ✓

- [ ] You've compared at least 2 models on the same tasks
- [ ] You can identify which model gives the best quality-per-dollar
- [ ] You understand the scoring dimensions

→ Continue to [Module 5: Docker Tasks](./05-docker-tasks.md)
