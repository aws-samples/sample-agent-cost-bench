"""Tests for the LLM judge command construction.

Focus: the judge must build a runnable Kiro CLI command for both the plain
`kiro-cli` binary and the `kiro-cli-plus` wrapper. The wrapper already invokes
`kiro-cli chat --v3`, so the judge must NOT append a second `chat` (doing so
made the long grading prompt land as an "unexpected argument").
"""

from __future__ import annotations

from agent_cost_bench.judge import LLMJudge, _is_kiro_plus_wrapper
from agent_cost_bench.models import BenchConfig, CompareMode
from agent_cost_bench.targets import make_kiro_target


def _cfg(**kw) -> BenchConfig:
    return BenchConfig(
        mode=CompareMode.MODEL_COMPARE,
        targets=[make_kiro_target("m")],
        **kw,
    )


def test_plain_cli_includes_chat_subcommand():
    judge = LLMJudge(_cfg(judge_model="claude-opus-4.8", kiro_cli_path="kiro-cli"))
    cmd = judge._build_command("PROMPT")
    assert cmd[0] == "kiro-cli"
    assert cmd[1] == "chat"
    assert "--no-interactive" in cmd
    assert "--model=claude-opus-4.8" in cmd
    assert cmd[-1] == "PROMPT"


def test_plus_wrapper_omits_duplicate_chat():
    # kiro-cli-plus -> `kiro-cli chat --v3 ...`, so the judge must not add `chat`.
    judge = LLMJudge(
        _cfg(judge_model="claude-opus-4.8", kiro_cli_path="/abs/path/to/kiro-cli-plus")
    )
    cmd = judge._build_command("PROMPT")
    assert cmd[0] == "/abs/path/to/kiro-cli-plus"
    assert "chat" not in cmd  # the wrapper supplies `chat` itself
    assert "--no-interactive" in cmd
    assert cmd[-1] == "PROMPT"


def test_judge_cli_path_overrides_kiro_cli_path():
    judge = LLMJudge(
        _cfg(judge_model="m", kiro_cli_path="kiro-cli-plus", judge_cli_path="kiro-cli")
    )
    cmd = judge._build_command("p")
    # The override is a plain binary -> `chat` is included again.
    assert cmd[0] == "kiro-cli"
    assert cmd[1] == "chat"


def test_is_kiro_plus_wrapper_detection():
    assert _is_kiro_plus_wrapper("kiro-cli-plus") is True
    assert _is_kiro_plus_wrapper("/a/b/kiro-cli-plus") is True
    assert _is_kiro_plus_wrapper("/a/b/kiro-cli-plus/") is True
    assert _is_kiro_plus_wrapper("kiro-cli") is False
    assert _is_kiro_plus_wrapper("") is False
