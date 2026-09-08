"""Canonical Markdown export of a person record (spec 24, 26.4).

Markdown is an export artifact in Phase 2, not canonical storage. The
serializer is the exact Phase 1 one, so a migrated record exports
byte-identically to its canonical Phase 1 form -- the property the
migration verification relies on (spec 32.6).
"""

from __future__ import annotations

from ..markdown_store import person_to_markdown as _person_to_markdown
from ..models import Person


def person_to_markdown(person: Person) -> str:
    return _person_to_markdown(person)


def canonical_markdown_hash(person: Person) -> str:
    import hashlib

    return hashlib.sha256(person_to_markdown(person).encode("utf-8")).hexdigest()
