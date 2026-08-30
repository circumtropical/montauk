"""Canonical Markdown person-file read/write (spec section 8, Appendix B).

The canonical format is YAML front matter plus fixed `## ` Markdown
headings, where each heading's body is itself a valid YAML document (a
list of fact mappings for the six fixed categories; a list of
single-key field mappings per `### int-N` sub-heading under
`## Interactions`, merged into one dict). The server always regenerates
a person's entire file canonically on every write (never edits in
place), so no partial round-trip/formatting-preservation machinery is
needed here.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Any

import yaml

from .models import Fact, Interaction, Person
from .schema import CATEGORIES

INTERACTIONS_HEADING = "Interactions"
_KNOWN_HEADINGS = (*CATEGORIES, INTERACTIONS_HEADING)

_FRONT_MATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)
_HEADING2_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_HEADING3_RE = re.compile(r"^###[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_LOCAL_ID_SORT_RE = re.compile(r"-(\d+)$")


class MarkdownFormatError(Exception):
    """The file's Markdown/YAML structure could not be parsed at all, as
    opposed to parsing fine but failing domain validation (pydantic.ValidationError)."""


class PersonNotFoundError(Exception):
    def __init__(self, person_id: str, *, archived: bool = False):
        self.person_id = person_id
        self.archived = archived
        where = "archive/" if archived else "people/"
        super().__init__(f"person {person_id!r} not found in {where}")


class _IndentedDumper(yaml.SafeDumper):
    """PyYAML's default dumper doesn't indent block sequences nested under a
    mapping key (`sources:\\n- type: ...` instead of `sources:\\n  - type:
    ...`). Override so canonical output matches the spec's illustrated
    format and stays comfortable to hand-edit."""

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


def _yaml_load(text: str, *, context: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise MarkdownFormatError(f"invalid YAML in {context}: {exc}") from exc


def _local_id_sort_key(local_id: str) -> tuple[int, str]:
    if m := _LOCAL_ID_SORT_RE.search(local_id):
        return (int(m.group(1)), local_id)
    return (10**9, local_id)


# --- parsing -----------------------------------------------------------


def _split_sections(body: str) -> list[tuple[str, str]]:
    """Split the body into (heading, content) for each `## ` section, in
    file order. Content before the first `## ` (the `# Name` title line)
    is discarded; the title is regenerated from `name` on serialize."""
    matches = list(_HEADING2_RE.finditer(body))
    sections = []
    for idx, m in enumerate(matches):
        heading = m.group(1).strip()
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        sections.append((heading, body[start:end]))
    return sections


def _parse_facts_section(heading: str, content: str, *, context: str) -> list[dict]:
    data = _yaml_load(content, context=f"{context} section '## {heading}'")
    if data is None:
        return []
    if not isinstance(data, list):
        raise MarkdownFormatError(
            f"{context}: section '## {heading}' must be a YAML list of facts, got {type(data).__name__}"
        )
    for item in data:
        if not isinstance(item, dict):
            raise MarkdownFormatError(f"{context}: section '## {heading}' contains a non-mapping fact entry: {item!r}")
    return data


def _parse_interactions_section(content: str, *, context: str) -> list[dict]:
    interactions: list[dict] = []
    matches = list(_HEADING3_RE.finditer(content))
    for idx, m in enumerate(matches):
        interaction_id = m.group(1).strip()
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(content)
        block = content[start:end]
        items = _yaml_load(block, context=f"{context} interaction '{interaction_id}'")
        merged: dict[str, Any] = {"id": interaction_id}
        if items is not None:
            if not isinstance(items, list):
                raise MarkdownFormatError(
                    f"{context}: interaction '{interaction_id}' must be a YAML list of fields, "
                    f"got {type(items).__name__}"
                )
            for item in items:
                if not isinstance(item, dict) or len(item) != 1:
                    raise MarkdownFormatError(
                        f"{context}: interaction '{interaction_id}' has a malformed field entry: {item!r}"
                    )
                merged.update(item)
        interactions.append(merged)
    return interactions


def markdown_to_person(text: str, *, source_path: str | None = None) -> Person:
    """Parse a canonical person Markdown file into a validated Person.

    Raises MarkdownFormatError for structurally malformed YAML/headings,
    or pydantic.ValidationError for well-formed-but-invalid field values.
    """
    context = source_path or "<string>"
    m = _FRONT_MATTER_RE.match(text)
    if not m:
        raise MarkdownFormatError(f"{context}: missing YAML front matter (expected leading '---' ... '---' block)")
    front_matter_text, body = m.groups()
    front_matter = _yaml_load(front_matter_text, context=f"{context} front matter")
    if not isinstance(front_matter, dict):
        raise MarkdownFormatError(f"{context}: front matter must be a YAML mapping")

    facts: list[dict] = []
    interactions: list[dict] = []
    seen_headings: set[str] = set()
    for heading, content in _split_sections(body):
        if heading in seen_headings:
            raise MarkdownFormatError(f"{context}: duplicate section heading '## {heading}'")
        seen_headings.add(heading)
        if heading == INTERACTIONS_HEADING:
            interactions = _parse_interactions_section(content, context=context)
        elif heading in CATEGORIES:
            for fact in _parse_facts_section(heading, content, context=context):
                fact = dict(fact)
                fact["category"] = heading
                facts.append(fact)
        else:
            raise MarkdownFormatError(
                f"{context}: unrecognized section heading '## {heading}'; expected one of {_KNOWN_HEADINGS}"
            )

    payload = dict(front_matter)
    payload["facts"] = facts
    payload["interactions"] = interactions
    return Person.model_validate(payload)


# --- serializing ---------------------------------------------------------


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
    """Serialize a Person to its canonical Markdown representation.

    Output is deterministic (facts/interactions sorted by their numeric
    local ID within fixed category order) so an unchanged Person always
    regenerates byte-identical text -- this keeps content-hash-based
    index reconciliation and diff-based Git snapshots working correctly.
    """
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


# --- atomic write ----------------------------------------------------------


def atomic_write_text(path: Path, content: str) -> None:
    """Write `content` to `path` atomically: write to a temp file in the
    same directory, fsync the file and directory entry, then atomically
    rename into place (spec Appendix B). Never edits `path` in place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# --- store -----------------------------------------------------------------


