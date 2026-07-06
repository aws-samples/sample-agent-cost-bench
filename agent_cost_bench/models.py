"""
Core data models for agent_cost_bench.

This module unifies the two original harnesses (agent-cli-bench's ``RunnerSpec``
and kiro-bench's ``ModelSpec``) into a single ``Target`` abstraction, plus the
shared enums, scoring weights, usage, config, and result types used by both the
``cli-compare`` and ``model-compare`` modes.

Design highlights
-----------------
* ``Target`` carries everything needed to invoke ONE CLI with ONE model:
  command templates, a ``cost_source`` describing how to derive cost, pricing,
  and capability flags (``supports_spec`` / ``supports_agents``).
* ``BenchConfig`` is the single internal config the runner/reporter consume.
  The two YAML schemas (cli-compare / model-compare) desugar into it via the
  loaders in :mod:`agent_cost_bench.config`.
* ``RunResult`` is a superset result usable by both modes — cost is always
  recorded as both USD and native units (credits / premium requests), and the
  four quality dimensions are present (zero/unused in cli-compare).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, field_validator, model_validator

if TYPE_CHECKING:
    from .stats import RepeatStats


def _utcnow() -> datetime:
    """Timezone-aware UTC now (replaces deprecated datetime.utcnow)."""
    return datetime.now(timezone.utc)


# Max characters of raw CLI stdout/stderr persisted per run in the JSON/HTML
# reports. The full, untruncated transcript always remains in the .log file.
_TRANSCRIPT_LIMIT = 20000


def _clip_transcript(text: str) -> str:
    """Strip ANSI codes and truncate a CLI transcript for the reports."""
    from .logger import strip_ansi

    text = strip_ansi(text or "")
    if len(text) > _TRANSCRIPT_LIMIT:
        omitted = len(text) - _TRANSCRIPT_LIMIT
        # Keep the tail — cost/latency telemetry is usually at the end.
        text = (
            f"… [{omitted} chars truncated; see .log for full transcript] …\n"
            + text[-_TRANSCRIPT_LIMIT:]
        )
    return text


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CompareMode(str, Enum):
    """Which benchmark mode produced/consumes a config or run."""

    CLI_COMPARE = "cli-compare"
    MODEL_COMPARE = "model-compare"


class TaskMode(str, Enum):
    VIBE = "vibe"
    SPEC_DRIVEN = "spec-driven"


class SpecWorkflow(str, Enum):
    REQUIREMENTS_FIRST = "requirements-first"
    DESIGN_FIRST = "design-first"
    QUICK_PLAN = "quick-plan"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"
    UNAVAILABLE = "unavailable"  # CLI/model service was down during the run


class CostSource(str, Enum):
    """How a target's cost is derived from its CLI output."""

    KIRO_CREDITS = "kiro_credits"        # parse "Credits: X" telemetry, × usd_per_credit
    CLAUDE_JSON = "claude_json"          # parse `claude -p --output-format json` total_cost_usd
    COPILOT_JSON = "copilot_json"        # parse `copilot --output-format json` JSONL + session-state AIU
    CODEX_JSON = "codex_json"            # parse `codex exec --json` JSONL turn.completed events
    CURSOR_JSON = "cursor_json"          # parse `cursor -p --output-format json` result event
    TOKENS = "tokens"                    # parse token counts via regex, price per-token
    PREMIUM_REQUEST = "premium_request"  # fixed N premium/credit requests per run × price
    KAS_PROXY_METRICS = "kas_proxy_metrics"  # read kas-proxy's metrics.jsonl, correlated by run_id
    NONE = "none"                        # no cost data available


# ---------------------------------------------------------------------------
# Cost / pricing / usage
# ---------------------------------------------------------------------------


