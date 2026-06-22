"""
Harness tests for task-005-log-analyzer-cli.

Runs the model's loganalyzer.py against a fixed sample log (sample_access.log,
located next to this file in verify/) and checks each field of the JSON summary.

WORKSPACE is set to the model's output directory by the harness.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

WS = Path(os.environ.get("WORKSPACE", "."))
SAMPLE_LOG = Path(__file__).parent / "sample_access.log"

# Known-correct summary for sample_access.log (9 valid lines, 1 malformed).
_EXPECTED_TOTAL = 9
_EXPECTED_STATUS = {"2xx": 6, "3xx": 1, "4xx": 1, "5xx": 1}
_EXPECTED_BYTES = 2300
_EXPECTED_TOP_IPS = [["10.0.0.1", 4], ["10.0.0.2", 2], ["10.0.0.3", 2], ["10.0.0.4", 1]]


@pytest.fixture(scope="module")
def output() -> dict:
    script = WS / "loganalyzer.py"
    if not script.exists():
        pytest.skip("loganalyzer.py not found")
    r = subprocess.run(
        [sys.executable, str(script), str(SAMPLE_LOG)],
        cwd=WS, capture_output=True, text=True, timeout=30,
    )
    try:
        data = json.loads(r.stdout.strip())
        assert isinstance(data, dict), "output is not a JSON object"
        return data
    except (json.JSONDecodeError, AssertionError) as e:
        pytest.fail(f"loganalyzer.py did not output valid JSON: {r.stdout[:300]!r} — {e}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_script_exists():
    assert (WS / "loganalyzer.py").exists(), "loganalyzer.py not produced"


def test_total_requests(output):
    assert output.get("total_requests") == _EXPECTED_TOTAL, \
        f"total_requests: expected {_EXPECTED_TOTAL}, got {output.get('total_requests')}"


def test_status_2xx(output):
    assert output.get("status_classes", {}).get("2xx") == _EXPECTED_STATUS["2xx"]


def test_status_3xx(output):
    assert output.get("status_classes", {}).get("3xx") == _EXPECTED_STATUS["3xx"]


def test_status_4xx(output):
    assert output.get("status_classes", {}).get("4xx") == _EXPECTED_STATUS["4xx"]


def test_status_5xx(output):
    assert output.get("status_classes", {}).get("5xx") == _EXPECTED_STATUS["5xx"]


def test_total_bytes(output):
    assert output.get("total_bytes") == _EXPECTED_BYTES, \
        f"total_bytes: expected {_EXPECTED_BYTES}, got {output.get('total_bytes')}"


def test_top_ips(output):
    raw = output.get("top_ips") or []
    normalized = [[str(e[0]), int(e[1])] for e in raw if len(e) == 2]
    assert normalized == _EXPECTED_TOP_IPS, \
        f"top_ips mismatch: expected {_EXPECTED_TOP_IPS}, got {normalized}"
