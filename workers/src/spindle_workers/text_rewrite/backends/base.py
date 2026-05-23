"""BaseRewriter — pluggable LLM rewriter behind a uniform interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RewriteResult:
    """What a Rewriter returns. The worker wraps this into a JobResult."""

    text: str
    """The rewritten output."""

    usage: dict[str, Any] = field(default_factory=dict)
    """Provider-specific token / cost telemetry — e.g.
    {input_tokens: 1234, output_tokens: 567, model: "claude-opus-4-7"}.
    Lands in JobResult.runtime so it survives in Mongo for cost tracking."""


class BaseRewriter(ABC):
    """Pluggable LLM rewriter.

    Implementations should:
    - Accept an arbitrary system prompt + user text in ``rewrite``.
    - Return a ``RewriteResult`` with the rewritten string + usage stats.
    - Raise on network / API errors so the worker's retry policy handles it.
    """

    @abstractmethod
    def rewrite(
        self,
        text: str,
        *,
        system_prompt: str,
        max_tokens: int = 4096,
        **opts,
    ) -> RewriteResult: ...
