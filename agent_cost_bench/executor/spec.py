"""
Spec-driven executor.

Modern Kiro CLIs run spec-driven development natively in a single invocation::

    kiro chat --no-interactive --trust-all-tools --mode spec --model X --effort Y "<prompt>"

So this executor makes ONE CLI call with the target's ``spec_mode_args`` injected
(``["--mode", "spec"]`` by default for Kiro targets). The CLI internally handles
requirements -> design -> tasks -> implementation and writes artifacts under
``.kiro/specs/``. Execution is gated on the target's ``supports_spec`` capability.

The prompt is resolved from (in order): the task's inline ``prompt:`` field, a
seeded ``requirements.md`` in the workspace, or the task ``description``.
"""

from __future__ import annotations

from ..models import PhaseResult
from .base import BaseExecutor, resolve_task_prompt


class SpecCapabilityError(Exception):
    """Raised when a spec task is run against a target that can't do spec-driven work."""


class SpecDrivenExecutor(BaseExecutor):
    """Drives spec-driven work via the CLI's native spec mode (single call)."""

    async def execute(self) -> list[PhaseResult]:
        if not self.target.capabilities.supports_spec:
            raise SpecCapabilityError(
                f"Target '{self.target.label}' does not support spec-driven tasks "
                f"(capabilities.supports_spec is false)."
            )

        prompt = self._resolve_prompt()
        spec_args = list(self.target.spec_mode_args)
        use_pty = getattr(self.config, "spec_use_pty", True)
        if self.config.spec_prompt_via_stdin:
            # The CLI reads the spec request from stdin; don't pass it as an arg.
            phase = await self.run_phase(
                "spec", "", agent=self.config.spec_executor_agent,
                extra_args=spec_args, stdin_text=prompt, use_pty=use_pty,
            )
        else:
            phase = await self.run_phase(
                "spec", prompt, agent=self.config.spec_executor_agent,
                extra_args=spec_args, use_pty=use_pty,
            )
        return [phase]

    # ------------------------------------------------------------------

    def _resolve_prompt(self) -> str:
        # 1. An explicit inline prompt (task.yaml `prompt:`) drives the spec end
        #    to end.
        prompt = resolve_task_prompt(self.task)
        if prompt:
            return prompt

        # 2. A seeded requirements.md — point the CLI at it.
        req = self.workspace / ".kiro" / "specs" / self.task.id / "requirements.md"
        if req.exists():
            return (
                f"Implement the feature specified in "
                f".kiro/specs/{self.task.id}/requirements.md. Produce the full spec "
                f"(requirements, design, tasks) and implement all tasks."
            )

        # 3. Fall back to the task description.
        return self.task.description
