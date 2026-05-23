"""``TextRewriteWorker`` — abstract base for backend-specific rewrite workers.

Mirrors the ``AudioTtsWorker`` shape: each LLM backend gets its own concrete
subclass + entry point, the base owns the shared execute() path.

Entry points (v0 ships Claude):
  python -m spindle_workers.text_rewrite.claude  → ClaudeTextRewriteWorker
"""
from __future__ import annotations

import logging
from abc import abstractmethod

from spindle_core.types.job import Job

from ..base import JobContext, JobResult, WorkerBase, WorkerConfig
from .backends.base import BaseRewriter

log = logging.getLogger("spindle_workers")


_DEFAULT_SYSTEM_PROMPT = (
    "You are a careful editor. Rewrite the user's text faithfully, preserving "
    "meaning, jargon, and concrete numbers. Output only the rewritten text — "
    "no preamble, no commentary."
)


class TextRewriteWorker(WorkerBase):
    """Abstract base for LLM rewriter workers."""

    capabilities = ["text.rewrite"]
    backend_name: str = ""

    def __init__(self, config: WorkerConfig) -> None:
        super().__init__(config)
        if not self.backend_name:
            raise SystemExit(
                f"{type(self).__name__}.backend_name is empty. Concrete "
                f"subclass must set it (e.g. 'claude', 'openai')."
            )
        self._rewriter = self._make_backend()
        log.info("text_rewrite backend=%s ready", self.backend_name)

    @abstractmethod
    def _make_backend(self) -> BaseRewriter:
        """Return the BaseRewriter for this subclass."""

    async def execute(self, job: Job, ctx: JobContext) -> JobResult:
        import asyncio

        text = job.input["text"]
        system_prompt = (
            job.input.get("system_prompt")
            or job.input.get("prompt_template")  # alias
            or _DEFAULT_SYSTEM_PROMPT
        )
        max_tokens = int(job.input.get("max_tokens", 4096))
        options = job.input.get("options") or {}

        log.info(
            "rewrite start chars_in=%d system_prompt_chars=%d",
            len(text), len(system_prompt),
        )
        # Run sync SDK call off the event loop so heartbeats stay responsive.
        result = await asyncio.to_thread(
            self._rewriter.rewrite,
            text,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            **options,
        )
        log.info(
            "rewrite done chars_out=%d input_tokens=%s output_tokens=%s",
            len(result.text),
            result.usage.get("input_tokens"),
            result.usage.get("output_tokens"),
        )

        return JobResult(
            output={
                "rewritten": result.text,
                "char_count_in": len(text),
                "char_count_out": len(result.text),
                "backend": self.backend_name,
            },
            runtime=result.usage,
        )
