"""Offline tests for the Harbor adapter; no container or model calls."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from shutil import copytree
from types import SimpleNamespace

import pytest
import yaml

from agent_cost_bench.config import (
    discover_tasks,
    load_cli_compare_config,
    load_model_compare_config,
)
from agent_cost_bench.executor import harbor
from agent_cost_bench.models import (
    BenchmarkRun,
    HarborExecutionConfig,
    Pricing,
    RunResult,
    TaskMode,
    TaskStatus,
)
from agent_cost_bench.preflight import check_targets_available
from agent_cost_bench.reporter.html import HTMLReporter
from agent_cost_bench.reporter.json_reporter import JSONReporter
from agent_cost_bench.runner import BenchmarkRunner

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "harbor" / "config.cli-compare.harbor-docker.example.yaml"
EC2_EXAMPLE = EXAMPLE.with_name("config.cli-compare.harbor-ec2.example.yaml")
FIXTURES = Path(__file__).parent / "fixtures/harbor"
AGENTS = ["claude-code", "opencode-v2", "codex", "copilot-cli", "cursor-cli",
          "antigravity-cli", "devin", "pi"]


def _write_config(tmp_path, modify=None):
    data = yaml.safe_load(EXAMPLE.read_text())
    data["task_sources"][0]["path"] = str(REPO_ROOT / data["task_sources"][0]["path"])
    data["workspace_base"] = str(tmp_path / "unused-workspace")
    data["output_dir"] = str(tmp_path / "results")
    if modify:
        modify(data)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_host_default(tmp_path):
    def modify(cfg):
        cfg.pop("task_sources")
        cfg.pop("execution")
        for runner in cfg["runners"]:
            runner.pop("harbor", None)

    assert load_cli_compare_config(_write_config(tmp_path, modify)).execution.backend == "host"


def test_host_rejects_harbor_sandbox_settings(tmp_path):
    def modify(cfg):
        cfg.pop("task_sources")
        cfg["execution"] = {"harbor": {"env": "ec2"}}
        for runner in cfg["runners"]:
            runner.pop("harbor", None)

    with pytest.raises(ValueError, match="execution.harbor requires execution.backend: harbor"):
        load_cli_compare_config(_write_config(tmp_path, modify))


def test_discovery_and_overrides(tmp_path):
    root = tmp_path / "dataset"
    for name in ("selected", "other"):
        task = root / name
        task.mkdir(parents=True)
        (task / "task.toml").touch()
    (root / "README.md").touch()

    def modify(cfg):
        cfg["task_sources"] = [{"type": "harbor", "path": str(root), "tasks": ["selected"]}]
        cfg["runners"][0].update(cli_path="unused-host-cli", harbor={"agent": "claude-code"})
        cfg["concurrency"] = "full"

    cfg = load_cli_compare_config(_write_config(tmp_path, modify))
    assert [t.harbor_agent() for t in cfg.targets] == AGENTS
    assert cfg.effective_workers(10) == 1
    assert check_targets_available(cfg) == []
    [task] = discover_tasks(cfg)
    assert task.id == "selected"
    assert task.task_dir == root / "selected"
    assert not Path(cfg.workspace_base).exists()
    cfg.task_sources[0].tasks = ["typo"]
    with pytest.raises(ValueError, match="Harbor tasks not found"):
        discover_tasks(cfg)


@pytest.mark.parametrize("metadata, expected", [
    ("", ""),
    ('\n[task]\nname = "bench/hello-file"\ndescription = "Write a greeting file."\n',
     "Write a greeting file."),
])
def test_native_task_description(tmp_path, metadata, expected):
    pytest.importorskip("harbor.models.task.task")
    cfg = load_cli_compare_config(_write_config(tmp_path))
    source = Path(cfg.task_sources[0].path)
    task_dir = copytree(source, tmp_path / "hello-file")
    toml = task_dir / "task.toml"
    original = toml.read_text() + metadata
    toml.write_text(original)
    cfg.task_sources[0].path = str(task_dir)

    [task] = discover_tasks(cfg)
    harbor.validate_task(task)

    assert task.description == expected
    assert toml.read_text() == original


@pytest.mark.parametrize(
    "modify, message",
    [
        (lambda c: c.pop("execution"), "requires execution.backend"),
        (lambda c: c.pop("task_sources"), "requires only type: harbor"),
        (lambda c: c["task_sources"][0].update(path="https://example.org/tasks"), "local paths"),
        (
            lambda c: c["task_sources"][0].update(dataset="org/tasks"),
            "Unsupported Harbor task source",
        ),
        (lambda c: c["runners"][0].update(cli_path="kiro"), "Harbor supports"),
        (lambda c: c["runners"][0].update(extra_args=["--foo"]), "owns the CLI invocation"),
        (lambda c: c["execution"]["harbor"].update(env=""), "at least 1 character"),
    ],
)
def test_invalid_config(tmp_path, modify, message):
    with pytest.raises(ValueError, match=message):
        load_cli_compare_config(_write_config(tmp_path, modify))


def test_model_compare_rejected(tmp_path):
    with pytest.raises(ValueError, match="only in cli-compare"):
        load_model_compare_config(_write_config(tmp_path, lambda c: c.update(models=["sonnet"])))


def test_finch_only_error(monkeypatch):
    monkeypatch.setattr(harbor, "_check_environment", lambda *a, **kw: harbor.HARBOR_VERSION)
    monkeypatch.setattr(harbor.shutil, "which", lambda _: None)
    with pytest.raises(ValueError, match="Finch is not supported"):
        harbor.preflight(HarborExecutionConfig())


EC2_KWARGS = {
    "region": "us-west-2",
    "ami_id": "ami-0123456789abcdef0",
    "use_public_ip": False,
    "subnet_id": "subnet-0123456789abcdef0",
    "security_group_ids": ["sg-0123456789abcdef0"],
    "tags": {"team": "benchmark"},
}


def test_docker_example_routes_four_agents_to_bedrock(tmp_path, monkeypatch):
    factory = pytest.importorskip("harbor.agents.factory").AgentFactory
    from harbor.agents.agent_auth_resolution import resolve_agent_model_connection

    credentials = {
        "AWS_ACCESS_KEY_ID": "offline-aws-key-id",
        "AWS_SECRET_ACCESS_KEY": "offline-aws-secret",
        "AWS_SESSION_TOKEN": "offline-aws-session-token",
    }
    for name, value in credentials.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-key")
    for name in (
        "AWS_PROFILE", "AWS_BEARER_TOKEN_BEDROCK", "OPENAI_BASE_URL", "OPENAI_API_BASE",
        "CODEX_AUTH_JSON_PATH", "CODEX_FORCE_AUTH_JSON",
    ):
        monkeypatch.delenv(name, raising=False)
    endpoint = "https://bedrock-runtime.us-west-2.amazonaws.com"
    model = "us.openai.gpt-6-sol"
    config = load_cli_compare_config(_write_config(tmp_path))
    [task] = discover_tasks(config)
    for target in config.targets:
        if target.name not in {"claude-code", "codex", "opencode", "pi"}:
            continue
        trial = harbor.build_trial_config(config, task, target, tmp_path)
        factory.run_preflight(trial.agent)
        agent = factory.create_agent_from_config(trial.agent, logs_dir=tmp_path)
        assert "AWS_BEARER_TOKEN_BEDROCK" not in agent.extra_env
        for name, value in credentials.items():
            assert agent.extra_env[name] == value
            assert value not in trial.model_dump_json()
        if target.name == "claude-code":
            auth = agent._resolve_auth_env()
            assert auth["CLAUDE_CODE_USE_BEDROCK"] == "1"
            assert auth["AWS_REGION"] == "us-west-2"
            assert {name: auth[name] for name in credentials} == credentials
            assert "AWS_BEARER_TOKEN_BEDROCK" not in auth
            assert agent._resolved_model_name() == "us.anthropic.claude-opus-5-5"
            continue

        connection = resolve_agent_model_connection(trial.agent)
        assert not connection.api_key
        if target.name == "codex":
            assert agent._resolved_model_name() == model
            assert not agent._resolved_base_url()
            effective = agent._build_effective_config(agent._resolved_base_url())
            assert effective == {
                "model_provider": "amazon-bedrock-runtime",
                "model_providers": {"amazon-bedrock-runtime": {"aws": {"region": "us-west-2"}}},
                "web_search": "disabled",
            }
            continue

        if target.name == "opencode":
            assert agent._execution_model_name() == f"docker-bedrock/{model}"
            provider = agent._native_config()["providers"]["docker-bedrock"]
            assert provider["package"] == "@opencode/ai/providers/amazon-bedrock"
            assert provider["settings"]["baseURL"] == endpoint
            assert provider["settings"]["region"] == "us-west-2"
            assert model in provider["models"]
        else:
            _, pi_model = agent._parse_model_name(trial.agent.model_name)
            assert pi_model == f"amazon-bedrock/{model}"
            assert agent._version == "1.6.0"
            assert agent.extra_env["AWS_REGION"] == "us-west-2"
            assert agent._build_custom_models_json(connection, pi_model) is None


def test_ec2_bedrock_example_routes_claude_code_to_instance_credentials(tmp_path, monkeypatch):
    factory = pytest.importorskip("harbor.agents.factory").AgentFactory
    credential_vars = (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_BEARER_TOKEN_BEDROCK",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_FORCE_OAUTH",
    )
    for name in credential_vars:
        monkeypatch.delenv(name, raising=False)
    config = load_cli_compare_config(EC2_EXAMPLE)
    [task] = discover_tasks(config)
    target = next(target for target in config.targets if target.name == "claude-code-bedrock")
    trial = harbor.build_trial_config(config, task, target, tmp_path)
    agent = factory.create_agent_from_config(trial.agent, logs_dir=tmp_path)
    auth = agent._resolve_auth_env()

    assert config.execution.harbor.env == "ec2"
    assert config.effective_workers(10) == config.repeats == 1
    assert auth["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert auth["AWS_REGION"] == config.execution.harbor.kwargs["region"]
    assert not any(auth.get(name) for name in credential_vars)
    assert agent._resolved_model_name() == "us.anthropic.claude-opus-5-5"
    assert {agent._resolved_model_name()} == {
        trial.agent.env[f"ANTHROPIC_DEFAULT_{tier}_MODEL"] for tier in ("HAIKU", "SONNET", "OPUS")
    }


def test_ec2_example_routes_gpt6_runners_to_worker_role(tmp_path, monkeypatch):
    factory = pytest.importorskip("harbor.agents.factory").AgentFactory
    for name in (
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
        "AWS_BEARER_TOKEN_BEDROCK", "OPENAI_API_KEY",
    ):
        monkeypatch.setenv(name, "launcher-only")
    for name in ("OPENAI_BASE_URL", "OPENAI_API_BASE", "CODEX_AUTH_JSON_PATH", "CODEX_FORCE_AUTH_JSON"):
        monkeypatch.delenv(name, raising=False)
    config = load_cli_compare_config(EC2_EXAMPLE)
    [task] = discover_tasks(config)
    targets = {target.name: target for target in config.targets}
    model = "us.openai.gpt-6-sol"
    region = config.execution.harbor.kwargs["region"]

    def create(name):
        trial = harbor.build_trial_config(config, task, targets[name], tmp_path)
        factory.run_preflight(trial.agent)
        agent = factory.create_agent_from_config(trial.agent, logs_dir=tmp_path)
        assert "launcher-only" not in repr((agent.extra_env, agent.model_connection.env))
        return agent

    codex = create("codex-bedrock")
    assert codex._resolved_model_name() == model
    assert codex._build_effective_config(codex._resolved_base_url()) == {
        "model_provider": "amazon-bedrock-runtime",
        "model_providers": {"amazon-bedrock-runtime": {"aws": {"region": region}}},
        "web_search": "disabled",
    }
    opencode = create("opencode-bedrock")
    assert opencode._execution_model_name() == f"ec2-bedrock/{model}"
    native = opencode._native_config()["providers"]["ec2-bedrock"]
    assert native["package"] == "@opencode/ai/providers/amazon-bedrock"
    assert native["settings"] == {
        "region": region, "baseURL": f"https://bedrock-runtime.{region}.amazonaws.com",
        "profile": "default", "apiKey": "{env:OPENAI_API_KEY}",
    }
    assert opencode.extra_env["OPENAI_API_KEY"] == ""
    assert model in native["models"]
    pi = create("pi-bedrock")
    assert pi._parse_model_name(pi.model_name)[1] == f"amazon-bedrock/{model}"
    assert pi._version == "1.6.0"
    assert pi.model_connection.execution_base_url is None
    assert pi.extra_env["AWS_REGION"] == region
    assert pi.extra_env["AWS_PROFILE"] == "default"


def test_ec2_preflight_does_not_require_local_docker(monkeypatch):
    ec2 = pytest.importorskip("harbor.environments.ec2")
    monkeypatch.setattr(ec2, "_HAS_BOTO3", True)

    def only_ssh(command):
        assert command == "ssh"
        return "/usr/bin/ssh"

    monkeypatch.setattr(harbor.shutil, "which", only_ssh)
    settings = HarborExecutionConfig(env="ec2", kwargs=EC2_KWARGS)
    assert harbor.validate_environment(settings) == harbor.HARBOR_VERSION
    assert harbor.preflight(settings) == harbor.HARBOR_VERSION

    monkeypatch.setattr(ec2, "_HAS_BOTO3", False)
    with pytest.raises(ValueError, match="uv sync --locked --extra harbor"):
        harbor.preflight(settings)


def test_sandbox_validation_and_custom_import():
    pytest.importorskip("harbor.models.trial.config")
    with pytest.raises(ValueError, match="Unknown Harbor sandbox"):
        harbor.validate_environment(HarborExecutionConfig(env="typo"))
    with pytest.raises(ValueError, match="Invalid kwargs.*ec2"):
        harbor.validate_environment(HarborExecutionConfig(env="ec2"))
    with pytest.raises(ValueError, match="Unknown execution.harbor.options keys: type"):
        harbor.build_environment_config(HarborExecutionConfig(options={"type": "ec2"}))

    env = "harbor.environments.ec2:EC2Environment"
    settings = HarborExecutionConfig(env=env, kwargs=EC2_KWARGS)
    assert harbor.validate_environment(settings) == harbor.HARBOR_VERSION
    assert harbor.build_environment_config(settings).import_path == env


def test_sandbox_options_are_not_in_benchmark_report(tmp_path):
    def modify(cfg):
        cfg["execution"]["harbor"].update(
            env="ec2", kwargs={"token": "private"}, options={"env": {"KEY": "secret"}},
        )

    cfg = load_cli_compare_config(_write_config(tmp_path, modify))
    run = BenchmarkRun(run_id="test", config=cfg, results=[])
    report = json.loads(JSONReporter(tmp_path).write(run).read_text())
    assert report["config"]["execution"]["harbor"] == {"env": "ec2", "n_concurrent": 1}


def _trial_result(rewards=None, exception=None):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        task_checksum="sha256:test",
        agent_info=SimpleNamespace(version="1.0"),
        verifier_environment_mode="separate",
        environment_setup=None,
        agent_setup=None,
        agent_execution=SimpleNamespace(started_at=now, finished_at=now + timedelta(seconds=5)),
        verifier=None,
        reward_valid=rewards is not None,
        verifier_result=SimpleNamespace(rewards=rewards),
        compute_token_cost_totals=lambda: (None, None, None, 0.99),
        recorded_exceptions=lambda: (
            [SimpleNamespace(exception_type=exception)] if exception else []
        ),
    )


@pytest.mark.parametrize(
    "reward, exception, status",
    [
        ({"reward": 0}, None, TaskStatus.FAILED),
        ({"reward": 1}, None, TaskStatus.PASSED),
        ({"reward": 1}, "AgentTimeoutError", TaskStatus.TIMEOUT),
        ({"reward": 1}, "VerifierTimeoutError", TaskStatus.UNAVAILABLE),
        (None, "AgentSetupError", TaskStatus.UNAVAILABLE),
        (None, None, TaskStatus.UNAVAILABLE),
        ({"reward": 1.1}, None, TaskStatus.UNAVAILABLE),
        ({"x": 0, "y": 1}, None, TaskStatus.UNAVAILABLE),
    ],
)
def test_result_mapping(tmp_path, reward, exception, status):
    cfg = load_cli_compare_config(_write_config(tmp_path))
    [task] = discover_tasks(cfg)
    target = cfg.targets[0]
    result = RunResult(
        task.id,
        target.name,
        TaskMode.VIBE,
        TaskStatus.RUNNING,
        execution_details={"backend": "harbor"},
        cost_source="unknown",
    )
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent/claude-code.txt").write_text((FIXTURES / "claude-code.txt").read_text())
    (tmp_path / "verifier").mkdir()
    (tmp_path / "verifier/test-stdout.txt").write_text("verifier output")
    (tmp_path / "verifier/test-stderr.txt").write_text("verifier error output")
    (tmp_path / "verifier/ctrf.json").write_text("{}")
    harbor.apply_trial_result(result, task, target, _trial_result(reward, exception), tmp_path)
    assert result.status is status
    assert result.cost_usd == 0.012
    assert result.input_tokens == 115
    assert result.cached_input_tokens == 100
    assert result.cli_reported_seconds == 1.2
    assert result.cost_source == "cli_native"
    assert result.execution_details["timings_seconds"]["agent"] == 5
    assert result.latency_seconds == 5
    assert result.execution_details["ctrf_path"].endswith("verifier/ctrf.json")
    if status in (TaskStatus.PASSED, TaskStatus.FAILED):
        assert result.functional_result.stdout == result.functional_result.stderr == ""
        run = BenchmarkRun(run_id="verifier", config=cfg, results=[result])
        for reporter in (JSONReporter(tmp_path), HTMLReporter(tmp_path)):
            report = reporter.write(run).read_text()
            assert "verifier output" not in report
            assert "verifier error output" not in report


def test_harbor_latency_reports_agent_and_wall_time_separately(tmp_path):
    cfg = load_cli_compare_config(_write_config(tmp_path))
    target, other = cfg.targets[:2]
    now = datetime.now(timezone.utc)

    def attempt(name, timings):
        return RunResult(
            "hello-file", name, TaskMode.VIBE, TaskStatus.TIMEOUT,
            started_at=now, finished_at=now + timedelta(seconds=1126),
            cli_reported_seconds=3,
            execution_details={"backend": "harbor", "timings_seconds": timings},
        )

    timed = attempt(target.name, {"environment_setup": 274, "agent": 120, "verifier": 401})
    untimed = attempt(target.name, {"environment_setup": 274})
    untimed.repeat = 2
    cfg.repeats = 2
    run = BenchmarkRun(
        run_id="timing", config=cfg,
        results=[timed, untimed, attempt(other.name, {})],
    )
    data = json.loads(JSONReporter(tmp_path).write(run).read_text())
    stats = data["summary"]["cost_stats_by_target"]
    assert stats[target.name]["avg_latency_seconds"] == 120
    assert stats[other.name]["avg_latency_seconds"] is None
    assert data["results"][0]["usage"]["cli_reported_seconds"] == 3
    assert data["results"][0]["usage"]["wall_clock_seconds"] == 1126
    assert untimed.latency_seconds is None

    reporter = HTMLReporter(tmp_path)
    context = reporter._build_cli_context(run)
    assert json.loads(context["chart_latency"]) == [120, None]
    assert [s["data"] for s in json.loads(context["chart_task_latency"])] == [[120], [None]]
    report = reporter.write(run).read_text()
    assert "Avg Agent Time" in report
    assert "Wall Time" in report
    assert "120.0s" in report
    assert "1126.0s" in report

    timed.execution_details = None
    assert timed.latency_seconds == 3
    timed.cli_reported_seconds = 0
    assert timed.latency_seconds == 1126


def test_opencode_costs_and_unknown_report(tmp_path):
    cfg = load_cli_compare_config(_write_config(tmp_path))
    [task] = discover_tasks(cfg)
    target = cfg.targets[1]
    target.pricing = Pricing()
    trial = _trial_result({"reward": 1})
    result = RunResult(
        task.id,
        target.name,
        TaskMode.VIBE,
        TaskStatus.RUNNING,
        execution_details={"backend": "harbor"},
        cost_source="unknown",
    )
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent/opencode-v2.txt").write_text((FIXTURES / "opencode.txt").read_text())
    harbor.apply_trial_result(result, task, target, trial, tmp_path)
    assert result.cost_usd is None  # A single priced turn isn't the trial's total.
    assert result.input_tokens == 210
    run = BenchmarkRun(run_id="test", config=cfg, results=[result])
    data = json.loads(JSONReporter(tmp_path).write(run).read_text())
    assert data["summary"]["total_cost_usd"] is None
    assert data["summary"]["unknown_cost_runs"] == 1
    assert data["summary"]["cost_stats_by_target"][target.name]["cost_per_pass"] is None
    assert data["results"][0]["usage"]["cost_source"] == "unknown"

    target.pricing.usd_per_input_token = 0.001
    target.pricing.usd_per_output_token = 0.002
    target.pricing.usd_per_cached_input_token = 0.0001
    target.pricing.usd_per_cache_write_token = 0.002
    harbor.apply_trial_result(result, task, target, trial, tmp_path)
    assert result.cost_usd == pytest.approx(0.251)
    assert result.cost_source == "token_pricing"


@pytest.mark.parametrize("backend, other_cost, expected", [
    ("harbor", 9.0, 10.0), ("harbor", None, None), ("host", 9.0, 1.0),
])
def test_cost_per_pass_by_backend(tmp_path, backend, other_cost, expected):
    cfg = load_cli_compare_config(_write_config(tmp_path))
    target = cfg.targets[0]
    run = BenchmarkRun(run_id="cost-per-pass", config=cfg, results=[
        RunResult("hello-file", target.name, TaskMode.VIBE, status, repeat=repeat,
                  cost_usd=cost, cost_source="unknown" if cost is None else "cli_native",
                  execution_details={"backend": backend})
        for repeat, status, cost in [(1, TaskStatus.PASSED, 1.0), (2, TaskStatus.FAILED, other_cost)]
    ])
    data = json.loads(JSONReporter(tmp_path).write(run).read_text())
    assert data["summary"]["cost_stats_by_target"][target.name]["cost_per_pass"] == expected


def test_artifact_symlink_is_not_read(tmp_path):
    root = tmp_path / "trial"
    root.mkdir()
    private_file = tmp_path / "outside"
    private_file.write_text("private")
    (root / "agent").symlink_to(private_file)
    assert harbor._read_artifact(root, "agent") == ""


@pytest.mark.parametrize("agent, filename, event, expected, source", [
    ("codex", "codex.txt", {"type": "turn.completed", "usage": {
        "input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 2,
        "reasoning_output_tokens": 1,
    }}, 0.0144, "token_pricing"),
    ("cursor-cli", "cursor-cli.txt", {"type": "result", "usage": {
        "inputTokens": 6, "cacheReadTokens": 4, "outputTokens": 2,
    }}, 0.0144, "token_pricing"),
    ("pi", "pi.txt", {"type": "turn_end", "message": {"usage": {
        "input": 6, "cacheRead": 4, "output": 2, "cost": {"total": 0.02},
    }}}, 0.02, "cli_native"),
    ("copilot-cli", "copilot-usage.json", {"modelMetrics": {"m": {
        "usage": {"inputTokens": 10, "cacheReadTokens": 4, "outputTokens": 2},
        "totalNanoAiu": 2_000_000_000,
    }}}, 0.02, "cli_native"),
    ("antigravity-cli", "antigravity-stream.jsonl", {"event": "result", "result": {
        "usage": {"input_tokens": 6, "cache_read_tokens": 4, "output_tokens": 2},
    }}, 0.0144, "token_pricing"),
    ("devin", "trajectory.json", {"final_metrics": {
        "total_prompt_tokens": 10, "total_cached_tokens": 4, "total_completion_tokens": 2,
    }}, 0.0144, "token_pricing"),
])
def test_usage_from_harbor_artifacts(tmp_path, agent, filename, event, expected, source):
    cfg = load_cli_compare_config(_write_config(tmp_path))
    [task] = discover_tasks(cfg)
    target = next(t for t in cfg.targets if t.harbor_agent() == agent)
    trial = _trial_result({"reward": 1})
    result = RunResult(task.id, target.name, TaskMode.VIBE, TaskStatus.RUNNING,
                       execution_details={}, cost_source="unknown")
    (tmp_path / "agent").mkdir()
    log = tmp_path / "agent" / filename
    log.write_text(json.dumps(event))
    if source == "token_pricing":
        target.pricing = Pricing()
        harbor.apply_trial_result(result, task, target, trial, tmp_path)
        assert result.cost_usd is None
        assert result.cost_source == "unknown"
    target.pricing.usd_per_input_token = 0.001
    target.pricing.usd_per_cached_input_token = 0.0001
    target.pricing.usd_per_output_token = 0.004
    harbor.apply_trial_result(result, task, target, trial, tmp_path)
    assert result.status is TaskStatus.PASSED
    assert (result.input_tokens, result.cached_input_tokens, result.output_tokens) == (10, 4, 2)
    assert result.cost_usd == pytest.approx(expected)
    assert result.cost_source == source
    assert result.raw_credits == (2 if agent == "copilot-cli" else None)


def test_copilot_partial_snapshot_does_not_estimate_cost():
    result = harbor._copilot_snapshot(json.dumps({
        "aiCreditsStatus": "partial",
        "modelMetrics": {"m": {"usage": {"inputTokens": 10}, "totalNanoAiu": 1_000_000_000}},
    }))
    assert result.input_tokens == 10
    assert result.cost_usd is None


def test_copilot_fallback_ignores_controller_session_state(tmp_path, monkeypatch):
    cfg = load_cli_compare_config(_write_config(tmp_path))
    [task] = discover_tasks(cfg)
    target = next(t for t in cfg.targets if t.harbor_agent() == "copilot-cli")
    result = RunResult(task.id, target.name, TaskMode.VIBE, TaskStatus.RUNNING,
                       execution_details={}, cost_source="unknown")

    def unexpected_lookup(*args, **kwargs):
        pytest.fail("Harbor telemetry accessed the controller's Copilot session state")

    monkeypatch.setattr("agent_cost_bench.usage.read_copilot_session_aiu", unexpected_lookup)
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent/copilot-cli.txt").write_text(
        '{"type":"result","sessionId":"controller-session"}\n'
    )
    harbor.apply_trial_result(result, task, target, _trial_result({"reward": 1}), tmp_path)
    assert result.status is TaskStatus.PASSED
    assert result.cost_usd is None


async def test_agent_preflight_failure_prevents_trials(tmp_path, monkeypatch):
    factory = pytest.importorskip("harbor.agents.factory").AgentFactory
    sdk = pytest.importorskip("harbor.trial.trial")
    cfg = load_cli_compare_config(_write_config(tmp_path))

    def reject_agent(agent):
        assert agent.name == cfg.targets[0].harbor_agent()
        raise ValueError("secret-provider-detail")

    async def unexpected_trial(*args, **kwargs):
        pytest.fail("Trial created before agent preflight passed")

    monkeypatch.setattr(harbor, "preflight", harbor.validate_environment)
    monkeypatch.setattr(factory, "run_preflight", reject_agent)
    monkeypatch.setattr(sdk.Trial, "create", unexpected_trial)
    with pytest.raises(ValueError, match="Runner 'claude-code': Harbor agent preflight failed") as exc:
        await BenchmarkRunner(cfg).run()
    assert "secret-provider-detail" not in str(exc.value)


@pytest.mark.parametrize("env", ["docker", "ec2"])
async def test_harbor_matrix_with_sdk(tmp_path, monkeypatch, env):
    sdk = pytest.importorskip("harbor.trial.trial")
    from harbor.models.verifier.result import VerifierResult

    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                "COPILOT_GITHUB_TOKEN", "CURSOR_API_KEY", "DEVIN_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setenv(key, "offline-test-placeholder")
    for key in ("AWS_PROFILE", "AWS_BEARER_TOKEN_BEDROCK", "OPENAI_BASE_URL", "OPENAI_API_BASE"):
        monkeypatch.delenv(key, raising=False)
    if env == "ec2":
        from harbor.environments import ec2

        def unexpected_aws_call(*args):
            pytest.fail("Offline trial attempted to create an AWS client")

        monkeypatch.setattr(ec2, "_HAS_BOTO3", True)
        monkeypatch.setattr(ec2.EC2Environment, "_ec2", unexpected_aws_call)
    kwargs = EC2_KWARGS if env == "ec2" else {}
    options = {"override_cpus": 2} if env == "ec2" else {}

    def modify(cfg):
        cfg["execution"]["harbor"].update(env=env, kwargs=kwargs, options=options)

    cfg = load_cli_compare_config(_write_config(tmp_path, modify))
    cfg.repeats = 2
    calls = []

    async def run_without_container(trial):
        calls.append(trial.config)
        try:
            trial._init_result()
            trial.result.verifier_result = VerifierResult(rewards={"reward": 1})
            return trial.result
        finally:
            trial._close_logger_handler()

    def unexpected_host_call(*args):
        pytest.fail("Harbor run entered the host execution path")

    monkeypatch.setattr(harbor, "preflight", harbor.validate_environment)
    monkeypatch.setattr(sdk.Trial, "run", run_without_container)
    monkeypatch.setattr(BenchmarkRunner, "_prevent_idle_sleep", lambda self: None)
    monkeypatch.setattr(BenchmarkRunner, "_cleanup_old_workspaces", unexpected_host_call)
    monkeypatch.setattr(BenchmarkRunner, "_docker_preflight", unexpected_host_call)
    monkeypatch.setattr("agent_cost_bench.runner.Workspace", unexpected_host_call)
    run = await BenchmarkRunner(cfg).run()
    total = cfg.repeats * len(AGENTS)
    assert run.total_runs == run.passed_runs == total, [r.error_message for r in run.results]
    assert len({c.trial_name for c in calls}) == total
    assert {c.agent.name for c in calls} == set(AGENTS)
    assert all(c.agent.override_timeout_sec is None for c in calls)
    assert all(c.environment.type == env for c in calls)
    assert all(c.environment.kwargs == kwargs for c in calls)
    assert all(c.environment.override_cpus == options.get("override_cpus") for c in calls)
    assert all(r.execution_details["sandbox"] == env for r in run.results)
    assert all(r.execution_details["verifier_mode"] == "separate" for r in run.results)
    assert all(r.execution_details["task_checksum"] for r in run.results)
    assert not Path(cfg.workspace_base).exists()
