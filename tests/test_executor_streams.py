"""Output capture and post-exit cleanup for CLI subprocesses."""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from agent_cost_bench.executor import base


def test_capture_keeps_head_and_tail(monkeypatch):
    """Long transcripts keep their final lines (where stream-json CLIs put the
    completion and credit events); only the middle is dropped."""
    monkeypatch.setattr(base, "_MAX_CAPTURE", 20)
    monkeypatch.setattr(base, "_TAIL_CAPTURE", 30)
    cap = base._Capture()
    for i in range(50):
        cap.add(f"line{i:02d}\n".encode())
    out = cap.value().decode()
    assert out.startswith("line00\nline01\nline02\n")
    assert out.endswith("line48\nline49\n")
    assert "bytes of output omitted" in out
    # The tail starts on a whole line, so JSON Lines parsers never see a fragment.
    tail = out.split("omitted]\n", 1)[1]
    assert all(len(line) == 6 for line in tail.splitlines())


def test_capture_small_output_unchanged():
    cap = base._Capture()
    cap.add(b"hello\n")
    cap.add(b"world\n")
    assert cap.value() == b"hello\nworld\n"


@pytest.mark.asyncio
async def test_finish_streams_stops_leftover_children(monkeypatch):
    """A background child that keeps the CLI's stdout open must not hold the
    run: the harness stops it after the grace period."""
    monkeypatch.setattr(base, "_DRAIN_GRACE_SECONDS", 0.5)
    # The "CLI" starts a long-lived child that inherits stdout, then exits.
    script = "import subprocess, sys; subprocess.Popen(['sleep', '60']); print('done')"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    out: list[bytes] = []
    err: list[bytes] = []
    readers = [
        asyncio.create_task(base._read_stream(proc.stdout, out)),
        asyncio.create_task(base._read_stream(proc.stderr, err)),
    ]
    # proc.wait() would block until the leftover `sleep` closes the pipes;
    # _wait_exit returns as soon as the CLI process itself exits.
    started = time.monotonic()
    await asyncio.wait_for(base._wait_exit(proc), timeout=10)
    assert time.monotonic() - started < 5
    started = time.monotonic()
    stopped = await base._finish_streams(proc, readers)
    assert time.monotonic() - started < 10
    assert stopped is True
    assert b"".join(out).strip() == b"done"
