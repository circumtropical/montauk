"""Structured JSON export of a person or a whole workspace (spec 26.4).

Excludes secrets (passwords, sessions, tokens, connector credentials);
this increment has no connector data, and person records carry none.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

from ..models import Person

EXPORT_SCHEMA_VERSION = "montauk.person.v2"


def person_to_dict(person: Person) -> dict[str, Any]:
    """Deterministic, key-ordered dict -- facts/interactions already sorted
    by numeric local id upstream. Suitable for hashing and diffing."""
    data = person.model_dump(mode="json", exclude_none=True)
    data["schema"] = EXPORT_SCHEMA_VERSION
    return data


def person_to_json(person: Person, *, indent: int | None = 2) -> str:
    return json.dumps(person_to_dict(person), indent=indent, sort_keys=True, ensure_ascii=False)


def canonical_json_hash(person: Person) -> str:
    payload = json.dumps(person_to_dict(person), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def workspace_export(
    *,
    workspace_name: str,
    workspace_slug: str,
    active: list[Person],
    archived: list[Person],
    revisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema": "montauk.workspace.v2",
        "exported_at": dt.datetime.now(dt.UTC).isoformat(),
        "workspace": {"name": workspace_name, "slug": workspace_slug},
        "people": {
            "active": [person_to_dict(p) for p in active],
            "archived": [person_to_dict(p) for p in archived],
        },
        "revision_history": revisions or [],
    }
