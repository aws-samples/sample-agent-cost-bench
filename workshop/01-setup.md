# Module 1: Setup

Time: ~20 minutes

## Objectives

- Install Python and the agent-cost-bench framework
- Install and authenticate at least two coding CLIs
- Verify everything works with a validation check

---

## Step 1: Clone the repository

```bash
git clone https://github.com/aws-samples/sample-agent-cost-bench.git
cd sample-agent-cost-bench
```

## Step 2: Create a Python virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate    # On macOS/Linux
```

## Step 3: Install the framework

```bash
pip install -e .
```

Verify it installed correctly:

```bash
agent-cost-bench --version
```

## Step 4: Install coding CLIs

You need at least **two** CLIs to run a comparison. Install the ones you have subscriptions for:

### Kiro CLI

```bash
# Install via native installer (macOS/Linux)
curl -fsSL https://cli.kiro.dev/install | bash

# Or via Homebrew (macOS)
brew install --cask kiro-cli

# Authenticate (opens browser)
kiro-cli login
```

Verify: `kiro-cli chat --no-interactive --trust-all-tools --model=claude-sonnet-4.6 "say hello"` should print a response.

> Reference: [kiro.dev/docs/getting-started/installation](https://kiro.dev/docs/getting-started/installation/)

### Claude Code

```bash
# Install via native installer (macOS/Linux/WSL) — recommended, no Node.js required
curl -fsSL https://claude.ai/install.sh | bash

# Or via npm (all platforms, requires Node.js 18+)
npm install -g @anthropic-ai/claude-code

# Authenticate (opens browser)
claude login
```

Verify: `claude -p "say hello" --output-format json --model claude-sonnet-4-6 --dangerously-skip-permissions` should print JSON.

> Reference: [docs.claude.com/en/docs/claude-code/setup](https://docs.claude.com/en/docs/claude-code/setup)

### GitHub Copilot CLI

```bash
# Install via Homebrew (macOS/Linux) — recommended
brew install --cask copilot-cli

# Or via npm (all platforms, requires Node.js 22+)
npm install -g @github/copilot

# Or via install script (macOS/Linux)
curl -fsSL https://gh.io/copilot-install | bash

# Authenticate (on first launch, use /login command)
copilot
# Then type: /login
```

Verify: `copilot -p "say hello" --model claude-sonnet-4.6 --allow-all-tools --output-format json` should print JSON.

> Reference: [docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli)

### Cursor (agent CLI)

```bash
# Install via native installer (macOS/Linux/WSL)
curl https://cursor.com/install -fsS | bash

# Authenticate — run once interactively to log in via browser
agent --login
```

The CLI binary is called `agent`. On first run it prompts for browser-based auth and stores the token in your system keychain.

Verify: `agent -p "say hello" --trust --yolo --output-format json --model claude-sonnet-4-6` should print JSON.

> Reference: [cursor.com/docs/cli/installation](https://cursor.com/docs/cli/installation)

### Devin

```bash
# Install via native installer (macOS/Linux/WSL)
curl -fsSL https://cli.devin.ai/install.sh | bash

# Authenticate
devin auth login
```

> **Note:** Devin requires an active Devin subscription. An admin must enable CLI access in your team's Devin settings.

Verify: `devin -p "say hello" --model claude-opus-4-8 --export test.json` should complete and write `test.json`.

> Reference: [docs.devin.ai/get-started/first-run](https://docs.devin.ai/get-started/first-run)

## Step 5: Set environment variables

Some CLIs need environment variables. Add to your shell profile (`~/.zshrc` or `~/.bashrc`):

```bash
# Only set the ones for CLIs you're using:
export KIRO_API_KEY=...          # If not using `kiro login`
export ANTHROPIC_API_KEY=...     # If not using `claude login`
export GITHUB_TOKEN=...          # If not using `copilot auth login`
```

## Step 6: Validate your setup

The framework has a built-in validation command:

```bash
# Copy the example config
cp config.cli-compare.example.yaml config.workshop.yaml
```

Edit `config.workshop.yaml` — keep only the runners for CLIs you've installed. For example, if you have Kiro and Claude Code:

```yaml
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
```

Run validation:

```bash
agent-cost-bench cli-compare validate config.workshop.yaml
```

You should see green checkmarks for each runner. If any fail, check the CLI installation and auth steps above.

---

## Checkpoint ✓

Before moving on, confirm:
- [ ] `agent-cost-bench --version` prints a version number
- [ ] At least 2 CLIs respond to a "say hello" prompt
- [ ] `agent-cost-bench cli-compare validate config.workshop.yaml` passes

→ Continue to [Module 2: Your First Run](./02-first-run.md)
