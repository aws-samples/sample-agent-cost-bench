"""
RunLogger — per-run, human-readable file logger for agent_cost_bench.

One log file per benchmark run: ``<output_dir>/<run_id>.log``. Each CLI
interaction is written as a clearly delimited block. All writes are serialised
through an asyncio.Lock so concurrent runs don't interleave.
"""

from __future__ import annotations

import asyncio
import re
import signal
from datetime import datetime, timezone
from pathlib import Path

_WIDE = 68


def _exit_status(exit_code: int) -> str:
    """Render a status label, decoding signal kills (negative codes) by name."""
    if exit_code == 0:
        return "OK"
    if exit_code < 0:
        try:
            sig_name = signal.Signals(-exit_code).name
            return f"FAIL (exit {exit_code} / {sig_name})"
        except (ValueError, AttributeError):
            pass
    return f"FAIL (exit {exit_code})"


_ANSI_ESCAPE = re.compile(
    r"\x1b"
    r"(?:"
    r"\[[0-9;?]*[ -/]*[@-~]"
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|[@-_]"
    r")",
    re.ASCII,
)
_CR_ERASE = re.compile(r"\r(?!\n)")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences and stray carriage-returns from *text*."""
    text = _ANSI_ESCAPE.sub("", text)
    text = _CR_ERASE.sub("", text)
    return text


class RunLogger:
    def __init__(self, output_dir: Path, run_id: str) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        self._path = output_dir / f"{run_id}.log"
        self._lock = asyncio.Lock()
        self._fh = self._path.open("a", encoding="utf-8", buffering=1)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        self._fh.write("agent_cost_bench run log\n")
        self._fh.write(f"run_id : {run_id}\n")
        self._fh.write(f"started: {now}\n")
        self._fh.write("=" * _WIDE + "\n\n")
        self._fh.flush()

    async def log_call(
        self,
        *,
        task_id: str,
        target: str,
        command: list[str],
        prompt: str,
        stdout: str,
        stderr: str,
        exit_code: int,
        duration_seconds: float,
        phase: str = "vibe",
        credits: float | None = None,
        cost_usd: float | None = None,
    ) -> None:
        block = self._format_block(
            task_id=task_id,
            target=target,
            phase=phase,
            command=command,
            prompt=prompt,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            duration_seconds=duration_seconds,
            credits=credits,
            cost_usd=cost_usd,
        )
        async with self._lock:
            self._fh.write(block)
            self._fh.flush()

    async def log_event(self, message: str) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        async with self._lock:
            self._fh.write(f"[{now}]  {message}\n")
            self._fh.flush()

    def close(self) -> None:
        try:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            self._fh.write(f"\n{'=' * _WIDE}\n")
            self._fh.write(f"run finished: {now}\n")
            self._fh.flush()
            self._fh.close()
        except Exception:
            pass

    @property
    def path(self) -> Path:
        return self._path

    @staticmethod
    def _format_block(
        *,
        task_id: str,
        target: str,
        phase: str,
        command: list[str],
        prompt: str,
        stdout: str,
        stderr: str,
        exit_code: int,
        duration_seconds: float,
        credits: float | None,
        cost_usd: float | None,
    ) -> str:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        status = _exit_status(exit_code)
        meta = ""
        if credits is not None:
            meta += f"  credits={credits:.3f}"
        if cost_usd is not None:
            meta += f"  cost=${cost_usd:.4f}"
        stdout = strip_ansi(stdout)
        stderr = strip_ansi(stderr)

        # Show the CLI flags in the COMMAND section but don't repeat the full
        # prompt there — it's already displayed cleanly in the PROMPT section.
        cmd_parts = list(command)
        if cmd_parts and prompt and cmd_parts[-1].strip() == prompt.strip():
            cmd_parts[-1] = "<prompt — see PROMPT section below>"
        cmd_display = " ".join(cmd_parts)

        lines: list[str] = [
            "\n" + "═" * _WIDE,
            f"[{now}]  {task_id}  |  target: {target}  |  phase: {phase}",
            "─" * _WIDE,
            "COMMAND",
            f"  {cmd_display}",
            "",
            "PROMPT",
        ]
        for ln in prompt.splitlines():
            lines.append(f"  {ln}")
        lines += ["", f"RESPONSE (stdout)  [{status}]"]
        if stdout.strip():
            for ln in stdout.splitlines():
                lines.append(f"  {ln}")
        else:
            lines.append("  (empty)")
        lines += ["", "STDERR"]
        if stderr.strip():
            for ln in stderr.splitlines():
                lines.append(f"  {ln}")
        else:
            lines.append("  (empty)")
        lines += [
            "",
            f"RESULT  exit_code={exit_code}  duration={duration_seconds:.1f}s{meta}  [{status}]",
            "═" * _WIDE + "\n",
        ]
        return "\n".join(lines)