class MarkdownStore:
    """Canonical read/write access to a deployment's people/ and archive/
    Markdown directories."""

    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir)
        self.people_dir = self.data_dir / "people"
        self.archive_dir = self.data_dir / "archive"
        self.people_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def person_path(self, person_id: str) -> Path:
        return self.people_dir / f"{person_id}.md"

    def archived_person_path(self, person_id: str) -> Path:
        return self.archive_dir / f"{person_id}.md"

    def exists(self, person_id: str) -> bool:
        return self.person_path(person_id).exists()

    def is_archived(self, person_id: str) -> bool:
        return self.archived_person_path(person_id).exists()

    def list_person_ids(self) -> list[str]:
        return sorted(p.stem for p in self.people_dir.glob("*.md"))

    def list_archived_person_ids(self) -> list[str]:
        return sorted(p.stem for p in self.archive_dir.glob("*.md"))

    def read_person(self, person_id: str) -> Person:
        path = self.person_path(person_id)
        if not path.exists():
            raise PersonNotFoundError(person_id)
        return markdown_to_person(path.read_text(encoding="utf-8"), source_path=str(path))

    def read_archived_person(self, person_id: str) -> Person:
        path = self.archived_person_path(person_id)
        if not path.exists():
            raise PersonNotFoundError(person_id, archived=True)
        return markdown_to_person(path.read_text(encoding="utf-8"), source_path=str(path))

    def write_person(self, person: Person) -> None:
        """Atomically (re)write a person's canonical file in people/.

        `person` is a pydantic model, so field-level and duplicate-ID
        invariants are already enforced by construction; repository-wide
        checks (e.g. unique person IDs across the whole store) are layered
        on by schema.validate_person / reconciliation.py.
        """
        atomic_write_text(self.person_path(person.id), person_to_markdown(person))

    def archive_person(self, person_id: str) -> None:
        src = self.person_path(person_id)
        if not src.exists():
            raise PersonNotFoundError(person_id)
        os.rename(src, self.archived_person_path(person_id))

    def restore_person(self, person_id: str) -> None:
        src = self.archived_person_path(person_id)
        if not src.exists():
            raise PersonNotFoundError(person_id, archived=True)
        os.rename(src, self.person_path(person_id))
