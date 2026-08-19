# agent-cost-bench Workshop

A hands-on workshop for setting up and running agent-cost-bench to compare the cost, speed, and correctness of AI coding CLI tools.

## Workshop Modules

| Module | Duration | Description |
|--------|----------|-------------|
| [Module 1: Setup](./01-setup.md) | 20 min | Install prerequisites, clone the repo, authenticate CLIs |
| [Module 2: Your First Run](./02-first-run.md) | 15 min | Run a minimal 2-task comparison between Kiro and Claude Code |
| [Module 3: Full CLI Comparison](./03-full-cli-compare.md) | 30 min | Run all 14 tasks across multiple CLIs, read the report |
| [Module 4: Model Comparison](./04-model-compare.md) | 20 min | Compare different models inside a single CLI |
| [Module 5: Docker Tasks](./05-docker-tasks.md) | 20 min | Set up Docker images for multi-language verification |
| [Module 6: Bring Your Own Task](./06-custom-task.md) | 25 min | Create a benchmark task from your own repo |
| [Module 7: Interpreting Results](./07-interpreting-results.md) | 15 min | Read reports, understand cost normalization, caveats |

## Prerequisites

- macOS or Linux (amd64/arm64)
- Python 3.10+
- At least two CLI tools installed (Kiro CLI + one of: Claude Code, GitHub Copilot, Cursor, Devin)
- Active subscriptions for the CLIs you want to benchmark

## Estimated Cost

A full 14-task run across 4 CLIs on Opus 4.8 costs approximately:
- Kiro: ~$0.85 total (~$0.06/task)
- Claude Code: ~$5.65 total (~$0.40/task)
- Copilot: ~$7.92 total (~$0.57/task)
- Cursor: ~$4.94 total (~$0.35/task)

Start with Module 2 (2 tasks, 2 CLIs) to validate your setup at minimal cost (~$0.50 total).
