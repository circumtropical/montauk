from pathlib import Path

import pytest
from pydantic import ValidationError

from montauk.markdown_store import (
    MarkdownFormatError,
    MarkdownStore,
    PersonNotFoundError,
    markdown_to_person,
    person_to_markdown,
)
from montauk.models import ContactInfo, Fact, Interaction, Person

SPEC_EXAMPLE = """---
id: P0001
name: Mike Chen
aliases:
  - Michael Chen
birthday: 1982-04-17
location: "Normally Boston; often vacations in Puerto Rico; last known in New Zealand"
company: Acme Robotics
job_title: VP Engineering
desired_contact_cadence_days: 90
summary: >
  Stanford acquaintance working in robotics. Met through a technology event.
contact:
  emails:
    - mike@example.com
  phones:
    - "+1-207-555-0100"
  messaging:
    telegram: "@mikechen"
---

# Mike Chen

## Family

- id: fact-1
  date: 2025
  confidence: high
  text: Mike is married to Jane.
  sources:
    - type: interaction
      id: int-1

## Work & Education

- id: fact-2
  date: 2026-08
  confidence: medium
  text: Mike may be considering leaving Acme Robotics.
  sources:
    - type: agent
      id: personal-assistant

## Interests

## Relationship with User

- id: fact-3
  date: 2025
  confidence: high
  text: Met Mike at a Stanford-related robotics event.

## Life Events

## General Notes

## Interactions

### int-1
- date: 2026-08-12
- channel: in-person
- connection_level: 4
- summary: Had lunch with Mike.
- sources:
  - type: agent
    id: personal-assistant
"""

SIMPSONS_DIR = Path(__file__).parent.parent / "examples" / "simpsons"


class TestSpecExampleRoundTrip:
    def test_parses_spec_example(self):
        person = markdown_to_person(SPEC_EXAMPLE, source_path="P0001.md")
        assert person.id == "P0001"
        assert person.name == "Mike Chen"
        assert person.aliases == ["Michael Chen"]
        assert person.birthday.to_string() == "1982-04-17"
        assert person.company == "Acme Robotics"
        assert person.desired_contact_cadence_days == 90
        assert [f.id for f in person.facts] == ["fact-1", "fact-2", "fact-3"]
        assert person.get_fact("fact-1").category == "Family"
        assert person.get_fact("fact-2").category == "Work & Education"
        assert person.get_fact("fact-2").confidence.value == "medium"
        assert person.get_fact("fact-1").sources[0].type == "interaction"
        assert person.get_fact("fact-1").sources[0].id == "int-1"
        assert len(person.interactions) == 1
        i = person.interactions[0]
        assert i.id == "int-1"
        assert i.date.to_string() == "2026-08-12"
        assert i.channel == "in-person"
        assert i.connection_level == 4
        assert i.summary == "Had lunch with Mike."
        assert i.sources[0].type == "agent" and i.sources[0].id == "personal-assistant"

    def test_round_trip_is_semantically_stable(self):
        person = markdown_to_person(SPEC_EXAMPLE, source_path="P0001.md")
        regenerated_text = person_to_markdown(person)
        person2 = markdown_to_person(regenerated_text, source_path="round-trip.md")
        assert person == person2

    def test_round_trip_text_is_byte_stable_after_first_pass(self):
        # The very first regeneration may reformat cosmetic YAML style
        # (quoting, folded-scalar whitespace), but every subsequent
        # regeneration of an unchanged Person must be byte-identical --
        # this is what keeps content-hash-based index reconciliation and
        # diff-based Git snapshots from seeing spurious "changes".
        person = markdown_to_person(SPEC_EXAMPLE, source_path="P0001.md")
        once = person_to_markdown(person)
        twice = person_to_markdown(markdown_to_person(once, source_path="x.md"))
        assert once == twice


