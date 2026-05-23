"""Claude rewrite worker — Anthropic-hosted, CPU-only (HTTP client)."""
from __future__ import annotations

from ..backends.base import BaseRewriter
from ..backends.claude import ClaudeRewriter
from ..worker import TextRewriteWorker


class ClaudeTextRewriteWorker(TextRewriteWorker):
    backend_name = "claude"

    def _make_backend(self) -> BaseRewriter:
        # Constructor picks up ANTHROPIC_API_KEY from env. ModelConfig.params
        # can override the model via `params.model` — but currently the
        # WorkerConfig doesn't pipe params through; using the default for
        # now and revisiting once a second model variant is needed.
        return ClaudeRewriter()
