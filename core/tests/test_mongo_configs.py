"""ModelConfig storage tests against real Mongo."""

from __future__ import annotations

from spindle_core import ModelConfig
from spindle_core.state.mongo import MongoStateStore


def _make_config(**overrides) -> ModelConfig:  # type: ignore[no-untyped-def]
    defaults: dict[str, object] = {
        "id": "qwen-text-v1",
        "name": "Qwen text",
        "version": "v1",
        "job_types": ["text.generate"],
        "preferred_node": "control",
        "runtime_backend": "mlx",
        "model_ref": "local/qwen-a3b",
    }
    defaults.update(overrides)
    return ModelConfig(**defaults)  # type: ignore[arg-type]


async def test_upsert_and_get(state_store: MongoStateStore) -> None:
    cfg = _make_config()
    saved = await state_store.upsert_config(cfg)
    assert saved.id == "qwen-text-v1"

    fetched = await state_store.get_config("qwen-text-v1")
    assert fetched is not None
    assert fetched.runtime_backend == "mlx"
    assert fetched.job_types == ["text.generate"]


async def test_get_missing_returns_none(state_store: MongoStateStore) -> None:
    assert await state_store.get_config("nope") is None


async def test_upsert_replaces_existing_preserves_created_at(
    state_store: MongoStateStore,
) -> None:
    cfg = _make_config()
    first = await state_store.upsert_config(cfg)
    original_created_at = first.created_at

    # Re-upsert with a changed param — should overwrite mutable fields
    # but keep created_at.
    cfg2 = _make_config(name="Qwen text v2")
    second = await state_store.upsert_config(cfg2)

    assert second.created_at == original_created_at
    assert second.name == "Qwen text v2"
    assert second.updated_at >= original_created_at


async def test_list_configs_active_only(state_store: MongoStateStore) -> None:
    active = _make_config(id="a", is_active=True)
    inactive = _make_config(id="b", is_active=False)
    await state_store.upsert_config(active)
    await state_store.upsert_config(inactive)

    listed = await state_store.list_configs(active_only=True)
    assert {c.id for c in listed} == {"a"}

    all_listed = await state_store.list_configs(active_only=False)
    assert {c.id for c in all_listed} == {"a", "b"}


async def test_list_configs_filters_by_node(state_store: MongoStateStore) -> None:
    on_control = _make_config(id="a", preferred_node="control")
    on_gpu = _make_config(id="b", preferred_node="gpu")
    any_node = _make_config(id="c", preferred_node=None)
    for c in (on_control, on_gpu, any_node):
        await state_store.upsert_config(c)

    control_set = await state_store.list_configs(node="control")
    # Should include "a" (preferred_node=control) and "c" (preferred_node=None).
    assert {c.id for c in control_set} == {"a", "c"}


async def test_delete_config(state_store: MongoStateStore) -> None:
    cfg = _make_config()
    await state_store.upsert_config(cfg)

    assert await state_store.delete_config("qwen-text-v1") is True
    assert await state_store.get_config("qwen-text-v1") is None
    # Deleting a missing one returns False.
    assert await state_store.delete_config("qwen-text-v1") is False