class Pricing(BaseModel):
    """
    Pricing knobs used to normalize each CLI's native cost unit to USD.

    Only the fields relevant to a target's ``cost_source`` are used:
      - kiro_credits      -> usd_per_credit
      - claude_json       -> (none; CLI reports total_cost_usd directly)
      - copilot_json      -> usd_per_premium_request (fallback) and/or token rates
      - codex_json        -> usd_per_input_token + usd_per_output_token + usd_per_reasoning_token
      - tokens            -> usd_per_input_token + usd_per_output_token
      - premium_request   -> usd_per_premium_request
      - kas_proxy_metrics -> kas_metrics_file + kas_metrics_timeout_seconds
    """

    usd_per_credit: float | None = Field(
        default=None, description="USD value of one Kiro credit (for kiro_credits)"
    )
    usd_per_input_token: float | None = Field(
        default=None, description="USD per input token (for tokens / codex_json cost_source)"
    )
    usd_per_cached_input_token: float | None = Field(
        default=None,
        description=(
            "USD per cached input token read from cache (codex_json / cursor_json). "
            "When None, cached tokens are billed at the regular input rate. "
            "For Codex o4-mini (standard): $0.275/1M = $0.000000275/token."
        ),
    )
    usd_per_cache_write_token: float | None = Field(
        default=None,
        description=(
            "USD per cache write token (cursor_json cost_source). "
            "Cursor bills cache writes at a separate rate from regular input. "
            "For Cursor Opus 4.8: $6.25/1M = $0.00000625/token. "
            "When None, cache write tokens are billed at the regular input rate."
        ),
    )
    usd_per_output_token: float | None = Field(
        default=None, description="USD per output token (for tokens / codex_json cost_source)"
    )
    usd_per_reasoning_token: float | None = Field(
        default=None,
        description=(
            "USD per reasoning / thinking token (codex_json cost_source). "
            "When None, reasoning tokens are billed at the regular output rate."
        ),
    )
    usd_per_premium_request: float | None = Field(
        default=None, description="USD per premium/credit request (Copilot)"
    )
    requests_per_run: float = Field(
        default=1.0, description="Premium requests counted per run for premium_request"
    )
    # ---- kas_proxy_metrics cost source ----
    kas_metrics_file: str | None = Field(
        default=None,
        description=(
            "Path to kas-proxy's metrics.jsonl. The parser reads this file and "
            "looks up the record matching the per-turn KAS_RUN_ID. Defaults to "
            "~/.kas-proxy/metrics.jsonl when None."
        ),
    )
    kas_metrics_timeout_seconds: float = Field(
        default=5.0, ge=0.0,
        description=(
            "How long to wait after the CLI exits before giving up on a "
            "matching record (the proxy may write the line fractionally after "
            "the CLI returns control)."
        ),
    )


@dataclass
class Usage:
    """Normalized usage parsed from a CLI invocation. Cost is recorded both as
    USD and in native units (credits / premium requests) for transparency."""

    cost_usd: float | None = None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None          # tokens served from cache (Codex)
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None      # thinking/reasoning tokens (Codex o-series)
    seconds: float | None = None
    # Native units, kept for side-by-side reporting
    raw_credits: float | None = None
    premium_requests: float | None = None


# ---------------------------------------------------------------------------
# Unified Target (core abstraction merging RunnerSpec + ModelSpec)
# ---------------------------------------------------------------------------


class TargetCapabilities(BaseModel):
    """What a target's CLI supports. Gates spec phases and agent flags."""

    supports_spec: bool = Field(
        default=False, description="CLI can run the 4-phase spec-driven workflow (Kiro)"
    )
    supports_agents: bool = Field(
        default=False, description="CLI accepts a per-phase --agent flag (Kiro)"
    )
    requires_pty: bool = Field(
        default=False,
        description="CLI requires a pseudo-terminal to run without hanging "
        "(e.g. Cursor CLI blocks on stdout when no TTY is attached)",
    )


class Target(BaseModel):
    """
    One CLI invoked with one model — the unit every mode benchmarks.

    The command is assembled from templates so new CLIs need no code changes::

        [cli_path] + rendered(cli_base_args) + cli_model_flag + cli_agent_flag
                   + cli_effort_flag + extra_args [+ prompt if not already used]

    ``{model}``, ``{prompt}``, ``{agent}`` and ``{effort}`` are substituted per
    call. Tokens that resolve to empty are dropped. If no rendered arg contained
    ``{prompt}`` the prompt is appended as the final argument.

    * cli-compare runners put ``{prompt}`` / ``{model}`` directly in
      ``cli_base_args`` and leave the flag templates blank.
    * model-compare (Kiro) targets leave ``cli_base_args`` prompt-free and set
      ``cli_model_flag`` / ``cli_agent_flag`` / ``cli_effort_flag``.
    """

    model_config = {"protected_namespaces": ()}

    name: str = Field(..., description="Short id, e.g. 'kiro', 'claude-code', 'sonnet'")
    display_name: str | None = Field(default=None, description="Human-friendly name in reports")
    cli_path: str = Field(..., description="Path/name of the CLI binary")
    model_id: str = Field(
        ..., description="Model id this CLI uses (per-target → different models per CLI)"
    )

    # Command templates (model-compare style flags; blank by default)
    cli_base_args: list[str] = Field(
        default_factory=list,
        description="Arguments after the binary; templated with {model}/{prompt}/{agent}/{effort}",
    )
    cli_model_flag: str = Field(
        default="", description="Template for the model flag, e.g. '--model={model}'. Blank = none."
    )
    cli_agent_flag: str = Field(
        default="", description="Template for the agent flag, e.g. '--agent={agent}'. Blank = none."
    )
    cli_effort_flag: str = Field(
        default="", description="Template for the effort flag, e.g. '--effort={effort}'. Blank = none."
    )
    extra_args: list[str] = Field(
        default_factory=list, description="Extra args appended verbatim (forward-compat)"
    )
    spec_mode_args: list[str] = Field(
        default_factory=list,
        description="Args injected ONLY for spec-driven tasks (e.g. ['--mode', 'spec']). "
        "Kiro targets default to native spec mode; empty means the CLI has no spec mode.",
    )

    # Cost derivation
    cost_source: CostSource = Field(default=CostSource.NONE)
    pricing: Pricing = Field(default_factory=Pricing)
    token_regex: str | None = Field(
        default=None, description="Regex with named input/output groups (cost_source=tokens)"
    )

    # Capabilities
    capabilities: TargetCapabilities = Field(default_factory=TargetCapabilities)

    env: dict[str, str] = Field(default_factory=dict, description="Extra env vars for this target")
    enabled: bool = Field(default=True, description="Set false to skip without deleting")

    # Optional grouping label (cli-compare's comparison_label). Reporting only.
    comparison_label: str | None = Field(default=None, exclude=True)

    @property
    def label(self) -> str:
        return self.display_name or self.name


