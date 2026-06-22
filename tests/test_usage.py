"""Task 2 — cost/usage parsing tests (USD + native units)."""

from __future__ import annotations

import json

from kirobench.models import CostSource, Pricing, Target
from kirobench.targets import make_cli_target
from kirobench.usage import (
    parse_claude_usage,
    parse_copilot_usage,
    parse_kiro_usage,
    parse_token_regex_usage,
    parse_usage,
)


def _target(cost_source, **pricing):
    return make_cli_target(
        {
            "name": "t",
            "cli_path": "x",
            "model_id": "m",
            "cost_source": cost_source,
            "pricing": pricing,
        }
    )


def test_kiro_credits_and_usd():
    stderr = "some output\n▸ Credits: 0.05 • Time: 2s\n"
    u = parse_kiro_usage("", stderr, Pricing(usd_per_credit=0.04))
    assert abs(u.raw_credits - 0.05) < 1e-9
    assert abs(u.cost_usd - 0.05 * 0.04) < 1e-9
    assert u.seconds == 2.0


def test_kiro_ignores_model_generated_text_prefers_telemetry_banner():
    # A model echoing "Credits:" in stdout must not override the real banner.
    stdout = "Here is the plan. Credits: 999\n"
    stderr = "▸ Credits: 0.10 • Time: 1m 3s\n"
    u = parse_kiro_usage(stdout, stderr, Pricing(usd_per_credit=0.04))
    assert abs(u.raw_credits - 0.10) < 1e-9
    assert u.seconds == 63.0


def test_claude_json_total_cost_usd():
    obj = {
        "type": "result",
        "total_cost_usd": 0.0123,
        "duration_ms": 4200,
        "usage": {"input_tokens": 1200, "output_tokens": 350},
    }
    u = parse_claude_usage(json.dumps(obj), "", Pricing())
    assert abs(u.cost_usd - 0.0123) < 1e-9
    assert u.input_tokens == 1200
    assert u.output_tokens == 350
    assert abs(u.seconds - 4.2) < 1e-9


def test_copilot_jsonl_premium_request_fallback():
    lines = [
        json.dumps({"type": "turn", "usage": {"input_tokens": 100, "output_tokens": 50}}),
        json.dumps({"type": "result", "premiumRequests": 0.33, "sessionDurationMs": 5000}),
    ]
    u = parse_copilot_usage("\n".join(lines), "", Pricing(usd_per_premium_request=0.04))
    assert abs(u.premium_requests - 0.33) < 1e-9
    assert abs(u.cost_usd - 0.33 * 0.04) < 1e-9
    assert u.seconds == 5.0
    assert u.input_tokens == 100


def test_copilot_session_state_aiu_overrides(tmp_path):
    # Write a fake session-state events.jsonl with totalNanoAiu.
    session_id = "sess-123"
    ev_dir = tmp_path / ".copilot" / "session-state" / session_id
    ev_dir.mkdir(parents=True)
    (ev_dir / "events.jsonl").write_text(
        json.dumps({"type": "session.shutdown", "data": {"totalNanoAiu": 2_000_000_000}}) + "\n"
    )
    stdout = json.dumps({"type": "result", "sessionId": session_id, "premiumRequests": 0.33})
    u = parse_copilot_usage(stdout, "", Pricing(usd_per_premium_request=0.04), home=tmp_path)
    # 2e9 nanoAIU = 2 AIU = $0.02, more accurate than premiumRequests * price.
    assert abs(u.cost_usd - 0.02) < 1e-9
    assert abs(u.raw_credits - 2.0) < 1e-9


def test_token_regex_pricing():
    text = "tokens in=1000 out=500"
    u = parse_token_regex_usage(
        text,
        "",
        Pricing(usd_per_input_token=0.000001, usd_per_output_token=0.000002),
        r"in=(?P<input>\d+)\s+out=(?P<output>\d+)",
    )
    assert u.input_tokens == 1000
    assert u.output_tokens == 500
    assert abs(u.cost_usd - (1000 * 1e-6 + 500 * 2e-6)) < 1e-12


def test_premium_request_fixed():
    t = _target("premium_request", usd_per_premium_request=0.04, requests_per_run=2)
    u = parse_usage(t, "", "")
    assert u.premium_requests == 2
    assert abs(u.cost_usd - 0.08) < 1e-9


def test_dispatch_none_source_returns_empty():
    t = Target(name="t", cli_path="x", model_id="m", cost_source=CostSource.NONE)
    u = parse_usage(t, "anything", "")
    assert u.cost_usd is None and u.raw_credits is None
