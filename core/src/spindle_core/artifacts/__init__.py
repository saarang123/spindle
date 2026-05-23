"""Artifact backends — implementations of the ArtifactStore protocol.

Use `make_artifact_store(settings)` to construct the backend selected by
SPINDLE_ARTIFACT_BACKEND. Backends are imported lazily so missing optional
drivers don't blow up at import time.
"""

from __future__ import annotations

from spindle_core.artifacts.protocol import ArtifactStat, ArtifactStore
from spindle_core.settings import Settings

__all__ = ["ArtifactStat", "ArtifactStore", "make_artifact_store"]


def make_artifact_store(settings: Settings) -> ArtifactStore:
    match settings.artifact_backend:
        case "s3":
            from spindle_core.artifacts.s3 import S3ArtifactStore

            return S3ArtifactStore(
                endpoint=settings.s3_endpoint,
                bucket=settings.s3_bucket,
                access_key=settings.s3_access_key.get_secret_value(),
                secret_key=settings.s3_secret_key.get_secret_value(),
                region=settings.s3_region,
            )
        case "memory":
            from spindle_core.artifacts.memory import MemoryArtifactStore

            return MemoryArtifactStore()
        case other:
            raise ValueError(f"unknown artifact backend: {other!r}")