import re as _re

# ---------------------------------------------------------------------------
# Scoring weights + task config
# ---------------------------------------------------------------------------


class RepoSpec(BaseModel):
    """
    A GitHub (or any git) repository cloned into the workspace before the model
    runs. Enables realistic brownfield tasks against real public repos.

    The clone is cached under ``workspace_base/.repo_cache/<url_hash>/<ref>/``
    so parallel model runs on the same repo+ref share one network fetch.

    Best practice: pin ``ref`` to a full 40-char commit SHA for reproducibility.
    A branch name yields different code every time the branch advances.
    """

    model_config = {"protected_namespaces": ()}

    url: str = Field(..., description="HTTPS or SSH URL of the repository")
    ref: str = Field(
        default="main",
        description=(
            "Branch, tag, or full 40-char commit SHA. "
            "A SHA is strongly recommended — branch names are not reproducible."
        ),
    )
    subdir: str | None = Field(
        default=None,
        description=(
            "Clone only this subdirectory into the workspace src/ (sparse checkout). "
            "Useful for monorepos. E.g. 'src', 'packages/core'."
        ),
    )
    depth: int = Field(
        default=1, ge=0,
        description="git clone --depth. 1 = shallow (fast); 0 = full history.",
    )
    token_env: str | None = Field(
        default=None,
        description=(
            "Name of an environment variable holding a personal access token for "
            "cloning a PRIVATE repo over HTTPS (e.g. 'GITHUB_TOKEN'). Put the "
            "variable NAME here, never the token itself. The token is read at "
            "runtime and sent via an ephemeral auth header — it is never written "
            "to disk (.git/config) or visible in the process list."
        ),
    )
    token_user: str = Field(
        default="x-access-token",
        description=(
            "Username paired with the token for HTTP basic auth. Defaults to "
            "'x-access-token' (works for GitHub). Use 'oauth2' for GitLab, "
            "'x-token-auth' for Bitbucket."
        ),
    )

    @property
    def is_sha_pinned(self) -> bool:
        """True when ref is a full 40-character hex commit SHA."""
        return bool(_re.match(r"^[0-9a-f]{40}$", self.ref, _re.I))


class ScoringWeights(BaseModel):
    functional_tests: float = Field(default=1.0, ge=0.0, le=1.0)
    spec_artifact_quality: float = Field(default=0.0, ge=0.0, le=1.0)
    task_completion_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    steering_adherence: float = Field(default=0.0, ge=0.0, le=1.0)


