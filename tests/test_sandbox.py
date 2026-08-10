"""Workspace setup — per-run isolation and seeded CLI policy files."""

from __future__ import annotations

import json
from pathlib import Path

from agent_cost_bench.models import BenchConfig, CompareMode
from agent_cost_bench.sandbox import Workspace
from tests.conftest import mock_target

_POLICY = {
    "permissions": {"allow": ["Read(**)", "Write(**)"], "deny": ["Exec(sudo)"]}
}


def _cfg(tmp_path, **kw) -> BenchConfig:
    return BenchConfig(
        mode=CompareMode.CLI_COMPARE,
        targets=[mock_target()],
        workspace_base=str(tmp_path / "ws"),
        output_dir=str(tmp_path / "results"),
        open_report=False,
        **kw,
    )


def _write_policy(tmp_path) -> str:
    path = tmp_path / "devin-policy.json"
    path.write_text(json.dumps(_POLICY), encoding="utf-8")
    return str(path)


def test_devin_policy_seeded_into_workspace(tmp_path, vibe_task):
    """Devin's non-interactive mode rejects any tool call needing approval, so the
    scoped allow/deny policy must land in each workspace as .devin/config.json."""
    cfg = _cfg(tmp_path, devin_permissions_file=_write_policy(tmp_path))
    ws = Workspace("run-1", vibe_task, cfg)
    ws.setup()
    seeded = ws.devin_dir / "config.json"
    assert seeded.is_file()
    assert json.loads(seeded.read_text(encoding="utf-8")) == _POLICY


def test_devin_policy_is_per_run(tmp_path, vibe_task):
    """Each run gets its own copy, so a task can't mutate the policy another run
    is about to use."""
    cfg = _cfg(tmp_path, devin_permissions_file=_write_policy(tmp_path))
    first, second = Workspace("run-1", vibe_task, cfg), Workspace("run-2", vibe_task, cfg)
    first.setup()
    second.setup()
    assert first.devin_dir != second.devin_dir
    (first.devin_dir / "config.json").write_text("{}", encoding="utf-8")
    assert json.loads((second.devin_dir / "config.json").read_text(encoding="utf-8")) == _POLICY


def test_devin_policy_empty_setting_skips_copy(tmp_path, vibe_task):
    cfg = _cfg(tmp_path, devin_permissions_file="")
    ws = Workspace("run-1", vibe_task, cfg)
    ws.setup()
    assert not ws.devin_dir.exists()


def test_devin_policy_missing_file_is_not_fatal(tmp_path, vibe_task):
    """A stale path shouldn't abort the run — Devin falls back to its default
    mode and other CLIs are unaffected."""
    cfg = _cfg(tmp_path, devin_permissions_file=str(tmp_path / "nope.json"))
    ws = Workspace("run-1", vibe_task, cfg)
    ws.setup()
    assert ws.path.is_dir()
    assert not (ws.devin_dir / "config.json").exists()


def test_shipped_devin_policy_denies_credential_paths():
    """Guard the policy that actually ships: the deny rules the user relies on
    must stay present, and no allow rule may re-open them."""
    shipped = Path(__file__).parent.parent / "tasks" / "devin" / "config.json"
    policy = json.loads(shipped.read_text(encoding="utf-8"))
    perms = policy["permissions"]
    deny = set(perms["deny"])
    for rule in (
        "Read(~/.ssh/**)", "Read(~/.aws/**)", "Read(**/.env)", "Read(**/*.pem)",
        "Exec(sudo)", "Exec(ssh)",
    ):
        assert rule in deny, f"shipped policy no longer denies {rule}"
    # Devin resolves deny > ask > allow, so an overlapping allow would silently
    # negate a deny rule (that is why the shells had to leave deny entirely
    # rather than being added to allow alongside it).
    assert deny.isdisjoint(set(perms["allow"]))


def test_shipped_devin_policy_allows_the_shell():
    """The shell is allowed deliberately, and the Exec deny list is therefore
    best-effort rather than a boundary.

    Denying bash/sh made runs measure the policy instead of the CLI: a single
    unlisted segment in `which sam; sam --version; ...` aborted a task with
    nothing written, and a heredoc blocked an agent from writing its own scratch
    test. A rejection exits 0, so the harness scores it as a failure — turning a
    policy gap into "Devin is worse". Containment is the disposable per-run
    workspace; the other two runners already get --trust-all-tools /
    --dangerously-skip-permissions, so this keeps the comparison fair.

    If this assertion is ever flipped back, re-check that every command the task
    suite needs is allowlisted AND that no task relies on a shell idiom
    (heredocs, pipelines, redirection) — otherwise silent failures return.
    """
    shipped = Path(__file__).parent.parent / "tasks" / "devin" / "config.json"
    perms = json.loads(shipped.read_text(encoding="utf-8"))["permissions"]
    for rule in ("Exec(bash)", "Exec(sh)"):
        assert rule in set(perms["allow"]), f"shipped policy no longer allows {rule}"


def test_shipped_devin_policy_allows_network_fetch():
    """curl/wget are allowed so the comparison stays symmetric.

    Denying them protected nothing — the shell is allowed, so bash -c "curl ..."
    was never blocked — but it did cost a measurement: harden-k8s scored 15%
    having written nothing, because Devin was refused when it tried to look up a
    pinned image tag. Kiro and Claude Code get --trust-all-tools /
    --dangerously-skip-permissions on the same task and both scored 100%, so the
    deny bound only the runner being measured and manufactured a capability gap.

    Restrict egress at the sandbox or network layer for all three runners if you
    need it; do not reintroduce it here for one runner alone.
    """
    shipped = Path(__file__).parent.parent / "tasks" / "devin" / "config.json"
    perms = json.loads(shipped.read_text(encoding="utf-8"))["permissions"]
    allow, deny = set(perms["allow"]), set(perms["deny"])
    for rule in ("Exec(curl)", "Exec(wget)"):
        assert rule in allow, f"shipped policy no longer allows {rule}"
        assert rule not in deny, f"{rule} is in deny; deny beats allow, so it wins"


def test_shipped_devin_policy_allows_venv_interpreters():
    """Exec() matches literal prefixes only — no globs — so a venv interpreter
    invoked by path needs a verbatim rule.

    Without these, the standard Python workflow
    ``python3 -m venv .venv && .venv/bin/pip install -r requirements.txt``
    is rejected at the second segment and the agent ships untested code, which
    silently skews a cost comparison rather than failing loudly.
    """
    shipped = Path(__file__).parent.parent / "tasks" / "devin" / "config.json"
    allow = set(json.loads(shipped.read_text(encoding="utf-8"))["permissions"]["allow"])
    for prefix in (".venv/bin/", "./.venv/bin/", "venv/bin/", "./venv/bin/"):
        for tool in ("python", "pip", "pytest"):
            rule = f"Exec({prefix}{tool})"
            assert rule in allow, f"shipped policy no longer allows {rule}"
