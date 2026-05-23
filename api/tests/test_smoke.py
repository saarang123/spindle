"""Smoke tests that don't require Mongo / Redis / MinIO.

Real end-to-end tests live with the spindle-wide smoke test (task #5).
These just verify the app constructs and the simple routes wire up.
"""
from __future__ import annotations

import pytest

from spindle_api.main import create_app


def test_create_app_with_no_backends_skips_lifespan() -> None:
    """When test fixtures inject backends, lifespan is bypassed."""

    class _Stub:
        pass

    app = create_app(state=_Stub(), queue=_Stub(), artifacts=_Stub())
    assert app.title == "Spindle API"


def test_health_route_wired() -> None:
    from fastapi.testclient import TestClient

    app = create_app(state=object(), queue=object(), artifacts=object())
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_get_job_returns_404_when_missing() -> None:
    """Lightweight check that the route is registered and returns 404 from
    a stub state store that returns None for any job_id."""
    from fastapi.testclient import TestClient

    class _State:
        async def get_job(self, _job_id):
            return None

    app = create_app(state=_State(), queue=object(), artifacts=object())
    with TestClient(app) as client:
        r = client.get("/jobs/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404