class VerifySpec(BaseModel):
    """
    Declarative verification with two runners:

    * ``runner: docker`` — run ``test_cmd`` inside a Docker ``image`` and parse
      the result with a named ``parser`` → a graduated score. Tests live in
      ``tests_subdir`` (mounted read-only). Used for compiled/multi-language tasks.
    * ``runner: local`` — the framework creates an isolated virtualenv in the
      workspace, installs ``deps`` (plus the model's ``requirements.txt``), and
      runs the ``score`` script, which prints a ``AGENT_COST_BENCH_RESULT`` marker. This
      replaces the per-task ``verify.sh`` boilerplate (venv + pip + run scorer).

    ``runner`` is inferred when omitted: ``docker`` if ``image`` is set, else
    ``local``.

    Docker in-container contract (provided by the generic runner as env vars):
      $SRC_RO      read-only mount of the model's workspace ``src/``
      $TESTS_RO    read-only mount of the task's authoritative tests
      $BUILD       writable scratch dir; ``src/`` is pre-copied to $BUILD/src
      $RESULTS_DIR writable dir the test command should write its report into

    Local scorer contract: invoked as ``python <score> <workspace> <task_dir>``
    with ``WORKSPACE`` and ``TASK_DIR`` in the environment; it must print a
    ``AGENT_COST_BENCH_RESULT: {...}`` line with a 0.0–1.0 ``score``.
    """

    model_config = {"protected_namespaces": ()}

    # Runner selection (inferred from `image` when omitted).
    runner: str | None = Field(
        default=None, description="'docker' or 'local'; inferred from `image` if omitted"
    )

    # --- local runner ---
    deps: list[str] = Field(
        default_factory=list,
        description="pip packages installed into the isolated verify venv (local runner)",
    )
    score: str | None = Field(
        default=None,
        description="task-relative scorer script that prints a AGENT_COST_BENCH_RESULT marker (local runner)",
    )

    # --- docker runner ---
    image: str | None = Field(default=None, description="Docker image to run verification in")
    test_cmd: str | None = Field(default=None, description="Shell command that runs the tests")
    parser: str = Field(
        default="exit-code",
        description="Result parser: trx|junit-xml|pytest-json|vitest-json|tap|regex|exit-code",
    )
    setup: list[str] = Field(
        default_factory=list, description="Shell commands run in $BUILD before test_cmd"
    )
    workdir: str = Field(default="src", description="Subdir under $BUILD to run test_cmd in")
    tests_subdir: str | None = Field(
        default="verify/tests",
        description="Task-relative dir of authoritative tests, mounted ro at $TESTS_RO",
    )
    regex: str | None = Field(default=None, description="Pattern for the 'regex' parser")
    network: str = Field(default="none", description="docker run --network value")
    timeout_seconds: int | None = Field(default=None)

    @model_validator(mode="after")
    def _infer_and_validate_runner(self) -> "VerifySpec":
        if self.runner is None:
            self.runner = "docker" if self.image else "local"
        if self.runner not in ("docker", "local", "pytest"):
            raise ValueError(
                f"verify.runner must be 'docker', 'local', or 'pytest', got '{self.runner}'"
            )
        if self.runner == "docker":
            if not self.image:
                raise ValueError("verify.runner=docker requires an 'image'")
            if not self.test_cmd:
                raise ValueError("verify.runner=docker requires a 'test_cmd'")
        elif self.runner == "local":
            if not self.score:
                raise ValueError("verify.runner=local requires a 'score' script path")
        # pytest: test files are discovered from verify/test_*.py; no extra fields required.
        return self


class RubricSpec(BaseModel):
    """
    No-code quality grading: a plain-English checklist scored by the LLM judge.

    Authoring a task this way needs zero verification code — a non-programmer
    writes a prompt plus a list of acceptance criteria, and the judge grades the
    produced files against each criterion (met / unmet). The functional score is
    the fraction of criteria met. Requires ``judge_model`` to be configured.
    """

    model_config = {"protected_namespaces": ()}

    rubric: list[str] = Field(
        default_factory=list,
        description="Plain-English acceptance criteria, each graded met/unmet by the judge",
    )
    reference_solution: str | None = Field(
        default=None,
        description="Optional task-relative path (file or dir) to a golden solution "
        "shown to the judge to anchor its grading",
    )


class TaskConfig(BaseModel):
    """Parsed from each task's task.yaml. Covers vibe and spec-driven tasks."""

    id: str
    mode: TaskMode = TaskMode.VIBE
    description: str = ""
    # The task prompt, inline in task.yaml.
    prompt: str = ""
    timeout_minutes: int = 15
    # Per-task effort level (low/medium/high). Controls how much reasoning the
    # model applies. When set, overrides the run-level default from the config.
    # Leave unset (None) to inherit the config's effort for backward compat.
    effort: str | None = Field(default=None)
    trust_tools: list[str] = Field(default=["read", "write", "shell"])

    # Minimum graduated functional score (0.0–1.0) for a PASS on tasks that
    # have a functional test. Default 0.99 = "everything must work".
    functional_pass_threshold: float = Field(default=0.99, ge=0.0, le=1.0)

    # Optional Docker image this task's verification needs (e.g. a compiled-
    # language task verified in a container). Used by preflight to check the
    # image exists locally before the run. None = no Docker needed.
    docker_image: str | None = Field(default=None)

    # Declarative verification (preferred over a bespoke verify.sh). When set,
    # the generic Docker verify runner executes it.
    verify: VerifySpec | None = Field(default=None)

    # No-code quality grading via an LLM-judge checklist. When set and no
    # code-based verification (Docker verify / verify.sh / pytest) exists, the
    # rubric becomes the functional signal: each criterion is graded met/unmet
    # by the judge and the score is the fraction met. Lets non-programmers
    # author tasks without writing tests. Requires judge_model in the config.
    quality: RubricSpec | None = Field(default=None)

    # GitHub (or any git) repository cloned into the workspace before the model
    # runs. Enables brownfield tasks against real repos without shipping source
    # inside the benchmark repository. Clones are cached across parallel runs.
    repo: RepoSpec | None = Field(default=None)

    # Spec-driven specific
    spec_workflow: SpecWorkflow = SpecWorkflow.REQUIREMENTS_FIRST
    seed_spec: bool = False
    require_all_tasks: bool = False

    # Scoring
    scoring: ScoringWeights = Field(default_factory=ScoringWeights)

    # Resolved at load time (not in yaml)
    task_dir: Path | None = Field(default=None, exclude=True)


