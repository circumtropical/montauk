"""Person, fact, and interaction ID generation (spec section 7, 10, 12)."""

from __future__ import annotations

import re
from collections.abc import Iterable

_SLUG_INVALID_RE = re.compile(r"[^a-z0-9]+")
_LOCAL_ID_RE_TEMPLATE = r"^{prefix}-(\d+)$"

PERSON_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def slugify(name: str) -> str:
    """Derive a lowercase, hyphenated slug from a display name."""
    slug = _SLUG_INVALID_RE.sub("-", name.strip().lower()).strip("-")
    if not slug:
        raise ValueError(f"cannot derive a slug from name: {name!r}")
    return slug


def next_person_id(name: str, existing_ids: Iterable[str]) -> str:
    """Assign a permanent person ID: the bare slug if free, otherwise the
    lowest available numeric suffix (spec section 7: mike-chen, mike-chen-2, ...).
    """
    base = slugify(name)
    existing = set(existing_ids)
    if base not in existing:
        return base
    suffix = 2
    while f"{base}-{suffix}" in existing:
        suffix += 1
    return f"{base}-{suffix}"


def _next_local_id(prefix: str, existing_ids: Iterable[str]) -> str:
    """Person-local IDs (fact-N, int-N) increment monotonically from the
    highest existing number and never reuse a number freed by removal, so a
    stale provenance reference never silently points at a different, later
    fact or interaction.
    """
    pattern = re.compile(_LOCAL_ID_RE_TEMPLATE.format(prefix=re.escape(prefix)))
    max_n = 0
    for existing_id in existing_ids:
        if m := pattern.match(existing_id):
            max_n = max(max_n, int(m.group(1)))
    return f"{prefix}-{max_n + 1}"


def next_fact_id(existing_fact_ids: Iterable[str]) -> str:
    return _next_local_id("fact", existing_fact_ids)


def next_interaction_id(existing_interaction_ids: Iterable[str]) -> str:
    return _next_local_id("int", existing_interaction_ids)
