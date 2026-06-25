"""
Unified execution core.

A single ``BaseExecutor`` drives any ``Target``: it assembles the command from
the target's templates, runs a prompt, parses usage via the target's
``cost_source`` (recording USD + native units), retries transient
CLI/backend/network failures with backoff, and kills the whole process tree on
timeout/cancel. ``VibeExecutor`` is the one-phase specialisation; spec phases
live in :mod:`agent_cost_bench.executor.spec`.
"""

from __future__ import annotations

import asyncio
import os
import pty
import shlex
import signal
import sys
import time as _time
import uuid
from pathlib import Path

from ..logger import RunLogger
from ..models import BenchConfig, CostSource, PhaseResult, Target, TaskConfig, Usage
from ..usage import parse_usage

_IS_TTY = sys.stdout.isatty()


def resolve_task_prompt(task) -> str:
    """Resolve a task's prompt from the inline ``prompt:`` field in task.yaml.
    Returns an empty string when no prompt is set."""
    if getattr(task, "prompt", "") and task.prompt.strip():
        return task.prompt.strip()
    return ""


class CLIError(Exception):
    pass


class CLITimeoutError(TimeoutError):
    """Timeout that carries whatever stdout/stderr was captured before the kill,
    so a hung CLI's partial output is preserved for diagnosis."""

    def __init__(self, message: str, stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


# Cap on captured bytes per stream so a CLI stuck in a redraw loop can't exhaust
# memory. Excess is dropped but the process is still drained so it never blocks.
_MAX_CAPTURE = 5_000_000


async def _read_stream(stream: asyncio.StreamReader | None, buf: list[bytes]) -> None:
    """Drain a stream into ``buf`` (capped). Always reads to EOF so the child
    never blocks on a full pipe."""
    if stream is None:
        return
    total = 0
    while True:
        try:
            chunk = await stream.read(65536)
        except Exception:
            break
        if not chunk:
            break
        if total < _MAX_CAPTURE:
            buf.append(chunk)
            total += len(chunk)


def _describe_exit(exit_code: int) -> str:
    if exit_code >= 0:
        return f"exit {exit_code} (clean exit)" if exit_code == 0 else f"exit {exit_code}"
    sig_num = -exit_code
    try:
        sig_name = signal.Signals(sig_num).name
    except (ValueError, AttributeError):
        sig_name = f"signal {sig_num}"
    hints = {
        "SIGTERM": "terminated (SIGTERM) — external kill, harness timeout/cancel, or OOM",
        "SIGKILL": "killed (SIGKILL) — hard kill, often the OS OOM killer",
        "SIGINT": "interrupted (SIGINT) — Ctrl+C / parent interrupt",
        "SIGSEGV": "crashed (SIGSEGV) — segmentation fault",
    }
    detail = hints.get(sig_name, f"killed by {sig_name}")
    return f"exit {exit_code} → {detail}"


async def _terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the CLI's entire process group and reap it without blocking forever.
    proc must have been started with start_new_session=True."""
    if proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
        return
    except asyncio.TimeoutError:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        pass


class BaseExecutor:
    """Async wrapper that runs one ``Target`` and parses usage per turn."""

    def __init__(
        self,
        config: BenchConfig,
        task: TaskConfig,
        workspace_path: Path,
        target: Target,
        logger: RunLogger | None = None,
    ):
        self.config = config
        self.task = task
        self.workspace = workspace_path
        self.target = target
        self._logger = logger

    # ------------------------------------------------------------------
    # Command / env assembly from Target templates
    # ------------------------------------------------------------------

    def _build_env(self, run_id: str | None = None) -> dict[str, str]:
        env = os.environ.copy()
        # Kiro targets: only inject an explicit API key when configured;
        # otherwise rely on the CLI's own login session — exactly like a manual
        # run. We deliberately do NOT override KIRO_HOME: that is the CLI's
        # config/auth/session dir (not the spec output location, which is
        # project-local under <cwd>/.kiro/specs). Repointing it at an empty
        # per-run dir strips the login session and makes spec mode hang.
        # Per-run isolation comes from the unique workspace cwd instead.
        if self.target.cost_source == CostSource.KIRO_CREDITS or self.target.capabilities.supports_spec:
            if self.config.kiro_api_key:
                env["KIRO_API_KEY"] = self.config.kiro_api_key
        env.update(self.target.env)
        # Per-turn correlation id for kas-proxy: the shim forwards this as an
        # X-Kas-Run-Id header on redirected inference requests, letting the
        # kas_proxy_metrics cost source look up the matching metrics.jsonl
        # record. Setting this for every Kiro turn is harmless when kas-proxy
        # isn't in use (the shim only attaches the header when present).
        if run_id:
            env["KAS_RUN_ID"] = run_id
        return env

    def _build_command(self, prompt: str, agent: str | None = None, extra_args: list[str] | None = None) -> list[str]:
        t = self.target
        # Per-task effort takes precedence over the run-level default.
        effort = getattr(self.task, "effort", None) or self.config.effort or "high"
        cmd: list[str] = [t.cli_path]
        prompt_used = False
        for arg in t.cli_base_args:
            rendered = arg.format(
                model=t.model_id, prompt=prompt, agent=agent or "", effort=effort
            )
            if "{prompt}" in arg:
                prompt_used = True
            cmd.append(rendered)

        # Task-specific args (e.g. spec-mode '--mode spec') injected by callers.
        if extra_args:
            cmd.extend(extra_args)

        if t.cli_model_flag:
            cmd.extend(shlex.split(t.cli_model_flag.format(model=t.model_id)))
        if agent and t.cli_agent_flag and t.capabilities.supports_agents:
            cmd.extend(shlex.split(t.cli_agent_flag.format(agent=agent)))
        if effort and t.cli_effort_flag:
            cmd.extend(shlex.split(t.cli_effort_flag.format(effort=effort)))

        cmd.extend(t.extra_args)

        if not prompt_used and prompt:
            cmd.append(prompt)
        return cmd

    # ------------------------------------------------------------------
    # Raw CLI turn
    # ------------------------------------------------------------------

    async def _exec_pty(
        self, cmd: list[str], timeout: float, stdin_text: str | None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str, bool]:
        """Run ``cmd`` attached to a pseudo-terminal so a TTY-requiring CLI (e.g.
        Kiro's `--mode spec`) runs as if interactive. Returns
        (exit_code, combined_output, timed_out). stdout+stderr are merged by the
        PTY. Unix only.

        ``env`` is the subprocess environment; pass an env that already includes
        the per-turn KAS_RUN_ID. Falls back to ``self._build_env()`` for backward
        compatibility with direct callers."""
        master_fd, slave_fd = pty.openpty()
        # Give the pty a sane window size so TUIs render instead of waiting.
        try:
            import fcntl
            import struct
            import termios

            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
        except Exception:
            pass

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=str(self.workspace),
            env=env if env is not None else self._build_env(),
            start_new_session=True,
        )
        os.close(slave_fd)  # parent keeps only the master end

        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        master_file = os.fdopen(master_fd, "rb", buffering=0)
        transport, _ = await loop.connect_read_pipe(lambda: protocol, master_file)

        captured = bytearray()

        async def _drain() -> None:
            while True:
                try:
                    chunk = await reader.read(65536)
                except Exception:
                    break  # pty master raises EIO on slave close — that's EOF
                if not chunk:
                    break
                if len(captured) < _MAX_CAPTURE:
                    captured.extend(chunk)

        drain_task = asyncio.create_task(_drain())

        # If the CLI reads its request from the tty's stdin, write it to master.
        if stdin_text:
            try:
                os.write(master_fd, stdin_text.encode("utf-8"))
            except Exception:
                pass

        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=float(timeout))
        except asyncio.TimeoutError:
            timed_out = True
            await _terminate_process_tree(proc)
        finally:
            try:
                transport.close()
            except Exception:
                pass
            await asyncio.gather(drain_task, return_exceptions=True)

        exit_code = proc.returncode if proc.returncode is not None else -1
        output = bytes(captured).decode("utf-8", errors="replace")
        return exit_code, output, timed_out

    async def run_cli_turn(
        self,
        prompt: str,
        agent: str | None = None,
        timeout_seconds: int | None = None,
        phase: str = "vibe",
        extra_args: list[str] | None = None,
        stdin_text: str | None = None,
        use_pty: bool = False,
    ) -> tuple[int, str, str, str]:
        """Run one CLI turn headless. Returns (exit_code, stdout, stderr, run_id).

        ``run_id`` is the unique per-turn correlation id set as ``KAS_RUN_ID``
        in the subprocess env; callers thread it into ``parse_usage`` so the
        kas_proxy_metrics cost source can look up the matching record.

        If ``stdin_text`` is provided, it is piped to the process's stdin (used
        by spec mode, which reads its request from stdin). Otherwise stdin is
        /dev/null so the CLI never blocks waiting for interactive input.

        If ``use_pty`` is true, the process runs under a pseudo-terminal so a
        TTY-requiring CLI (Kiro's `--mode spec`) behaves as if interactive."""
        cmd = self._build_command(prompt, agent, extra_args)
        timeout = timeout_seconds or (self.task.timeout_minutes * 60)
        start = _time.monotonic()
        # Generate a unique per-turn correlation id. The shim (loaded via
        # kiro-cli-plus) sends it as X-Kas-Run-Id on the redirected inference
        # request so the kas-proxy metrics record can be looked up by id later.
        # Cheap and always-on: setting KAS_RUN_ID is a no-op when kas-proxy
        # isn't in the loop, but lets the kas_proxy_metrics cost source work
        # the moment the proxy is.
        run_id = uuid.uuid4().hex
        env = self._build_env(run_id=run_id)

        if self._logger:
            stdin_note = "  stdin=<prompt>" if stdin_text is not None else ""
            pty_note = "  pty=1" if use_pty else ""
            # Truncate the command for the event line — the full prompt is logged
            # later in the PROMPT section of the call block.
            cmd_str = shlex.join(cmd)
            if len(cmd_str) > 300:
                cmd_str = cmd_str[:300] + "…"
            await self._logger.log_event(
                f"CALL START  {self.task.id}  target={self.target.label}  phase={phase}"
                f"  timeout={timeout}s{stdin_note}{pty_note}\n"
                f"    cmd: {cmd_str}"
            )

        # PTY path for TTY-requiring CLIs (spec mode). stdout/stderr are merged.
        if use_pty:
            exit_code, stdout, timed_out = await self._exec_pty(cmd, timeout, stdin_text, env=env)
            stderr = ""
            wall = _time.monotonic() - start
            if timed_out:
                if self._logger:
                    await self._logger.log_call(
                        task_id=self.task.id, target=self.target.label, phase=phase,
                        command=cmd, prompt=prompt, stdout=stdout,
                        stderr=f"[TIMEOUT after {timeout}s — process killed]",
                        exit_code=-1, duration_seconds=wall,
                    )
                raise CLITimeoutError(
                    f"{self.target.label} timed out after {timeout}s on '{self.task.id}' "
                    f"(phase: {phase})",
                    stdout=stdout, stderr="",
                )
            if self._logger:
                usage = parse_usage(self.target, stdout, stderr, run_id=run_id)
                await self._logger.log_call(
                    task_id=self.task.id, target=self.target.label, phase=phase,
                    command=cmd, prompt=prompt, stdout=stdout, stderr=stderr,
                    exit_code=exit_code, duration_seconds=wall,
                    credits=usage.raw_credits, cost_usd=usage.cost_usd,
                )
            return exit_code, stdout, stderr, run_id

        stdin_mode = asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=stdin_mode,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace),
                env=env,
                start_new_session=True,
            )
            input_bytes = stdin_text.encode("utf-8") if stdin_text is not None else None
            if input_bytes is not None and proc.stdin is not None:
                try:
                    proc.stdin.write(input_bytes)
                    await proc.stdin.drain()
                except Exception:
                    pass
                try:
                    proc.stdin.close()
                except Exception:
                    pass

            # Read both streams concurrently so we retain partial output even if
            # the process has to be killed on timeout.
            out_buf: list[bytes] = []
            err_buf: list[bytes] = []
            out_task = asyncio.create_task(_read_stream(proc.stdout, out_buf))
            err_task = asyncio.create_task(_read_stream(proc.stderr, err_buf))

            try:
                await asyncio.wait_for(proc.wait(), timeout=float(timeout))
            except asyncio.TimeoutError:
                await _terminate_process_tree(proc)
                # Let the readers drain whatever was buffered before the kill.
                await asyncio.gather(out_task, err_task, return_exceptions=True)
                partial_out = b"".join(out_buf).decode("utf-8", errors="replace")
                partial_err = b"".join(err_buf).decode("utf-8", errors="replace")
                if self._logger:
                    await self._logger.log_call(
                        task_id=self.task.id,
                        target=self.target.label,
                        phase=phase,
                        command=cmd,
                        prompt=prompt,
                        stdout=partial_out,
                        stderr=(partial_err + f"\n[TIMEOUT after {timeout}s — process killed]"),
                        exit_code=-1,
                        duration_seconds=_time.monotonic() - start,
                    )
                raise CLITimeoutError(
                    f"{self.target.label} timed out after {timeout}s on '{self.task.id}' "
                    f"(phase: {phase})",
                    stdout=partial_out,
                    stderr=partial_err,
                )

            await asyncio.gather(out_task, err_task, return_exceptions=True)
            exit_code = proc.returncode or 0
            stdout = b"".join(out_buf).decode("utf-8", errors="replace")
            stderr = b"".join(err_buf).decode("utf-8", errors="replace")
            wall = _time.monotonic() - start

            if self._logger and exit_code != 0:
                produced = bool(stdout.strip())
                await self._logger.log_event(
                    f"ABNORMAL EXIT  {self.task.id}  target={self.target.label}  "
                    f"{_describe_exit(exit_code)}  after={wall:.1f}s  "
                    f"stdout={'present' if produced else 'EMPTY'}"
                )

            if self._logger:
                usage = parse_usage(self.target, stdout, stderr, run_id=run_id)
                await self._logger.log_call(
                    task_id=self.task.id,
                    target=self.target.label,
                    phase=phase,
                    command=cmd,
                    prompt=prompt,
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=exit_code,
                    duration_seconds=wall,
                    credits=usage.raw_credits,
                    cost_usd=usage.cost_usd,
                )
            return exit_code, stdout, stderr, run_id

        except asyncio.CancelledError:
            if proc is not None:
                await _terminate_process_tree(proc)
            if self._logger:
                await self._logger.log_event(
                    f"CALL CANCELLED  {self.task.id}  target={self.target.label}  phase={phase}"
                )
            raise
        except FileNotFoundError:
            raise CLIError(
                f"CLI not found at '{self.target.cli_path}' for target '{self.target.label}'."
            )

    # ------------------------------------------------------------------
    # Phase wrapper with transient-retry-with-backoff
    # ------------------------------------------------------------------

    async def run_phase(
        self,
        phase: str,
        prompt: str,
        agent: str | None = None,
        timeout_seconds: int | None = None,
        extra_args: list[str] | None = None,
        stdin_text: str | None = None,
        use_pty: bool = False,
    ) -> PhaseResult:
        """Run one CLI turn → a fully-formed PhaseResult (usage parsed).
        Transient CLI/backend/network failures are retried with backoff up to
        config.transient_retries; real code/logic failures and timeouts are not."""
        max_retries = max(0, getattr(self.config, "transient_retries", 0))
        attempt = 0
        while True:
            phase_result = await self._run_phase_once(
                phase, prompt, agent=agent, timeout_seconds=timeout_seconds,
                extra_args=extra_args, stdin_text=stdin_text, use_pty=use_pty,
            )
            if phase_result.success or not phase_result.transient_error or attempt >= max_retries:
                phase_result.transient_retries = attempt
                return phase_result
            attempt += 1
            if self._logger:
                await self._logger.log_event(
                    f"TRANSIENT RETRY  {self.task.id}  target={self.target.label}  "
                    f"phase={phase}  attempt={attempt}/{max_retries}  "
                    f"reason={(phase_result.error or 'transient error')[:80]}"
                )
            await asyncio.sleep(min(5.0 * attempt, 15.0))

    async def _run_phase_once(
        self,
        phase: str,
        prompt: str,
        agent: str | None = None,
        timeout_seconds: int | None = None,
        extra_args: list[str] | None = None,
        stdin_text: str | None = None,
        use_pty: bool = False,
    ) -> PhaseResult:
        if not _IS_TTY:
            print(f"    -> {phase} ...", flush=True)

        start = _time.monotonic()
        try:
            exit_code, stdout, stderr, run_id = await self.run_cli_turn(
                prompt=prompt, agent=agent, timeout_seconds=timeout_seconds, phase=phase,
                extra_args=extra_args, stdin_text=stdin_text, use_pty=use_pty,
            )
            usage: Usage = parse_usage(self.target, stdout, stderr, run_id=run_id)
            phase_result = PhaseResult(
                phase=phase,
                success=exit_code == 0,
                duration_seconds=_time.monotonic() - start,
                stdout=stdout,
                stderr=stderr,
                credits=usage.raw_credits,
                cost_usd=usage.cost_usd,
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                output_tokens=usage.output_tokens,
                reasoning_output_tokens=usage.reasoning_output_tokens,
                premium_requests=usage.premium_requests,
                cli_reported_seconds=usage.seconds,
                error=None if exit_code == 0 else f"CLI exited with code {exit_code}",
            )
            if phase_result.model_unavailable:
                phase_result.success = False
                phase_result.error = (
                    f"Model '{self.target.label}' is temporarily unavailable "
                    f"(service down — not a code/test failure)"
                )
            return phase_result
        except TimeoutError as e:
            return PhaseResult(
                phase=phase,
                success=False,
                duration_seconds=_time.monotonic() - start,
                stdout=getattr(e, "stdout", "") or "",
                stderr=getattr(e, "stderr", "") or "",
                error=str(e),
            )
        except CLIError:
            raise
        except Exception as e:
            return PhaseResult(
                phase=phase,
                success=False,
                duration_seconds=_time.monotonic() - start,
                error=f"Unexpected error: {e}",
            )


class VibeExecutor(BaseExecutor):
    """Runs a single vibe prompt (one phase) through one target."""

    async def execute(self) -> list[PhaseResult]:
        prompt = resolve_task_prompt(self.task)
        if not prompt:
            where = self.task.task_dir
            return [
                PhaseResult(
                    phase="vibe",
                    success=False,
                    duration_seconds=0.0,
                    error=f"No prompt found: set 'prompt:' in task.yaml for task in {where}",
                )
            ]
        return [await self.run_phase(
            "vibe", prompt,
            agent=self.config.vibe_agent,
            use_pty=self.config.vibe_use_pty,
        )]