# ---------------------------------------------------------------------------
# Unified top-level config (both YAML schemas desugar into this)
# ---------------------------------------------------------------------------


class BenchConfig(BaseModel):
    """
    The single internal configuration the runner and reporters consume. The
    cli-compare and model-compare YAML schemas are parsed by the loaders in
    :mod:`agent_cost_bench.config` and desugared into this unified shape.
    """

    model_config = {"protected_namespaces": ()}

    mode: CompareMode

    # Targets (desugared from runners: / models:)
    targets: list[Target] = Field(default_factory=list)

    # cli-compare reporting label describing the comparison (a grouping label
    # only — each runner still uses its own model_id, which may differ).
    comparison_label: str = Field(default="cross-CLI comparison")

    # ---- Kiro connection / model-compare agents (ignored by cli-compare) ----
    kiro_api_key: str = Field(default="")
    kiro_cli_path: str = Field(default="kiro")
    effort: str = Field(
        default="high",
        description="Default effort level; per-task effort in task.yaml takes precedence.",
    )
    vibe_agent: str | None = Field(default=None)
    spec_driver_agent: str | None = Field(default=None)
    spec_executor_agent: str | None = Field(default=None)
    # Native `--mode spec` requires an interactive TTY; run spec tasks under a
    # pseudo-terminal so the CLI behaves as it does in a real terminal.
    spec_use_pty: bool = Field(default=True)
    # Some CLIs read the spec request from stdin in `--mode spec` (the request
    # is NOT a positional arg). Set true to pipe the prompt to stdin for spec
    # tasks instead of appending it as an argument.
    spec_prompt_via_stdin: bool = Field(default=False)
    # Run vibe tasks under a pseudo-terminal. Set true for kiro-cli v3 (via
    # kiro-cli-plus) which doesn't exit cleanly in headless mode: under a PTY
    # the master-close signals EOF to the child and the process exits naturally.
    vibe_use_pty: bool = Field(default=False)

    # ---- kas-proxy integration (model-compare via OpenRouter open-weight models) ----
    # When true, Kiro targets are auto-configured with cost_source=kas_proxy_metrics,
    # which reads per-turn cost/credits from kas-proxy's metrics.jsonl correlated
    # by an X-Kas-Run-Id header (set by the harness as the KAS_RUN_ID env var on
    # each subprocess). Lets a single run capture cost for both Kiro-passthrough
    # and OpenRouter-routed turns through the same authoritative source.
    kas_proxy_metrics: bool = Field(default=False)
    kas_proxy_metrics_file: str | None = Field(
        default=None,
        description="Path to metrics.jsonl. None = ~/.kas-proxy/metrics.jsonl",
    )
    kas_proxy_metrics_timeout_seconds: float = Field(
        default=5.0, ge=0.0,
        description="How long to wait for the proxy to write the per-turn record.",
    )

    # ---- Task discovery ----
    tasks_dir: str = Field(default="tasks")
    task_ids: list[str] | None = Field(default=None)
    modes: list[TaskMode] | None = Field(default=None)

    # ---- Execution ----
    # Concurrency strategy. Controls how many task × target jobs run at once.
    #   "per_target" (default) — one concurrent job per enabled CLI/model. Safe
    #                            for laptops and external-API rate limits.
    #   "full"                 — targets × tasks jobs all at once. Best for CI
    #                            with fast machines and high API quotas.
    #   N (positive int)       — explicit cap regardless of targets/tasks.
    concurrency: str | int = Field(
        default="per_target",
        description='"per_target" | "full" | explicit int cap',
    )
    # Hard upper bound applied on top of the concurrency strategy. Useful when
    # using "full" but you still want to cap API parallelism (e.g. max_concurrency: 6).
    max_concurrency: int | None = Field(
        default=None, ge=1,
        description="Hard cap applied after the concurrency strategy",
    )
    # Kept for programmatic use in tests; YAML users should prefer `concurrency`.
    parallel_workers: int = Field(default=0, ge=0, exclude=True)
    timeout_minutes: int = Field(default=15)
    task_timeout_minutes: int | None = Field(default=None)
    repeats: int = Field(default=1, ge=1)
    transient_retries: int = Field(default=2, ge=0)
    functional_pass_threshold: float = Field(default=0.99, ge=0.0, le=1.0)
    pass_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    workspace_base: str = Field(default="/tmp/agent_cost_bench")

    # ---- Reporting ----
    output_dir: str = Field(default="results")
    report_title: str = Field(default="agent_cost_bench results")
    open_report: bool = Field(default=True)

    # ---- LLM-as-judge (model-compare) ----
    judge_model: str | None = Field(default=None)
    judge_cli_path: str | None = Field(default=None)
    judge_api_key: str | None = Field(default=None)
    judge_weight: float = Field(default=0.6, ge=0.0, le=1.0)

    @field_validator("concurrency", mode="before")
    @classmethod
    def _validate_concurrency(cls, v: object) -> str | int:
        if isinstance(v, int):
            if v > 0:
                return v
            raise ValueError("concurrency as an integer must be positive")
        if isinstance(v, str):
            if v in ("per_target", "full"):
                return v
            try:
                n = int(v)
                if n > 0:
                    return n
            except ValueError:
                pass
        raise ValueError(
            f"concurrency must be 'per_target', 'full', or a positive integer; got {v!r}"
        )

    @field_validator("targets")
    @classmethod
    def targets_not_empty(cls, v: list[Target]) -> list[Target]:
        if not v:
            raise ValueError("At least one target must be specified")
        if not [t for t in v if t.enabled]:
            raise ValueError("At least one enabled target must be specified")
        return v

    @model_validator(mode="after")
    def _spec_only_in_model_compare(self) -> "BenchConfig":
        # cli-compare is vibe-only — reject a spec-driven mode filter there.
        if self.mode == CompareMode.CLI_COMPARE and self.modes:
            if any(m == TaskMode.SPEC_DRIVEN for m in self.modes):
                raise ValueError("cli-compare does not support spec-driven tasks")
        return self

    def enabled_targets(self) -> list[Target]:
        return [t for t in self.targets if t.enabled]

    def effective_workers(self, n_tasks: int = 1) -> int:
        """Resolve concurrency.

        Strategy:
          ``per_target`` — one concurrent job per enabled target (default, conservative).
          ``full``       — targets × tasks, all jobs at once (opt-in for CI/fast machines).
          int            — explicit cap regardless of matrix size.

        ``parallel_workers`` (legacy programmatic override) takes priority when set.
        ``max_concurrency`` applies as a hard cap after the strategy is resolved.
        """
        # Legacy programmatic override (used in tests).
        if self.parallel_workers and self.parallel_workers > 0:
            return self.parallel_workers

        n_targets = max(1, len(self.enabled_targets()))
        if isinstance(self.concurrency, int):
            workers = self.concurrency
        elif self.concurrency == "full":
            workers = n_targets * max(1, n_tasks)
        else:  # "per_target" default
            workers = n_targets

        if self.max_concurrency and self.max_concurrency > 0:
            workers = min(workers, self.max_concurrency)
        return max(1, workers)

    # Backwards-friendly accessors used in a few report/runner spots.
    @property
    def is_cli_compare(self) -> bool:
        return self.mode == CompareMode.CLI_COMPARE


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


