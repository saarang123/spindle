"""Claude rewrite worker subpackage.

Entry point: ``python -m spindle_workers.text_rewrite.claude``.

Requires the ``[text_rewrite_claude]`` extra (pulls ``anthropic``) and
``ANTHROPIC_API_KEY`` in the worker's environment.
"""
from .worker import ClaudeTextRewriteWorker

__all__ = ["ClaudeTextRewriteWorker"]
