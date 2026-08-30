import pytest
from pydantic import ValidationError

from montauk.dates import Birthday, FlexDate
from montauk.models import ContactInfo, Fact, Interaction, Person, Source
from montauk.schema import Confidence


class TestFact:
    def test_valid_fact(self):
        f = Fact(id="fact-1", category="Family", text="Mike is married to Jane.")
        assert f.confidence == Confidence.HIGH  # default
        assert f.date is None

    def test_fact_with_date_and_sources(self):
        f = Fact(
            id="fact-2",
            category="Work & Education",
            date="2026-08",
            confidence="medium",
            text="Mike may be considering leaving Acme Robotics.",
            sources=[Source(type="agent", id="personal-assistant")],
        )
        assert isinstance(f.date, FlexDate)
        assert f.date.to_string() == "2026-08"
        assert f.confidence == Confidence.MEDIUM

    def test_blank_date_becomes_none(self):
        f = Fact(id="fact-1", category="Family", text="x", date="")
        assert f.date is None

    def test_rejects_bad_id_format(self):
        with pytest.raises(ValidationError):
            Fact(id="not-a-fact-id", category="Family", text="x")

    def test_rejects_invalid_category(self):
        with pytest.raises(ValidationError):
            Fact(id="fact-1", category="Nonexistent Category", text="x")

    def test_rejects_empty_text(self):
        with pytest.raises(ValidationError):
            Fact(id="fact-1", category="Family", text="   ")


class TestInteraction:
    def test_valid_interaction(self):
        i = Interaction(id="int-1", date="2026-08-12", channel="in-person", connection_level=4)
        assert i.date.to_string() == "2026-08-12"

    def test_requires_date(self):
        with pytest.raises(ValidationError):
            Interaction(id="int-1")

    def test_rejects_out_of_range_connection_level(self):
        with pytest.raises(ValidationError):
            Interaction(id="int-1", date="2026", connection_level=7)

    def test_rejects_bad_id_format(self):
        with pytest.raises(ValidationError):
            Interaction(id="interaction-1", date="2026")


class TestPerson:
    def _person(self, **overrides):
        defaults = dict(id="mike-chen-2", name="Mike Chen")
        defaults.update(overrides)
        return Person(**defaults)

    def test_minimal_person(self):
        p = self._person()
        assert p.facts == []
        assert p.interactions == []
        assert isinstance(p.contact, ContactInfo)

    def test_full_person_from_spec_example(self):
        p = Person(
            id="mike-chen-2",
            name="Mike Chen",
            aliases=["Michael Chen"],
            birthday="1982-04-17",
            location="Normally Boston; often vacations in Puerto Rico; last known in New Zealand",
            company="Acme Robotics",
            job_title="VP Engineering",
            desired_contact_cadence_days=90,
            summary="Stanford acquaintance working in robotics.",
            contact=ContactInfo(emails=["mike@example.com"], phones=["+1-207-555-0100"]),
            facts=[
                Fact(id="fact-1", category="Family", date="2025", text="Mike is married to Jane."),
            ],
            interactions=[
                Interaction(id="int-1", date="2026-08-12", channel="in-person", summary="Had lunch."),
            ],
        )
        assert isinstance(p.birthday, Birthday)
        assert p.birthday.to_string() == "1982-04-17"
        assert p.fact_ids() == ["fact-1"]
        assert p.interaction_ids() == ["int-1"]
        assert p.get_fact("fact-1") is not None
        assert p.get_fact("fact-99") is None
        assert p.last_interaction_date().to_string() == "2026-08-12"

    def test_rejects_bad_person_id(self):
        with pytest.raises(ValidationError):
            self._person(id="Mike_Chen")

    def test_rejects_empty_name(self):
        with pytest.raises(ValidationError):
            self._person(name="  ")

    def test_rejects_non_positive_cadence(self):
        with pytest.raises(ValidationError):
            self._person(desired_contact_cadence_days=0)

    def test_null_cadence_allowed(self):
        p = self._person(desired_contact_cadence_days=None)
        assert p.desired_contact_cadence_days is None

    def test_last_interaction_date_none_when_no_interactions(self):
        p = self._person()
        assert p.last_interaction_date() is None

    def test_last_interaction_date_picks_most_recent_by_latest_anchor(self):
        p = self._person(
            interactions=[
                Interaction(id="int-1", date="2024"),
                Interaction(id="int-2", date="2026-01-05"),
                Interaction(id="int-3", date="2025-06"),
            ]
        )
        assert p.last_interaction_date().to_string() == "2026-01-05"

    def test_extra_fields_rejected(self):
        with pytest.raises(ValidationError):
            self._person(unknown_field="x")
