"""State backends — implementations of the StateStore protocol.

Use `make_state_store(settings)` to construct the backend selected by the
SPINDLE_STATE_BACKEND env var. Backends are imported lazily so missing optional
drivers don't blow up at import time.
"""

from __future__ import annotations

from spindle_core.settings import Settings
from spindle_core.state.protocol import IdempotencyConflictError, StateStore

__all__ = ["IdempotencyConflictError", "StateStore", "make_state_store"]


def make_state_store(settings: Settings) -> StateStore:
    match settings.state_backend:
        case "mongo":
            from spindle_core.state.mongo import MongoStateStore

            return MongoStateStore(
                settings.mongo_url,
                settings.mongo_db,
                validate_on_read=settings.state_validate_on_read,
            )
        case other:
            raise ValueError(f"unknown state backend: {other!r}")
