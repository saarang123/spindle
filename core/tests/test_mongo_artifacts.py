"""StateStore artifact-metadata tests against real Mongo."""

from __future__ import annotations

from uuid import uuid4

from spindle_core import ArtifactKind, ArtifactMeta
from spindle_core.state.mongo import MongoStateStore


def _make_artifact(**overrides) -> ArtifactMeta:  # type: ignore[no-untyped-def]
    defaults: dict[str, object] = {
        "job_id": uuid4(),
        "kind": ArtifactKind.IMAGE,
        "uri": "s3://spindle-artifacts/abc/output.png",
        "mime_type": "image/png",
        "size_bytes": 1024,
        "width": 1024,
        "height": 1024,
    }
    defaults.update(overrides)
    return ArtifactMeta(**defaults)  # type: ignore[arg-type]


async def test_record_and_get(state_store: MongoStateStore) -> None:
    art = _make_artifact()
    saved = await state_store.record_artifact(art)
    assert saved.id == art.id

    fetched = await state_store.get_artifact(art.id)
    assert fetched is not None
    assert fetched.uri == art.uri
    assert fetched.kind == ArtifactKind.IMAGE
    assert fetched.size_bytes == 1024


async def test_get_missing_returns_none(state_store: MongoStateStore) -> None:
    assert await state_store.get_artifact(uuid4()) is None


async def test_list_artifacts_for_job(state_store: MongoStateStore) -> None:
    job_a = uuid4()
    job_b = uuid4()
    a1 = _make_artifact(job_id=job_a, uri="s3://b/a1.png")
    a2 = _make_artifact(job_id=job_a, uri="s3://b/a2.png")
    b1 = _make_artifact(job_id=job_b, uri="s3://b/b1.png")
    orphan = _make_artifact(job_id=None, uri="s3://b/orphan.png")
    for art in (a1, a2, b1, orphan):
        await state_store.record_artifact(art)

    a_list = await state_store.list_artifacts_for_job(job_a)
    assert {a.id for a in a_list} == {a1.id, a2.id}

    b_list = await state_store.list_artifacts_for_job(job_b)
    assert {a.id for a in b_list} == {b1.id}


async def test_delete_artifact(state_store: MongoStateStore) -> None:
    art = _make_artifact()
    await state_store.record_artifact(art)

    assert await state_store.delete_artifact(art.id) is True
    assert await state_store.get_artifact(art.id) is None
    assert await state_store.delete_artifact(art.id) is False


async def test_artifact_with_lineage(state_store: MongoStateStore) -> None:
    """Artifacts with parent_artifact_ids round-trip the list."""
    parent_a = uuid4()
    parent_b = uuid4()
    art = _make_artifact(parent_artifact_ids=[parent_a, parent_b])
    await state_store.record_artifact(art)

    fetched = await state_store.get_artifact(art.id)
    assert fetched is not None
    assert fetched.parent_artifact_ids == [parent_a, parent_b]
