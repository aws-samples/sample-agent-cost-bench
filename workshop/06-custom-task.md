# Module 6: Bring Your Own Task

Time: ~25 minutes

## Objectives

- Create a custom benchmark task from your own repository
- Write verification tests or rubric criteria
- Run the benchmark against your real-world use case

---

## Step 1: Decide your task type

| Approach | When to use | Effort |
|----------|-------------|--------|
| **Pytest verification** | You have deterministic tests that can validate the output | Write test file |
| **LLM rubric** | Subjective quality (code style, architecture decisions) | Write English criteria |
| **Custom scorer** | Complex validation logic (parse HCL, check Dockerfile patterns) | Write `score.py` |
| **Docker** | Need a specific runtime (Java, .NET, etc.) | Write Dockerfile + test |

## Step 2: Scaffold a new task

```bash
# For a pytest-verified task:
agent-cost-bench new-task my-bug-fix --with-tests

# For a rubric-graded task:
agent-cost-bench new-task my-refactor
```

This creates:

```
tasks/vibe/my-bug-fix/
├── task.yaml         # Task configuration + prompt
├── src/              # Seed files (optional — your repo code goes here)
└── verify/
    └── test_my_bug_fix.py   # Your verification tests
```

## Step 3: Configure task.yaml

### Option A: GitHub repo task (brownfield fix)

```yaml
id: fix-auth-bug
description: "Fix the failing authentication test"
timeout_minutes: 15
effort: high

prompt: |
  Fix the failing test in tests/test_auth.py. The test_refresh_token test
  is failing because the refresh endpoint doesn't properly validate expired
  tokens. Fix the implementation without changing the test.

repo:
  url: https://github.com/your-org/your-service.git
  ref: a1b2c3d4e5f6     # Pin to a specific commit for reproducibility
  token_env: GITHUB_TOKEN  # For private repos

verify:
  runner: pytest
  deps: [pytest, httpx, fastapi]
```

### Option B: Greenfield task (create from scratch)

```yaml
id: build-csv-parser
description: "Build a CSV parser CLI with specific requirements"
timeout_minutes: 10
effort: medium

prompt: |
  Create a Python CLI tool called `csvtool.py` that:
  1. Accepts a CSV file path as the first argument
  2. Supports --filter COLUMN=VALUE to filter rows
  3. Supports --sort COLUMN to sort output
  4. Supports --format (table|json|csv) for output format
  5. Prints to stdout

  Requirements:
  - Use only the standard library (no pandas)
  - Handle malformed CSV gracefully (skip bad rows, print count to stderr)
  - Exit code 0 on success, 1 on file not found, 2 on invalid arguments

verify:
  runner: pytest
  deps: [pytest]
```

### Option C: Rubric-graded task

```yaml
id: refactor-to-clean-arch
description: "Refactor a monolithic module into clean architecture"
timeout_minutes: 20
effort: high

prompt: |
  Refactor src/app.py into a clean architecture with separate layers:
  domain/, application/, infrastructure/, and presentation/.
  Keep all existing functionality working.

repo:
  url: https://github.com/your-org/monolith-service.git
  ref: main

quality:
  rubric:
    - "Domain layer contains pure business logic with no framework imports"
    - "Application layer defines use cases / service classes"
    - "Infrastructure layer handles database and external API calls"
    - "Presentation layer contains only route handlers"
    - "No circular imports between layers"
    - "All original tests still pass (if test_app.py exists)"
    - "Each layer is in its own directory under src/"
    - "Dependencies point inward (presentation → application → domain)"

functional_pass_threshold: 0.6
```

## Step 4: Write verification tests

For pytest tasks, put tests in `verify/`. These are hidden from the model — it never sees them.

```python
# tasks/vibe/my-bug-fix/verify/test_my_bug_fix.py
"""Verification tests — the model never sees this file."""
import subprocess
import sys
from pathlib import Path

WORKSPACE = Path(__file__).parent.parent.parent  # Set by harness via env var


def test_auth_module_exists():
    """Basic check that the fix didn't delete the module."""
    assert (Path(WORKSPACE) / "src" / "auth.py").exists()


def test_refresh_token_works():
    """Run the project's own test suite to verify the fix."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_auth.py::test_refresh_token", "-v"],
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Test failed:\n{result.stdout}\n{result.stderr}"
```

> **Tip:** Use the `WORKSPACE` environment variable (set by the harness) to locate files. Don't hardcode paths.

## Step 5: Test your task locally

Run your task with a single CLI to verify the prompt + tests work:

```yaml
# config.test-task.yaml
runners:
  - name: kiro
    display_name: "Kiro (claude-sonnet-4.6)"
    cli_path: kiro-cli
    model_id: claude-sonnet-4.6
    pricing: { usd_per_credit: 0.04 }
    cli_base_args: [chat, --no-interactive, --trust-all-tools,
                    "--model={model}", "--effort={effort}"]

tasks_dir: tasks
task_ids: [my-bug-fix]
modes: ["vibe"]
concurrency: per_target
timeout_minutes: 15
workspace_base: /tmp/agent-cost-bench-test
output_dir: results
open_report: true
```

```bash
agent-cost-bench cli-compare run config.test-task.yaml
```

## Step 6: Iterate on the prompt

If the model fails:
1. Check the workspace at `/tmp/agent-cost-bench-test/` to see what it produced
2. Adjust the prompt to be more specific
3. Lower `functional_pass_threshold` if partial credit is acceptable
4. Add more context to the prompt if the task is ambiguous

Once one CLI passes, add more CLIs and run the comparison.

---

## Checkpoint ✓

- [ ] You've created a task.yaml with your prompt
- [ ] Verification tests (or rubric) are in place
- [ ] At least one CLI passes the task
- [ ] You can run a comparison on your custom task

→ Continue to [Module 7: Interpreting Results](./07-interpreting-results.md)
