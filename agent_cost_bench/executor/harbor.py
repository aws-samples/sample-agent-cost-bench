"""Delegate one benchmark attempt to Harbor's trial SDK."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from ..models import (
    BenchConfig,
    FunctionalTestResult,
    HarborExecutionConfig,
    PhaseResult,
    Pricing,
    RunResult,
    Target,
    TaskConfig,
    TaskStatus,
    Usage,
)
from ..usage import (
    parse_antigravity_usage,
    parse_codex_usage,
    parse_copilot_usage,
    parse_cursor_usage,
    parse_devin_export_usage,
    parse_pi_usage,
)

if TYPE_CHECKING:
    from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TrialConfig
    from harbor.models.trial.result import TimingInfo, TrialResult

HARBOR_VERSION = "0.27.0"
_AGENT_LOGS = {
    "antigravity-cli": "antigravity-stream.jsonl",
    "claude-code": "claude-code.txt",
    "codex": "codex.txt",
    "copilot-cli": "copilot-cli.txt",
    "cursor-cli": "cursor-cli.txt",
    "devin": "trajectory.json",
    "opencode": "opencode.txt",
    "opencode-v2": "opencode-v2.txt",
    "pi": "pi.txt",
}


def require_harbor() -> str:
    message = (
        "Harbor requires Python 3.12+ and the project extra: "
        f"uv sync --extra harbor (Harbor {HARBOR_VERSION})"
    )
    if sys.version_info < (3, 12):
        raise ValueError(message)
    try:
        installed = version("harbor")
    except PackageNotFoundError:
        raise ValueError(message) from None
    if installed != HARBOR_VERSION:
        raise ValueError(f"Found Harbor {installed}; {message}")
    return installed


def build_environment_config(settings: HarborExecutionConfig) -> EnvironmentConfig:
    from harbor.models.environment_type import EnvironmentType
    from harbor.models.trial.config import EnvironmentConfig

    if ":" in settings.env:
        selector = {"import_path": settings.env}
    else:
        try:
            selector = {"type": EnvironmentType(settings.env)}
        except ValueError:
            raise ValueError(
                f"Unknown Harbor sandbox {settings.env!r}; see `harbor environment list`. "
                "Custom sandboxes use module.path:ClassName."
            ) from None

    supported = EnvironmentConfig.model_fields.keys() - {"type", "import_path", "kwargs"}
    if unknown := settings.options.keys() - supported:
        raise ValueError(f"Unknown execution.harbor.options keys: {', '.join(sorted(unknown))}")
    try:
        return EnvironmentConfig.model_validate({
            **selector, **settings.options, "kwargs": settings.kwargs,
        })
    except ValidationError as exc:
        fields = ", ".join(
            ".".join(map(str, error["loc"])) or "options"
            for error in exc.errors(include_input=False)
        )
        raise ValueError(f"Invalid Harbor environment configuration: {fields}") from None


def _check_environment(settings: HarborExecutionConfig, *, runtime: bool) -> str:
    installed = require_harbor()
    from harbor.environments.factory import EnvironmentFactory

    environment = build_environment_config(settings)
    try:
        EnvironmentFactory.validate_resource_policies(environment)
        if runtime and environment.type != "docker":
            EnvironmentFactory.run_preflight(
                environment.type, import_path=environment.import_path, kwargs=environment.kwargs,
            )
        else:
            EnvironmentFactory.validate_options(environment)
    except SystemExit as exc:
        raise ValueError(f"Harbor sandbox {settings.env!r}: {exc}") from None
    except ImportError as exc:
        extra = getattr(exc, "extra", "<provider-extra>")
        install_hint = (
            "Sync the EC2 dependencies with `uv sync --locked --extra harbor`."
            if environment.type == "ec2"
            else f"If needed, install its dependencies with `uv run --extra harbor "
            f"--with 'harbor[{extra}]=={HARBOR_VERSION}' agent-cost-bench …`."
        )
        raise ValueError(
            f"Could not import Harbor sandbox {settings.env!r}: {exc}\n{install_hint}"
        ) from None
    return installed


def validate_environment(settings: HarborExecutionConfig) -> str:
    return _check_environment(settings, runtime=False)


def preflight(settings: HarborExecutionConfig) -> str:
    installed = _check_environment(settings, runtime=True)
    if settings.env != "docker":
        return installed
    docker = shutil.which("docker")
    if docker is None:
        raise ValueError(
            "Harbor needs Docker CLI with Compose v2 on PATH; "
            "Finch is not supported by this Harbor backend"
        )
    for args, requirement in (
        (["compose", "version"], "Compose v2"),
        (["info"], "a running Docker daemon"),
    ):
        try:
            subprocess.run([docker, *args], capture_output=True, check=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError(f"Harbor needs {requirement}") from exc
    return installed


def validate_task(task: TaskConfig) -> None:
    """Validate a native task and populate its description from Harbor metadata."""
    from harbor.models.task.task import Task

    assert task.task_dir is not None
    try:
        native_task = Task(task.task_dir)
    except (OSError, ValueError) as exc:
        raise ValueError(f"Invalid Harbor task '{task.id}': {exc}") from exc
    if native_task.has_steps:
        raise ValueError(f"Harbor task '{task.id}': multi-step tasks are not supported yet")
    package = native_task.config.task
    task.description = package.description if package is not None else ""


def _build_agent_config(
    config: BenchConfig,
    task: TaskConfig,
    target: Target,
) -> AgentConfig:
    from harbor.models.trial.config import AgentConfig

    settings = target.harbor
    effort = (settings.effort if settings else None) or task.effort
    if effort is None and "effort" in config.model_fields_set:
        effort = config.effort
    timeout_minutes = config.task_timeout_minutes
    if timeout_minutes is None:
        timeout_minutes = task.harbor_timeout_minutes
    if timeout_minutes is None and "timeout_minutes" in config.model_fields_set:
        timeout_minutes = config.timeout_minutes
    kwargs = dict(settings.kwargs) if settings else {}
    if effort:
        kwargs["reasoning_effort"] = effort
    return AgentConfig(
        name=target.harbor_agent(),
        model_name=target.model_id,
        env=target.env,
        base_url=settings.base_url if settings else None,
        model_api_key_name=settings.model_api_key_name if settings else None,
        skip_model_parsing=settings.skip_model_parsing if settings else False,
        kwargs=kwargs,
        override_timeout_sec=timeout_minutes * 60 if timeout_minutes is not None else None,
    )


def build_trial_config(
    config: BenchConfig,
    task: TaskConfig,
    target: Target,
    output_dir: Path,
) -> TrialConfig:
    from harbor.models.trial.config import TrialConfig
    from harbor.models.trial.config import TaskConfig as HarborTaskConfig

    assert task.task_dir is not None
    return TrialConfig(
        task=HarborTaskConfig(path=task.task_dir, source=task.harbor_source),
        trials_dir=output_dir,
        agent=_build_agent_config(config, task, target),
        environment=build_environment_config(config.execution.harbor),
    )


def preflight_agents(config: BenchConfig, task: TaskConfig) -> None:
    """Check selected agents before any trial can provision a sandbox."""
    from harbor.agents.factory import AgentFactory

    for target in config.enabled_targets():
        try:
            AgentFactory.run_preflight(_build_agent_config(config, task, target))
        except (Exception, SystemExit) as exc:
            # Provider exceptions may include resolved credentials or options.
            raise ValueError(
                f"Runner '{target.name}': Harbor agent preflight failed ({type(exc).__name__}); "
                "check its model, endpoint, settings and credential environment"
            ) from None


async def run_trial(
    config: BenchConfig,
    task: TaskConfig,
    target: Target,
    result: RunResult,
    output_dir: Path,
    harbor_version: str,
) -> None:
    result.cost_source = "unknown"
    result.execution_details = {
        "backend": "harbor",
        "harbor_version": harbor_version,
        "sandbox": config.execution.harbor.env,
        "task_source": task.harbor_source,
        "task_path": str(task.task_dir),
        "agent": target.harbor_agent(),
        "model": target.model_id,
    }
    try:
        from harbor.trial.trial import Trial

        trial_config = build_trial_config(config, task, target, output_dir)
        trial_dir = output_dir / trial_config.trial_name
        trial_dir.mkdir(parents=True, mode=0o700)
        result.execution_details["trial_dir"] = str(trial_dir)
        trial = await Trial.create(trial_config)
        apply_trial_result(result, task, target, await trial.run(), trial_dir)
    except Exception as exc:
        # Raw SDK errors can contain resolved credentials. Don't echo them into
        # our log/reports; Harbor maintains its own scrubbed trial artifacts.
        result.status = TaskStatus.UNAVAILABLE
        result.error_message = (
            f"Harbor could not complete the trial ({type(exc).__name__}); see trial artifacts"
        )


def _read_artifact(root: Path, relative: str, limit: int = 4 * 1024 * 1024) -> str:
    """Read only regular files inside the trial dir, not agent-created symlinks."""
    try:
        path = root / relative
        if path.resolve() != path or not path.is_file():
            return ""
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        return data.decode("utf-8", errors="replace") if len(data) <= limit else ""
    except (OSError, RuntimeError):
        return ""


def _objects(text: str):
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(obj, dict):
            yield obj


def _number(value) -> float | None:
    if type(value) in (int, float) and 0 <= value < math.inf:
        return float(value)
    return None


def _tokens(value) -> int | None:
    return value if type(value) is int and 0 <= value < 2**63 else None


def _sum_known(values: list[int | None]) -> int | None:
    return sum(v for v in values if v is not None) if all(v is not None for v in values) else None


def _copilot_snapshot(raw: str) -> Usage:
    """Read Copilot's cumulative usage file, never the controller's session state."""
    try:
        snapshot = json.loads(raw)
    except (ValueError, RecursionError):
        return Usage()
    models = snapshot.get("modelMetrics") if isinstance(snapshot, dict) else None
    if not isinstance(models, dict) or not models:
        return Usage()
    if not all(isinstance(row, dict) for row in models.values()):
        return Usage()
    rows = list(models.values())
    priced = snapshot.get("aiCreditsStatus") not in ("partial", "unavailable") and all(
        row.get("aiCreditsStatus") not in ("partial", "unavailable") for row in rows
    )
    nano = [_number(row.get("totalNanoAiu")) for row in rows]
    credits = (
        sum(value for value in nano if value is not None) / 1e9
        if priced and None not in nano else None
    )

    def total(key: str) -> int | None:
        usage = [row.get("usage") for row in rows]
        return _sum_known([
            _tokens(item.get(key)) if isinstance(item, dict) else None for item in usage
        ])

    return Usage(
        cost_usd=credits * 0.01 if credits is not None else None,
        raw_credits=credits,
        input_tokens=total("inputTokens"),
        cached_input_tokens=total("cacheReadTokens"),
        cache_write_input_tokens=total("cacheWriteTokens"),
        output_tokens=total("outputTokens"),
        reasoning_output_tokens=total("reasoningTokens"),
    )


def _apply_reused_usage(
    result: RunResult, target: Target, raw: str, trial: TrialResult, trial_dir: Path,
) -> None:
    agent = target.harbor_agent()
    pricing = target.pricing
    cost_source = "token_pricing"
    if agent == "codex":
        usage = parse_codex_usage(raw, "", pricing)
    elif agent == "cursor-cli":
        usage = parse_cursor_usage(raw, "", pricing)
    elif agent == "pi":
        usage = parse_pi_usage(raw, "", Pricing())
        if usage.cost_usd is not None:
            cost_source = "cli_native"
        else:
            usage = parse_pi_usage(raw, "", pricing)
    elif agent == "copilot-cli":
        cost_source = "cli_native"
        snapshot = _read_artifact(trial_dir, "agent/copilot-usage.json")
        if snapshot:
            usage = _copilot_snapshot(snapshot)
        else:
            usage = parse_copilot_usage(raw, "", pricing, read_session_state=False)
    elif agent == "devin":
        usage = parse_devin_export_usage(raw, pricing)
    elif agent == "antigravity-cli":
        # Harbor uses stream-json, while the host parser reads the final JSON payload.
        final = next((
            event["result"] for event in reversed(list(_objects(raw)))
            if event.get("event") == "result" and isinstance(event.get("result"), dict)
            and isinstance(event["result"].get("usage"), dict)
        ), {})
        usage = parse_antigravity_usage(json.dumps(final), "", pricing)
    else:
        raise ValueError(f"No Harbor usage parser for {agent!r}")

    sdk_input, sdk_cache, sdk_output, _ = trial.compute_token_cost_totals()
    def tokens_or_sdk(value: int | None, fallback: int | None) -> int | None:
        parsed = _tokens(value)
        return fallback if parsed is None else parsed

    result.input_tokens = tokens_or_sdk(usage.input_tokens, sdk_input)
    result.cached_input_tokens = tokens_or_sdk(usage.cached_input_tokens, sdk_cache)
    result.output_tokens = tokens_or_sdk(usage.output_tokens, sdk_output)
    result.cache_write_input_tokens = _tokens(usage.cache_write_input_tokens)
    result.reasoning_output_tokens = _tokens(usage.reasoning_output_tokens)
    reported_seconds = _number(usage.seconds)
    if reported_seconds is not None:
        result.cli_reported_seconds = reported_seconds
    result.cost_usd = _number(usage.cost_usd)
    result.cost_source = cost_source if result.cost_usd is not None else "unknown"
    if agent == "copilot-cli":
        result.raw_credits = _number(usage.raw_credits)


def _price(row: tuple, pricing: Pricing) -> float | None:
    input_tokens, cache_read, cache_write, output_tokens, reasoning = row
    inp, out = pricing.usd_per_input_token, pricing.usd_per_output_token
    if inp is None or out is None or None in row:
        return None
    rates = (
        inp,
        inp if pricing.usd_per_cached_input_token is None else pricing.usd_per_cached_input_token,
        inp if pricing.usd_per_cache_write_token is None else pricing.usd_per_cache_write_token,
        out,
        out if pricing.usd_per_reasoning_token is None else pricing.usd_per_reasoning_token,
    )
    if any(_number(r) is None for r in rates):
        return None
    return _number(
        sum(
            n * r
            for n, r in zip(
                (input_tokens, cache_read, cache_write, output_tokens, reasoning),
                rates,
            )
        )
    )


def _apply_usage(result: RunResult, target: Target, raw: str, trial: TrialResult) -> None:
    def mapping(value):
        return value if isinstance(value, dict) else {}

    rows = []
    costs = []
    if target.harbor_agent() == "claude-code":
        event = next((e for e in reversed(list(_objects(raw))) if e.get("type") == "result"), {})
        if event:
            usage = mapping(event.get("usage"))
            rows.append(
                tuple(
                    _tokens(usage.get(key))
                    for key in (
                        "input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                        "output_tokens",
                    )
                )
                + (0,)
            )
            costs.append(_number(event.get("total_cost_usd")))
            duration = _number(event.get("duration_ms"))
            if duration is not None:
                result.cli_reported_seconds = duration / 1000
    else:
        for event in _objects(raw):
            if event.get("type") != "step_finish":
                continue
            part = mapping(event.get("part"))
            usage = mapping(part.get("tokens"))
            cache = mapping(usage.get("cache"))
            rows.append(
                (
                    _tokens(usage.get("input")),
                    _tokens(cache.get("read")),
                    _tokens(cache.get("write")),
                    _tokens(usage.get("output")),
                    _tokens(usage.get("reasoning")),
                )
            )
            # OpenCode can report zero as a placeholder when it has no rates.
            costs.append(_number(part.get("cost")) or None)

    sdk_input, sdk_cache, sdk_output, _ = trial.compute_token_cost_totals()
    if rows:
        buckets = [_sum_known(list(bucket)) for bucket in zip(*rows)]
        total_input = _sum_known(buckets[:3])
        cached, output, reasoning = buckets[1], buckets[3], buckets[4]
        result.input_tokens = sdk_input if total_input is None else total_input
        result.cached_input_tokens = sdk_cache if cached is None else cached
        result.cache_write_input_tokens = buckets[2]
        result.output_tokens = sdk_output if output is None else output
        result.reasoning_output_tokens = reasoning if target.harbor_agent() != "claude-code" else None
        if all(cost is not None for cost in costs):
            result.cost_usd = _number(sum(cost for cost in costs if cost is not None))
            if result.cost_usd is not None:
                result.cost_source = "cli_native"
        else:
            result.cost_usd = _price(tuple(buckets), target.pricing)
            if result.cost_usd is not None:
                result.cost_source = "token_pricing"
    else:
        result.input_tokens, result.cached_input_tokens, result.output_tokens = (
            sdk_input,
            sdk_cache,
            sdk_output,
        )
    # Harbor can estimate USD internally, and its aggregate context cannot
    # distinguish cache writes. Don't label those estimates as raw CLI cost.


def _seconds(timing: TimingInfo | None) -> float:
    if timing and timing.started_at and timing.finished_at:
        return (timing.finished_at - timing.started_at).total_seconds()
    return 0.0


def apply_trial_result(
    result: RunResult,
    task: TaskConfig,
    target: Target,
    trial: TrialResult,
    trial_dir: Path,
) -> None:
    assert result.execution_details is not None
    result.execution_details.update(
        {
            "task_checksum": trial.task_checksum,
            "agent_version": trial.agent_info.version,
            "verifier_mode": trial.verifier_environment_mode,
            "timings_seconds": {
                name: _seconds(timing)
                for name, timing in (
                    ("environment_setup", trial.environment_setup),
                    ("agent_setup", trial.agent_setup),
                    ("agent", trial.agent_execution),
                    ("verifier", trial.verifier),
                )
                if timing and timing.started_at and timing.finished_at
            },
        }
    )
    raw = _read_artifact(trial_dir, f"agent/{_AGENT_LOGS[target.harbor_agent()]}")
    # Raw agent output/trajectory is retained by Harbor, not duplicated in
    # our public report (it may contain task contents or credentials).
    if target.harbor_agent() in ("claude-code", "opencode", "opencode-v2"):
        _apply_usage(result, target, raw, trial)
    else:
        try:
            _apply_reused_usage(result, target, raw, trial, trial_dir)
        except (AttributeError, TypeError, ValueError, OverflowError, RecursionError):
            # Malformed or truncated CLI telemetry must not erase the verifier outcome.
            result.input_tokens, result.cached_input_tokens, result.output_tokens, _ = (
                trial.compute_token_cost_totals()
            )
    if _read_artifact(trial_dir, "verifier/ctrf.json"):
        result.execution_details["ctrf_path"] = str(trial_dir / "verifier/ctrf.json")
    exceptions = trial.recorded_exceptions()
    error_type = exceptions[0].exception_type if exceptions else None
    if trial.agent_execution is not None:
        result.phase_results.append(
            PhaseResult(
                phase="vibe",
                success=error_type is None,
                duration_seconds=_seconds(trial.agent_execution),
                error=error_type,
            )
        )
    if error_type:
        result.status = (
            TaskStatus.TIMEOUT if error_type == "AgentTimeoutError" else TaskStatus.UNAVAILABLE
        )
        result.error_message = f"Harbor trial reported {error_type}; see trial artifacts"
        return

    rewards = trial.verifier_result.rewards if trial.verifier_result else None
    score = None
    if trial.reward_valid and rewards:
        value = rewards.get("reward") if len(rewards) > 1 else next(iter(rewards.values()))
        score = _number(value)
    if score is None or score > 1:
        result.status = TaskStatus.UNAVAILABLE
        result.error_message = "Harbor verifier did not produce an unambiguous reward in [0, 1]"
        return

    passed = score >= task.functional_pass_threshold
    result.status = TaskStatus.PASSED if passed else TaskStatus.FAILED
    result.functional_score = result.final_score = score
    # Like agent logs, verifier transcripts can contain task data or credentials.
    # Keep them in the trial artifacts rather than duplicating them in reports.
    result.functional_result = FunctionalTestResult(
        passed=passed,
        score=score,
        duration_seconds=_seconds(trial.verifier),
        summary=f"Harbor reward: {score:g}",
    )
