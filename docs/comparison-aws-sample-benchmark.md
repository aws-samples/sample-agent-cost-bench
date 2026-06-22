# kirobench vs. aws-samples/sample-ai-coding-tools-benchmark

A comparison of this framework (**kirobench**) with the AWS sample benchmark at
<https://github.com/aws-samples/sample-ai-coding-tools-benchmark>, focused on
ease of use, tasks, the LLM-as-judge approach, and reporting.

> Note: this comparison is based on the AWS repo's `main` branch as reviewed
> (its README, `tools/gf1_qa.py`, and one evaluation report). If automation has
> been added since, some "manual" points below may be out of date.

## What each one is

**aws-samples/sample-ai-coding-tools-benchmark** — A published comparison plus a
lightweight methodology. Three scenarios (a greenfield CLI to-do app, and two
brownfield code-analysis prompts). Prompts live in markdown; a human runs each
tool by hand, drops the outputs into per-tool folders, and types the numbers
(cost, time, lines) into a markdown report. One Python QA script (`gf1_qa.py`)
deterministically checks the to-do app against its spec; the analysis tasks are
graded by reading. Cost is computed by hand (Kiro credits × $0.02, Claude Code
`/usage`, Codex API-equivalent pricing). The output is a committed markdown
report plus a static PNG chart.

**kirobench (this framework)** — An automated, config-driven harness.
`kirobench model-compare run config.yaml` spawns each CLI, captures transcripts,
parses cost/latency automatically, runs verification (pytest / Docker / rubric /
script), and generates HTML + JSON reports with charts and drill-downs.

## Comparison

| Dimension | AWS sample | kirobench (ours) |
|---|---|---|
| **Execution** | Manual — run each tool by hand, copy outputs into folders | Automated — one CLI command runs the full matrix |
| **Ease of use (non-programmer)** | Easy to *read* results; running requires manual discipline per tool | `pip install` + edit a YAML + one command; no per-run manual steps |
| **Config** | None — it's a process, not a tool | Declarative YAML (`models:` / `runners:`), env-var expansion |
| **Cost capture** | Manual (paste credits / `/usage`, multiply by rate) | Auto-parsed per CLI (Kiro credits, Claude JSON, Copilot AIU, tokens) -> USD + native units |
| **Latency** | Hand-recorded wall clock | Auto-captured (CLI-reported + wall clock) |
| **Correctness — deterministic** | One bespoke script (`gf1_qa.py`) for the to-do app | Centralized pytest runner, declarative Docker runner, graduated 0-1 scoring with checkpoints |
| **Correctness — subjective** | Human reads the analysis output | LLM-as-judge with per-criterion rubric + reference-solution anchoring |
| **LLM-as-judge** | Informal — the GF-1 prompt itself is an "ask an AI to verify" prompt; no structured scoring | Structured: per-criterion met/unmet JSON, score = fraction met, optional blend with rule-based, reference solution in prompt |
| **Tasks / BYO** | Add a markdown prompt; write a QA script if you want automation | Drop a `task.yaml` (inline prompt + rubric or hidden tests); `new-task` scaffolds it |
| **Task types** | Greenfield + brownfield analysis | Greenfield, brownfield bug-fix (multi-language via Docker), spec-driven, no-code rubric, clone-a-GitHub-repo |
| **Reporting** | Markdown table + static PNG, committed to repo | Auto HTML (charts, per-task drill-down, transcripts, pass@k) + JSON + run log |
| **Reproducibility** | Prompts are fixed; runs are manual, so subject to operator variance | Pinned repo SHAs, `repeats`/pass@k, isolated per-run workspaces, transient retries |
| **Concurrency** | N/A (manual) | `per_target` / `full` / numeric, with `max_concurrency` cap |
| **Scale** | Practical for ~3 tools x 3 prompts | Tasks x models x repeats, run in parallel |

## Where the AWS sample is genuinely better

1. **It tests Kiro IDE (the GUI), not just the CLI.** kirobench drives CLIs
   headlessly, so it fundamentally cannot benchmark an interactive IDE. The
   AWS sample's manual approach is the only way to include IDE results — a real
   coverage gap for any headless harness.
2. **Transparency / auditability.** Every output artifact is committed to the
   repo, so anyone can read exactly what each tool produced.
3. **Zero infrastructure.** No Python package, no Docker, no judge model. For a
   one-off blog-post comparison, it's lower friction to start.
4. **Published, real numbers.** It ships actual results people can cite today.

## Where kirobench has clear advantages

- **Repeatable at the press of a button** — no operator variance, runs the whole
  matrix unattended, supports `repeats` for pass@k and variance.
- **Automated cost + latency** across heterogeneous CLIs normalized to USD — the
  AWS sample does this by hand per tool with documented assumptions.
- **Graduated correctness** (0-1 with checkpoints) instead of a single
  PASS/FAIL, plus the anti-cheat pattern (hidden tests mounted read-only).
- **Structured LLM-as-judge** — per-criterion verdicts with reference-solution
  anchoring, designed for reproducibility, vs. an informal "ask an AI to check it."
- **No-code rubric tasks** — a non-programmer can author a gradable task with
  just a prompt + a checklist.
- **Real-repo brownfield tasks** — clone any GitHub repo at a pinned SHA and
  benchmark a feature/bug-fix against it.
- **Rich HTML report** — charts, pass-rate / cost / quality breakdowns, per-task
  drill-downs with the judge's reasoning, mode breakdowns. The AWS sample is a
  static table + one PNG.

## Honest summary

The AWS sample is a **demonstration and a methodology** — great for a
transparent, citable, IDE-inclusive one-time comparison with minimal setup.
kirobench is a **reusable measurement system** — it trades a bit of setup
(pip install, a judge model, optionally Docker) for automation, repeatability,
automated cost/quality scoring, bring-your-own tasks, and a polished report.

If the goal is "let customers run their own apples-to-apples comparisons
repeatedly and bring their own tasks," kirobench is the stronger foundation. The
one capability worth borrowing from the AWS sample is **IDE/GUI coverage**, which
no headless harness can replicate.