class TestMalformedInput:
    def test_missing_front_matter(self):
        with pytest.raises(MarkdownFormatError, match="missing YAML front matter"):
            markdown_to_person("# No front matter here\n", source_path="bad.md")

    def test_invalid_yaml_in_front_matter(self):
        text = "---\nid: [unterminated\n---\n\n# X\n"
        with pytest.raises(MarkdownFormatError, match="front matter"):
            markdown_to_person(text, source_path="bad.md")

    def test_front_matter_must_be_mapping(self):
        text = "---\n- just\n- a\n- list\n---\n\n# X\n"
        with pytest.raises(MarkdownFormatError, match="must be a YAML mapping"):
            markdown_to_person(text, source_path="bad.md")

    def test_unrecognized_heading(self):
        text = "---\nid: x\nname: X\n---\n\n# X\n\n## Not A Real Category\n"
        with pytest.raises(MarkdownFormatError, match="unrecognized section heading"):
            markdown_to_person(text, source_path="bad.md")

    def test_duplicate_heading(self):
        text = "---\nid: x\nname: X\n---\n\n# X\n\n## Family\n\n## Family\n"
        with pytest.raises(MarkdownFormatError, match="duplicate section heading"):
            markdown_to_person(text, source_path="bad.md")

    def test_category_section_must_be_a_list(self):
        text = "---\nid: x\nname: X\n---\n\n# X\n\n## Family\nnot: a-list\n"
        with pytest.raises(MarkdownFormatError, match="must be a YAML list"):
            markdown_to_person(text, source_path="bad.md")

    def test_malformed_interaction_field_entry(self):
        text = (
            "---\nid: x\nname: X\n---\n\n# X\n\n## Interactions\n\n"
            "### int-1\n- date: 2026\n- not_single_key: a\n  extra: b\n"
        )
        with pytest.raises(MarkdownFormatError, match="malformed field entry"):
            markdown_to_person(text, source_path="bad.md")

    def test_unclosed_quote_produces_diagnosable_yaml_error(self):
        # Mirrors the deliberately broken examples/simpsons fixture file.
        content = (SIMPSONS_DIR / "people" / "P0001.md").read_text()
        with pytest.raises(MarkdownFormatError) as exc_info:
            markdown_to_person(content, source_path="P0001.md")
        message = str(exc_info.value)
        assert "P0001.md" in message
        assert "line" in message  # underlying PyYAML error includes line/column

    def test_semantically_invalid_field_raises_pydantic_error_not_format_error(self):
        # Well-formed YAML/headings, but an invalid domain value (bad category)
        # should surface as a pydantic ValidationError, not MarkdownFormatError.
        text = (
            "---\nid: x\nname: X\n---\n\n# X\n\n## Family\n"
            "- id: not-a-valid-fact-id\n  confidence: high\n  text: hi\n"
        )
        with pytest.raises(ValidationError):
            markdown_to_person(text, source_path="bad.md")


class TestSimpsonsFixtureParses:
    # P0001 barney (malformed), P0002 gil #2, P0003 gil #1, P0004 homer,
    # P0005 marge, P0006 milhouse, P0007 moe, P0008 ned; archive P0009 frank.
    @pytest.mark.parametrize(
        "filename",
        ["P0003.md", "P0004.md", "P0005.md", "P0006.md", "P0007.md", "P0008.md"],
    )
    def test_active_person_parses(self, filename):
        content = (SIMPSONS_DIR / "people" / filename).read_text()
        person = markdown_to_person(content, source_path=filename)
        assert person.id.startswith("P")
        assert person.name

    def test_archived_person_parses(self):
        content = (SIMPSONS_DIR / "archive" / "P0009.md").read_text()
        person = markdown_to_person(content, source_path="P0009.md")
        assert person.id == "P0009"
        assert person.name == "Frank Grimes"

    def test_barney_gumble_is_deliberately_malformed(self):
        content = (SIMPSONS_DIR / "people" / "P0001.md").read_text()
        with pytest.raises(MarkdownFormatError):
            markdown_to_person(content, source_path="P0001.md")

    def test_gil_gunderson_pair_share_a_display_name(self):
        p1 = markdown_to_person((SIMPSONS_DIR / "people" / "P0003.md").read_text())
        p2 = markdown_to_person((SIMPSONS_DIR / "people" / "P0002.md").read_text())
        assert p1.name == p2.name == "Gil Gunderson"
        assert p1.id != p2.id

    def test_moe_has_cadence_but_no_interactions(self):
        p = markdown_to_person((SIMPSONS_DIR / "people" / "P0007.md").read_text())
        assert p.desired_contact_cadence_days is not None
        assert p.interactions == []

    def test_milhouse_is_a_minimal_bare_record(self):
        p = markdown_to_person((SIMPSONS_DIR / "people" / "P0006.md").read_text())
        assert p.birthday is None
        assert p.desired_contact_cadence_days is None
        assert p.summary is None
        assert p.facts == []
        assert p.interactions == []

    def test_ned_flanders_birthday_has_no_year(self):
        p = markdown_to_person((SIMPSONS_DIR / "people" / "P0008.md").read_text())
        assert p.birthday.year is None
        assert (p.birthday.month, p.birthday.day) == (5, 11)