@dataclass
class FunctionalTestResult:
    passed: bool = False
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    # Graduated functional score (0.0–1.0). Populated from a structured result
    # marker when present, else binary 1.0/0.0 from the verify exit code.
    score: float = 0.0
    checkpoints: dict[str, Any] = field(default_factory=dict)
    summary: str = ""


@dataclass
class SpecArtifactScores:
    requirements_score: float = 0.0
    design_score: float = 0.0
    tasks_score: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def overall(self) -> float:
        scores = [self.requirements_score, self.design_score, self.tasks_score]
        return sum(scores) / len(scores)


@dataclass
class PhaseResult:
    """Result of a single execution phase (vibe, or a spec phase)."""

    phase: str
    success: bool
    duration_seconds: float
    stdout: str = ""
    stderr: str = ""
    error: str | None = None

    # Usage parsed from the CLI output (both USD and native units)
    credits: float | None = None          # native Kiro credits
    cost_usd: float | None = None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None          # tokens from cache (Codex)
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None      # Codex o-series reasoning tokens
    premium_requests: float | None = None
    cli_reported_seconds: float | None = None

    transient_retries: int = 0

    @property
    def model_unavailable(self) -> bool:
        combined = f"{self.stdout}\n{self.stderr}".lower()
        return (
            "temporarily unavailable" in combined
            or "model you've selected is" in combined
            or "please relaunch with" in combined
        )

    @property
    def timed_out(self) -> bool:
        """True when this phase failed because the CLI call exceeded its timeout."""
        return (not self.success) and bool(self.error) and "timed out" in self.error.lower()

    @property
    def transient_error(self) -> bool:
        if self.success:
            return False
        combined = f"{self.stdout}\n{self.stderr}".lower()
        signatures = (
            "having trouble responding",
            "failed to receive the next message",
            "failed to send the request",
            "dispatch failure",
            "error sending request for url",
            "tool approval required but --no-interactive",
        )
        return any(s in combined for s in signatures)


