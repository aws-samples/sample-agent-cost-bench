# Module 7: Interpreting Results

Time: ~15 minutes

## Objectives

- Understand how cost is normalized across CLIs
- Know the caveats and limitations of the measurements
- Use results to make informed tool-selection decisions

---

## How cost is computed per CLI

Each CLI reports usage differently. The harness normalizes everything to USD:

| CLI | What it reports | How USD is derived |
|-----|----------------|-------------------|
| **Kiro** | Credits (e.g., 1.51 credits) | Credits × `usd_per_credit` ($0.04) |
| **Claude Code** | Direct USD | Reported `total_cost_usd` from JSON output |
| **Copilot** | AI Credits (per session) | AI Credits × `usd_per_premium_request` ($0.04) |
| **Cursor** | Token counts (in/out/cache) | Tokens × published per-token rates |
| **Devin** | Token counts (from ATIF export) | Tokens × published per-token rates |

## Key metrics in the report

### Avg Cost / Run
The raw average cost across all runs for that CLI, including failures.

### Cost / Success
Total cost ÷ number of passing runs. This is the **real cost of getting one working result**. If a CLI has an 86% pass rate, its cost/success is higher than its avg cost — you're paying for the failed attempts too.

### Pass Rate
What percentage of tasks produced correct, verified output. 100% means every task passed. Lower rates mean the CLI failed some tasks — either the model couldn't solve them or it hit infrastructure issues.

### Avg Duration
Time from prompt submission to CLI completion. This is CLI-self-reported time (not harness wall-clock), so it reflects the tool's own view of how long it took.

## How verification works

The harness never trusts the model's claim that it "completed" a task. Every result goes through independent verification:

| Method | Tasks using it | How it works |
|--------|---------------|-------------|
| pytest | rest-api, dashboard, dockerize-flask, harden-k8s, log-analyzer-cli | Run hidden test file against the workspace |
| Docker | dotnet-invoicing, java-ratelimiter, typescript-circuit-breaker, helm-chart, terraform-serverless-spa | Build/test inside a container with the correct runtime |
| LLM judge | note-cli, bedrock-sentiment, geotrack-duplicate-device | An independent model grades the output against rubric criteria |
| Custom scorer | terraform-s3 | Python script parses HCL and checks structural requirements |

## Caveats you should know

### 1. Pricing is self-reported and volatile
Costs are only as accurate as each tool's own usage reporting. Rates change — always verify against the vendor's current pricing page before drawing conclusions.

### 2. Credit/unit granularity varies
Kiro and Copilot meter in coarser units (credits) that may not scale linearly with complexity. Claude Code and Cursor bill at the token level. This means small tasks may show compressed differences.

### 3. Model parity isn't guaranteed
"Sonnet 4.6" may differ slightly across vendors (point versions, system prompts, tool scaffolding). Some cost or behavior differences come from implementation, not just pricing.

### 4. No MCP servers
All benchmark runs disable MCP servers. Every dollar reflects the CLI's interaction with the model alone — no external tool augmentation.

### 5. Rubric tasks are subjective
LLM-judge-graded tasks (note-cli, bedrock-sentiment, geotrack-duplicate-device) have inherent variability. The same code might score differently on repeat runs. Use these for directional comparison, not precise measurement.

### 6. Single-run variance
With `repeats: 1`, individual task costs can vary 10–30% between runs (model non-determinism, cache hit rates). For high-confidence numbers, run with `repeats: 3` and look at averages.

## Making decisions from the data

### "Which CLI should my team use?"

Consider the complete picture:

| Factor | What to look at |
|--------|----------------|
| Cost at volume | Avg cost × tasks/day × team size |
| Reliability | Pass rate — a cheaper CLI that fails 20% of the time costs more in rework |
| Speed | Duration — matters for interactive use, less for background agents |
| Task fit | Per-task breakdown — maybe CLI A is better for IaC and CLI B for bug fixes |

### "Is Opus worth the extra cost over Sonnet?"

Compare the same CLI on both models:
- If Opus gets 100% pass rate where Sonnet gets 85%, the effective cost/success may be similar
- If both get 100%, Sonnet is almost always the better value
- For complex brownfield tasks, Opus tends to shine more than for simple greenfield

### "Should I bring my own tasks?"

Yes, if:
- Your codebase has specific patterns the model needs to understand
- You want to measure against your actual test suite
- You're evaluating for a specific use case (e.g., "can it fix our flaky tests?")

The included 14 tasks give a broad baseline, but your mileage will vary on real code.

---

## Next steps

- **Automate**: Set up a weekly cron job to track cost/quality trends over time
- **Customize**: Add your own repos and tests (Module 6)
- **Scale**: Deploy the kiro-nomics web UI for team-wide access
- **Contribute**: Submit new tasks back to the repo via PR

---

## Checkpoint ✓

- [ ] You understand how each CLI's cost is normalized to USD
- [ ] You know the difference between avg cost and cost/success
- [ ] You're aware of the caveats (pricing volatility, credit granularity, model parity)
- [ ] You can make an informed recommendation based on the data

**Workshop complete!** 🎉
