# Security

kirobench benchmarks coding agents by **running them and the code they
produce**. Please read this before running it outside a throwaway environment.

## Threat model — read this first

kirobench is not a sandbox. By design it executes code that you should treat as
untrusted:

- **AI-generated code runs on your machine.** Every task asks a model (via a
  coding CLI) to write code, and the framework then builds, imports, and tests
  that code to score it. A model can produce anything — including code that
  reads files, opens network connections, or runs shell commands.
- **The CLIs run with all tool permissions enabled.** The example configs launch
  agents with full autonomy flags (`--trust-all-tools`,
  `--dangerously-skip-permissions`, `--allow-all-tools`) so runs are
  non-interactive. The agent can run arbitrary commands with your user
  privileges for the duration of a task.
- **Third-party repositories are cloned and executed.** GitHub repo tasks
  (`repo:` in a task) clone a real repository into the workspace; its files are
  then installed (`pip install -r requirements.txt`), imported, and tested on the
  host by the `local`/`pytest` verify runners.
- **`verify:` blocks and verify scripts are trusted task-author input.** They are
  executed as written. Only run task suites you trust, or author your own.

In short: **running kirobench can result in arbitrary code execution as your
user.** This is inherent to benchmarking autonomous coding agents.

## Running safely

- **Use a disposable, isolated environment.** Run in a throwaway VM, a CI runner,
  or a container you can discard — not your primary workstation or any host with
  access to production credentials or sensitive data.
- **Prefer the Docker verify runner for untrusted tasks.** Tasks with a
  `verify:` block set to `runner: docker` execute the build/test step inside a
  container with the model's code mounted read-only and (recommended)
  `network: none`. This contains the *verification* step. Note the *agent* phase
  (the CLI writing code) still runs on the host.
- **Scope credentials tightly.** Don't run on a machine logged into accounts or
  cloud roles you wouldn't hand to an arbitrary script. Use least-privilege
  tokens.
- **Review task suites before running them.** Treat a third-party task directory
  the way you'd treat any code you're about to execute.

## Handling secrets

- **Never commit API keys.** Configs support `${VAR}` / `${VAR:-default}`
  expansion — keep keys in environment variables and reference them, e.g.
  `kiro_api_key: ${KIRO_API_KEY}`. The provided `.gitignore` excludes
  `config.*.yaml` (keeping only `config.*.example.yaml`) and `.env*`.
- **API keys are passed to subprocesses via the environment only** — never on the
  command line — so they don't appear in process listings or run logs.
- **Private repositories.** Set `repo.token_env` to the *name* of an environment
  variable holding a personal access token (e.g. `token_env: GITHUB_TOKEN`),
  never the token itself. The token is read at runtime and passed to git through
  its ephemeral `GIT_CONFIG_*` interface, so it is not written to disk
  (`.git/config`) or visible in `ps`. Token auth is HTTPS-only.
- **`results/` may contain sensitive output.** Run logs capture full prompts and
  model stdout/stderr, which can include anything a model emits. `results/` is
  gitignored; review before sharing a report or log.

## Hardening already in place

- **Git transport allowlist.** All clones run with
  `GIT_ALLOW_PROTOCOL=https:http:ssh:git`, blocking `ext::` (arbitrary command
  execution) and `file::` (local file read) transports in a crafted `repo.url`.
  Interactive credential prompts are disabled (`GIT_TERMINAL_PROMPT=0`).
- **No shell string interpolation in the hot path.** Process launches use
  argument lists (`create_subprocess_exec`), not `shell=True`.
- **YAML is parsed with `yaml.safe_load`** everywhere.
- **Path containment.** A task's `reference_solution` is refused if it resolves
  outside the task directory.

## Reporting a vulnerability

Please do not open a public issue for security problems. Report them privately to
the maintainers (see the repository's contact/owner information) with steps to
reproduce. We'll acknowledge receipt and work with you on a fix and disclosure
timeline.
