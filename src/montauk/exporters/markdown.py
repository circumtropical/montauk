"""Canonical Markdown serialization of a person record (spec 24, 26.4).

Markdown is an export artifact in Phase 2, not canonical storage: this is
the reader-facing rendering used by ``get_full_record`` and the memory
fingerprint that decides briefing-cache staleness. Output is
deterministic -- facts and interactions are emitted in fixed category
order, sorted by their numeric local id -- so an unchanged record always
renders byte-identical text.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import yaml

from ..models import Fact, Interaction, Person
from ..schema import CATEGORIES

INTERACTIONS_HEADING = "Interactions"
_LOCAL_ID_SORT_RE = re.compile(r"-(\d+)$")


class _IndentedDumper(yaml.SafeDumper):
    """PyYAML's default dumper doesn't indent block sequences nested under a
    mapping key. Override so canonical output matches the illustrated
    format and stays comfortable to hand-read."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)


def _yaml_dump(data: Any) -> str:
    return yaml.dump(
        data,
        Dumper=_IndentedDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )


def _local_id_sort_key(local_id: str) -> tuple[int, str]:
    if m := _LOCAL_ID_SORT_RE.search(local_id):
        return (int(m.group(1)), local_id)
    return (10**9, local_id)


def _front_matter_dict(person: Person) -> dict[str, Any]:
    fm: dict[str, Any] = {"id": person.id, "name": person.name}
    if person.aliases:
        fm["aliases"] = list(person.aliases)
    if person.birthday is not None:
        fm["birthday"] = person.birthday.to_string()
    if person.location:
        fm["location"] = person.location
    if person.company:
        fm["company"] = person.company
    if person.job_title:
        fm["job_title"] = person.job_title
    if person.desired_contact_cadence_days is not None:
        fm["desired_contact_cadence_days"] = person.desired_contact_cadence_days
    if person.summary:
        fm["summary"] = person.summary
    contact = person.contact
    contact_dict: dict[str, Any] = {}
    if contact.emails:
        contact_dict["emails"] = list(contact.emails)
    if contact.phones:
        contact_dict["phones"] = list(contact.phones)
    if contact.address:
        contact_dict["address"] = contact.address
    if contact.messaging:
        contact_dict["messaging"] = dict(contact.messaging)
    if contact_dict:
        fm["contact"] = contact_dict
    return fm


def _fact_dict(fact: Fact) -> dict[str, Any]:
    d: dict[str, Any] = {"id": fact.id}
    if fact.date is not None:
        d["date"] = fact.date.to_string()
    d["confidence"] = fact.confidence.value
    d["text"] = fact.text
    if fact.related_person_id:
        d["related_person_id"] = fact.related_person_id
    if fact.sources:
        d["sources"] = [{"type": s.type, "id": s.id} for s in fact.sources]
    return d


def _interaction_items(interaction: Interaction) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = [{"date": interaction.date.to_string()}]
    if interaction.channel:
        items.append({"channel": interaction.channel})
    if interaction.connection_level is not None:
        items.append({"connection_level": interaction.connection_level})
    if interaction.summary:
        items.append({"summary": interaction.summary})
    if interaction.sources:
        items.append({"sources": [{"type": s.type, "id": s.id} for s in interaction.sources]})
    return items


def person_to_markdown(person: Person) -> str:
    """Serialize a Person to its canonical Markdown representation."""
    front_matter = _yaml_dump(_front_matter_dict(person)).rstrip("\n")
    lines = ["---", front_matter, "---", "", f"# {person.name}", ""]

    facts_by_category: dict[str, list[Fact]] = {c: [] for c in CATEGORIES}
    for fact in person.facts:
        facts_by_category[fact.category].append(fact)

    for category in CATEGORIES:
        lines.append(f"## {category}")
        lines.append("")
        cat_facts = sorted(facts_by_category[category], key=lambda f: _local_id_sort_key(f.id))
        if cat_facts:
            lines.append(_yaml_dump([_fact_dict(f) for f in cat_facts]).rstrip("\n"))
            lines.append("")

    lines.append(f"## {INTERACTIONS_HEADING}")
    lines.append("")
    for interaction in sorted(person.interactions, key=lambda i: _local_id_sort_key(i.id)):
        lines.append(f"### {interaction.id}")
        lines.append(_yaml_dump(_interaction_items(interaction)).rstrip("\n"))
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


def canonical_markdown_hash(person: Person) -> str:
    return hashlib.sha256(person_to_markdown(person).encode("utf-8")).hexdigest()
