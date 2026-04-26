"""Pydantic ↔ Mongo BSON document conversion.

Conventions:
  - The Pydantic model field `id` maps to the Mongo `_id` field.
  - UUIDs are kept as native Python `uuid.UUID`; the motor client encodes them
    as BSON Binary subtype 4 via uuidRepresentation="standard".
  - Datetimes stay as tz-aware UTC `datetime`; BSON Date is millisecond-precision.
  - StrEnums serialize to their string value (Pydantic v2 default in
    mode="python").
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def to_doc(model: BaseModel) -> dict[str, Any]:
    """Pydantic model → Mongo doc. Renames `id` to `_id`."""
    doc = model.model_dump(mode="python")
    if "id" in doc:
        doc["_id"] = doc.pop("id")
    return doc


def from_doc[ModelT: BaseModel](
    cls: type[ModelT], doc: dict[str, Any], *, validate: bool = True
) -> ModelT:
    """Mongo doc → Pydantic model. Renames `_id` to `id`.

    `validate` (default True) runs full Pydantic validation: type coercion,
    enum hydration, range checks. This is a few μs per Job — negligible at the
    scales Spindle targets.

    `validate=False` uses `model_construct`, which skips ALL validation. It is
    measurably faster but has sharp edges:
      - StrEnum fields stay as raw strings (`"queued"` instead of
        `JobStatus.QUEUED`), so identity comparisons against enum members
        will fail.
      - Nested models stay as dicts, not their model classes.
      - Numeric types are not coerced (e.g., int from a float).
    Only flip this off if you've measured `from_doc` as a hot path AND you're
    careful about how downstream code consumes the returned object.
    """
    data = dict(doc)
    if "_id" in data:
        data["id"] = data.pop("_id")
    if validate:
        return cls.model_validate(data)
    return cls.model_construct(**data)