@dataclass
class RunResult:
    """
    Result of a single task × target × repeat execution. A superset usable by
    both modes: cli-compare populates cost/usage + functional only; model-compare
    additionally populates the four quality dimensions and per-phase results.
    """

    task_id: str
    target: str               # target label
    mode: TaskMode
    status: TaskStatus
    repeat: int = 1

    transient_retries: int = 0

    started_at: datetime = field(default_factory=_utcnow)
    finished_at: datetime | None = None

    # Scores (0.0–1.0)
    functional_score: float = 0.0
    spec_artifact_score: float = 0.0
    task_completion_rate: float = 0.0
    steering_adherence_score: float = 0.0
    final_score: float = 0.0

    # Cost / usage — ALWAYS recorded both as USD and native units.
    cost_usd: float | None = None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None          # tokens from cache (Codex)
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None      # Codex o-series reasoning tokens
    cli_reported_seconds: float = 0.0
    raw_credits: float | None = None
    premium_requests: float | None = None
    # Aggregated native credits for model-compare (sum across phases).
    total_credits: float = 0.0

    # Sub-results
    functional_result: FunctionalTestResult | None = None
    spec_artifact_scores: SpecArtifactScores | None = None
    phase_results: list[PhaseResult] = field(default_factory=list)

    # Raw output
    agent_stdout: str = ""
    agent_stderr: str = ""
    error_message: str | None = None
    workspace_path: str | None = None

    @property
    def duration_seconds(self) -> float:
        if self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return 0.0

    @property
    def native_credits(self) -> float | None:
        """Unified native cost unit: Kiro credits, else Copilot premium requests."""
        if self.raw_credits is not None:
            return self.raw_credits
        if self.premium_requests is not None:
            return self.premium_requests
        if self.total_credits:
            return self.total_credits
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "target": self.target,
            "repeat": self.repeat,
            "mode": self.mode.value,
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_seconds": self.duration_seconds,
            "scores": {
                "functional": self.functional_score,
                "spec_artifact": self.spec_artifact_score,
                "task_completion": self.task_completion_rate,
                "steering_adherence": self.steering_adherence_score,
                "final": self.final_score,
            },
            "usage": {
                "cost_usd": self.cost_usd,
                "input_tokens": self.input_tokens,
                "cached_input_tokens": self.cached_input_tokens,
                "output_tokens": self.output_tokens,
                "reasoning_output_tokens": self.reasoning_output_tokens,
                "cli_reported_seconds": self.cli_reported_seconds,
                "wall_clock_seconds": self.duration_seconds,
                "raw_credits": self.raw_credits,
                "premium_requests": self.premium_requests,
                "total_credits": self.total_credits,
                "native_credits": self.native_credits,
                "cost_per_pass": (
                    self.cost_usd if self.status == TaskStatus.PASSED else None
                ),
                "credits_per_pass": (
                    self.total_credits if self.status == TaskStatus.PASSED else None
                ),
            },
            "functional_result": {
                "passed": self.functional_result.passed,
                "exit_code": self.functional_result.exit_code,
                "score": self.functional_result.score,
                "checkpoints": self.functional_result.checkpoints,
                "summary": self.functional_result.summary,
                "stdout": self.functional_result.stdout,
                "stderr": self.functional_result.stderr,
            }
            if self.functional_result
            else None,
            "spec_artifact_scores": {
                "requirements": self.spec_artifact_scores.requirements_score,
                "design": self.spec_artifact_scores.design_score,
                "tasks": self.spec_artifact_scores.tasks_score,
                "overall": self.spec_artifact_scores.overall,
                "details": self.spec_artifact_scores.details,
            }
            if self.spec_artifact_scores
            else None,
            "phase_results": [
                {
                    "phase": p.phase,
                    "success": p.success,
                    "duration_seconds": p.duration_seconds,
                    "credits": p.credits,
                    "cli_reported_seconds": p.cli_reported_seconds,
                    "error": p.error,
                    "transient_retries": p.transient_retries,
                }
                for p in self.phase_results
            ],
            "transcript": {
                "stdout": _clip_transcript(self.agent_stdout),
                "stderr": _clip_transcript(self.agent_stderr),
            },
            "error_message": self.error_message,
            "workspace_path": self.workspace_path,
        }


