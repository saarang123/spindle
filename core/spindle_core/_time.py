"""Time helpers used across the package.

Centralized so we don't have a private `_now` referenced across modules.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return a tz-aware UTC datetime. Use everywhere instead of
    `datetime.utcnow()` (deprecated, returns naive) or
    `datetime.now()` (uses local timezone)."""
    return datetime.now(UTC)
