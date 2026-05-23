"""Lease — a time-bounded claim on a job by a worker.

Leases are not stored as separate documents. Their fields live on the Job
(lease_id, lease_expires_at, assigned_worker_id). This class is the in-memory
representation passed back from acquire_lease() so callers don't have to
re-read the Job to learn what they just acquired.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class Lease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    job_id: UUID
    worker_id: str
    expires_at: datetime
