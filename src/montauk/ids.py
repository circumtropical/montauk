"""Person, fact, and interaction ID generation (spec sections 7, 10, 12).

Person IDs are permanent, generic, sequential identifiers (``P0001``,
``P0002``, ...). They are assigned by Montauk, never derived from a
person's name, and never reused -- see :class:`montauk.markdown_store.PersonIdSequence`
for the concurrency-safe high-water-mark allocator. This module only
holds the pure format helpers; the allocator needs filesystem access and
lives with the canonical store.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_SLUG_INVALID_RE = re.compile(r"[^a-z0-9]+")
_LOCAL_ID_RE_TEMPLATE = r"^{prefix}-(\d+)$"

# Canonical generic person ID: uppercase P followed by at least four
# digits, zero-padded. Continues naturally past P9999 (P10000, ...).
PERSON_ID_RE = re.compile(r"^P[0-9]{4,}$")

_PERSON_ID_MIN_DIGITS = 4


def format_person_id(n: int) -> str:
    """Render sequence number ``n`` (1-based) as a canonical person ID."""
    if n < 1:
        raise ValueError(f"person id sequence number must be >= 1, got {n}")
    return f"P{n:0{_PERSON_ID_MIN_DIGITS}d}"


def person_id_number(person_id: str) -> int | None:
    """Inverse of :func:`format_person_id`: the integer inside a canonical
    person ID, or ``None`` if ``person_id`` is not canonical."""
    if not PERSON_ID_RE.match(person_id):
        return None
    return int(person_id[1:])


def slugify(name: str) -> str:
    """Derive a lowercase, hyphenated slug from a display name. Still used
    for *agent* credential IDs (auth.py); person IDs are generic and never
    name-derived (see :func:`format_person_id`)."""
    slug = _SLUG_INVALID_RE.sub("-", name.strip().lower()).strip("-")
    if not slug:
        raise ValueError(f"cannot derive a slug from name: {name!r}")
    return slug


def normalize_alias(value: str) -> str:
    """Fold an alias/name to its comparison key: trimmed, internal
    whitespace collapsed, case-insensitive. Used for alias de-duplication
    and lookup only -- the human-readable spelling is always stored as
    given."""
    return " ".join(value.split()).casefold()


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