class TestMarkdownStore:
    def _store(self, tmp_path: Path) -> MarkdownStore:
        return MarkdownStore(tmp_path / "data")

    def _person(self, **overrides) -> Person:
        defaults = dict(id="P0001", name="Lisa Simpson")
        defaults.update(overrides)
        return Person(**defaults)

    def test_write_then_read_round_trip(self, tmp_path):
        store = self._store(tmp_path)
        person = self._person(
            summary="Plays the saxophone.",
            facts=[Fact(id="fact-1", category="Interests", text="Plays saxophone.")],
            interactions=[Interaction(id="int-1", date="2026-08-01", summary="Talked about jazz.")],
        )
        store.write_person(person)
        assert store.exists("P0001")
        loaded = store.read_person("P0001")
        assert loaded == person

    def test_read_missing_person_raises(self, tmp_path):
        store = self._store(tmp_path)
        with pytest.raises(PersonNotFoundError):
            store.read_person("nobody")

    def test_list_person_ids(self, tmp_path):
        store = self._store(tmp_path)
        store.write_person(self._person(id="P0002", name="Bart Simpson"))
        store.write_person(self._person(id="P0001", name="Lisa Simpson"))
        assert store.list_person_ids() == ["P0001", "P0002"]

    def test_archive_moves_file_out_of_people_dir(self, tmp_path):
        store = self._store(tmp_path)
        store.write_person(self._person())
        store.archive_person("P0001")
        assert not store.exists("P0001")
        assert store.is_archived("P0001")
        assert store.list_person_ids() == []
        assert store.list_archived_person_ids() == ["P0001"]

    def test_archive_missing_person_raises(self, tmp_path):
        store = self._store(tmp_path)
        with pytest.raises(PersonNotFoundError):
            store.archive_person("nobody")

    def test_restore_moves_file_back_to_people_dir(self, tmp_path):
        store = self._store(tmp_path)
        store.write_person(self._person())
        store.archive_person("P0001")
        store.restore_person("P0001")
        assert store.exists("P0001")
        assert not store.is_archived("P0001")

    def test_overwrite_existing_person(self, tmp_path):
        store = self._store(tmp_path)
        store.write_person(self._person(summary="Original summary."))
        store.write_person(self._person(summary="Updated summary."))
        assert store.read_person("P0001").summary == "Updated summary."

    def test_write_does_not_leave_temp_files_behind(self, tmp_path):
        store = self._store(tmp_path)
        store.write_person(self._person())
        remaining = list(store.people_dir.glob(".*"))
        assert remaining == []

    def test_contact_info_round_trips(self, tmp_path):
        store = self._store(tmp_path)
        person = self._person(
            contact=ContactInfo(
                emails=["lisa@example.com"],
                phones=["+1-555-0100"],
                address="742 Evergreen Terrace",
                messaging={"telegram": "@lisa"},
            )
        )
        store.write_person(person)
        loaded = store.read_person("P0001")
        assert loaded.contact == person.contact
