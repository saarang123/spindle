"""Claude (Anthropic) rewrite backend.

Wraps ``anthropic.Anthropic.messages.create``. Defaults to Claude Opus 4.7
for highest-quality narration rewrites; override per-deploy via the worker's
ModelConfig.params["model"].

Requires ``[text_rewrite_claude]`` install (pulls ``anthropic`` SDK).
"""
from __future__ import annotations

import os

from .base import BaseRewriter, RewriteResult


_DEFAULT_MODEL = "claude-opus-4-7"


class ClaudeRewriter(BaseRewriter):
    """Synchronous Claude API rewriter.

    Args:
        model: Anthropic model id. Defaults to ``"claude-opus-4-7"``.
        api_key: API key. Falls back to ``ANTHROPIC_API_KEY`` env var.
    """

    def __init__(
        self,
        model: str = _DEFAULT_MODEL,
        api_key: str | None = None,
    ) -> None:
        from anthropic import Anthropic  # type: ignore

        self.model = model
        self._client = Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY")
        )

    def rewrite(
        self,
        text: str,
        *,
        system_prompt: str,
        max_tokens: int = 4096,
        **opts,
    ) -> RewriteResult:
        temperature = float(opts.get("temperature", 0.3))
        response = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": text}],
            temperature=temperature,
        )

        # Concatenate text blocks (Claude can return multiple, e.g. when
        # tool use is mixed in; here we just take text content).
        rewritten_parts = [
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text"
        ]
        rewritten = "".join(rewritten_parts).strip()

        return RewriteResult(
            text=rewritten,
            usage={
                "model": self.model,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "stop_reason": response.stop_reason,
            },
        )
