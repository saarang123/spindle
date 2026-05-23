"""Text-rewrite workers — LLM-driven text transformations.

Generic shape: given a source text + a system prompt (or prompt template),
produce a rewritten string. The first concrete use case is podcast-this's
deep-tech-doc → spoken-narration rewrite, but the worker is content-agnostic.
"""