@dataclass
class BenchmarkRun:
    """Aggregated results for a full benchmark run (either mode)."""

    run_id: str
    config: BenchConfig
    started_at: datetime = field(default_factory=_utcnow)
    finished_at: datetime | None = None
    results: list[RunResult] = field(default_factory=list)

    # ---- timing / counts ----
    @property
    def duration_seconds(self) -> float:
        if self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return 0.0

    @property
    def total_runs(self) -> int:
        return len(self.results)

    @property
    def passed_runs(self) -> int:
        return sum(1 for r in self.results if r.status == TaskStatus.PASSED)

    @property
    def failed_runs(self) -> int:
        return sum(1 for r in self.results if r.status in (TaskStatus.FAILED, TaskStatus.ERROR))

    @property
    def unavailable_runs(self) -> int:
        return sum(1 for r in self.results if r.status == TaskStatus.UNAVAILABLE)

    @property
    def pass_rate(self) -> float:
        eligible = [r for r in self.results if r.status != TaskStatus.UNAVAILABLE]
        if not eligible:
            return 0.0
        return sum(1 for r in eligible if r.status == TaskStatus.PASSED) / len(eligible)

    @property
    def total_cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.results if r.cost_usd is not None)

    @property
    def total_credits(self) -> float:
        return sum(r.total_credits for r in self.results)

    @property
    def repeats(self) -> int:
        return getattr(self.config, "repeats", 1)

    # ---- groupings ----
    def results_by_target(self) -> dict[str, list[RunResult]]:
        out: dict[str, list[RunResult]] = {}
        for r in self.results:
            out.setdefault(r.target, []).append(r)
        return out

    def results_by_mode(self) -> dict[str, list[RunResult]]:
        out: dict[str, list[RunResult]] = {}
        for r in self.results:
            out.setdefault(r.mode.value, []).append(r)
        return out

    def avg_score_by_target(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for target, results in self.results_by_target().items():
            eligible = [r for r in results if r.status != TaskStatus.UNAVAILABLE]
            out[target] = (
                sum(r.final_score for r in eligible) / len(eligible) if eligible else 0.0
            )
        return out

    # ---- cost / efficiency stats (USD + native), keyed by target ----
    def cost_stats_by_target(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for target, all_results in self.results_by_target().items():
            results = [r for r in all_results if r.status != TaskStatus.UNAVAILABLE]
            n = len(results)
            passed = [r for r in results if r.status == TaskStatus.PASSED]
            costed = [r for r in results if r.cost_usd is not None]
            total_cost = sum(r.cost_usd for r in costed)
            passed_cost = sum(r.cost_usd for r in passed if r.cost_usd is not None)
            avg_cost = total_cost / len(costed) if costed else 0.0
            latencies = [
                r.cli_reported_seconds if r.cli_reported_seconds else r.duration_seconds
                for r in results
            ]
            avg_latency = sum(latencies) / n if n else 0.0
            total_in = sum(r.input_tokens for r in results if r.input_tokens is not None)
            total_out = sum(r.output_tokens for r in results if r.output_tokens is not None)

            # Native credits: prefer per-run native_credits (Kiro/Copilot), else
            # the model-compare aggregated total_credits.
            credit_vals = []
            for r in results:
                c = r.native_credits
                if c is not None:
                    credit_vals.append(c)
            total_credits = sum(credit_vals)
            passed_credits = sum(
                (r.native_credits or 0.0) for r in passed if r.native_credits is not None
            )
            avg_credits = total_credits / len(credit_vals) if credit_vals else 0.0

            out[target] = {
                "runs": float(n),
                "passed": float(len(passed)),
                "pass_rate": len(passed) / n if n else 0.0,
                "avg_cost_usd": avg_cost,
                "total_cost_usd": total_cost,
                "cost_per_pass": (passed_cost / len(passed)) if passed else float("inf"),
                "avg_latency_seconds": avg_latency,
                "total_input_tokens": float(total_in),
                "total_output_tokens": float(total_out),
                "avg_credits": avg_credits,
                "total_credits": total_credits,
                "credits_per_pass": (passed_credits / len(passed)) if passed else float("inf"),
                "has_credits": 1.0 if credit_vals else 0.0,
                "has_cost": 1.0 if costed else 0.0,
            }
        return out

    # ---- repeat-aware pass stats ----
    def _instance_outcomes_by_target(self) -> dict[str, list[list[bool]]]:
        grouped: dict[str, dict[str, list[bool]]] = {}
        for r in self.results:
            if r.status == TaskStatus.UNAVAILABLE:
                continue
            passed = r.status == TaskStatus.PASSED
            grouped.setdefault(r.target, {}).setdefault(r.task_id, []).append(passed)
        return {target: list(by_task.values()) for target, by_task in grouped.items()}

    def repeat_stats_by_target(self) -> dict[str, "RepeatStats"]:
        from .stats import compute_repeat_stats

        return {
            target: compute_repeat_stats(outcomes)
            for target, outcomes in self._instance_outcomes_by_target().items()
        }
