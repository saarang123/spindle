"""Rewrite backends — pluggable LLM providers behind BaseRewriter."""
from .base import BaseRewriter, RewriteResult

__all__ = ["BaseRewriter", "RewriteResult"]
