"""Thin HTTP client for the worker → API lifecycle endpoints.

Workers POST start / complete / fail here. The dispatcher does NOT use this —
it talks directly to ``StateStore`` and ``JobQueue``.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import httpx

from spindle_core.types.artifact import ArtifactMeta
from spindle_core.types.errors import ErrorPayload

log = logging.getLogger("spindle_workers")


class ApiClient:
    """Async client for ``spindle-api``'s worker-facing endpoints."""

    def __init__(
        self,
        base_url: str,
        auth_token: str | None = None,
        *,
        timeout_s: float = 30.0,
    ) -> None:
        headers = (
            {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
        )
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=timeout_s,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def start(
        self,
        job_id: UUID,
        lease_id: UUID,
        worker_id: str,
        attempt_id: UUID,
    ) -> dict[str, Any]:
        return await self._post(
            f"/jobs/{job_id}/start",
            {
                "lease_id": str(lease_id),
                "worker_id": worker_id,
                "attempt_id": str(attempt_id),
            },
        )

    async def complete(
        self,
        job_id: UUID,
        lease_id: UUID,
        worker_id: str,
        *,
        output: dict[str, Any],
        artifacts: list[ArtifactMeta],
        runtime: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._post(
            f"/jobs/{job_id}/complete",
            {
                "lease_id": str(lease_id),
                "worker_id": worker_id,
                "output": output,
                "artifacts": [a.model_dump(mode="json") for a in artifacts],
                "runtime": runtime,
            },
        )

    async def fail(
        self,
        job_id: UUID,
        lease_id: UUID,
        worker_id: str,
        *,
        error: ErrorPayload,
        runtime: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._post(
            f"/jobs/{job_id}/fail",
            {
                "lease_id": str(lease_id),
                "worker_id": worker_id,
                "error": error.model_dump(mode="json"),
                "runtime": runtime or {},
            },
        )

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            r = await self._client.post(path, json=payload)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            log.warning("API POST %s failed: %s", path, e)
            raise
