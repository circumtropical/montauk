"""Repository layer: workspace scoping, same-name people, category
enforcement, archive lifecycle, revisions (spec 32.1, 32.2, 32.5)."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from montauk.db import mapping
from montauk.db.repositories import (
    LocalRecordNotFound,
    PeopleRepository,
    PersonNotFound,
    RelatedPersonInvalid,
    RevisionRepository,
)
from montauk.models import ContactInfo, Fact, Interaction, Person


def _person(pid: str, name: str, **kw) -> Person:
    return Person(id=pid, name=name, **kw)


def _create(repo: PeopleRepository, domain: Person, **kw):
    row = repo.create(domain, **kw)
    repo.session.flush()
    return row


class TestWorkspaceScoping:
    def test_get_does_not_cross_workspaces(self, scope, other_scope):
        a = PeopleRepository(scope)
        b = PeopleRepository(other_scope)
        _create(a, _person("P0001", "Alice in Alpha"))
        _create(b, _person("P0001", "Bob in Beta"))

        assert a.require("P0001").name == "Alice in Alpha"
        assert b.require("P0001").name == "Bob in Beta"

    def test_list_and_count_are_workspace_local(self, scope, other_scope):
        a, b = PeopleRepository(scope), PeopleRepository(other_scope)
        _create(a, _person("P0001", "A1"))
        _create(a, _person("P0002", "A2"))
        _create(b, _person("P0001", "B1"))

        assert a.count() == 2
        assert b.count() == 1
        assert {p.public_id for p in b.list_people()} == {"P0001"}

    def test_missing_id_raises_not_found(self, scope):
        with pytest.raises(PersonNotFound):
            PeopleRepository(scope).require("P9999")

    def test_a_guessed_other_workspace_uuid_resolves_to_none(self, scope, other_scope):
        b = PeopleRepository(other_scope)
        row_b = _create(b, _person("P0001", "B1"))
        assert PeopleRepository(scope).get_by_uuid(row_b.id) is None


class TestSameNamePeople:
    def test_two_people_can_share_a_name(self, scope):
        repo = PeopleRepository(scope)
        _create(repo, _person("P0001", "John Smith"))
        _create(repo, _person("P0002", "John Smith"))
        matches = repo.find_by_name("john  smith")
        assert {p.public_id for p in matches} == {"P0001", "P0002"}

    def test_public_id_is_unique_within_workspace(self, scope):
        repo = PeopleRepository(scope)
        _create(repo, _person("P0001", "First"))
        with pytest.raises(IntegrityError):
            _create(repo, _person("P0001", "Dupe"))
        repo.session.rollback()


class TestCategoryEnforcement:
    def test_unknown_category_is_rejected_by_the_domain_model(self):
        with pytest.raises(Exception):
            Fact(id="fact-1", category="Nonsense", text="x")

    def test_all_six_categories_persist_and_round_trip(self, scope):
        repo = PeopleRepository(scope)
        cats = [
            "Family",
            "Work & Education",
            "Interests",
            "Relationship with User",
            "Life Events",
            "General Notes",
        ]
        facts = [Fact(id=f"fact-{i + 1}", category=c, text=f"about {c}") for i, c in enumerate(cats)]
        _create(repo, _person("P0001", "Cat Person", facts=facts))
        back = mapping.person_to_domain(repo.require("P0001"))
        assert [f.category for f in back.facts] == cats


class TestRoundTrip:
    def test_full_person_round_trips_through_postgres(self, scope):
        repo = PeopleRepository(scope)
        domain = Person(
            id="P0007",
            name="Frank Grimes",
            aliases=["Grimey"],
            birthday="1958-03-02",
            location="Springfield",
            company="Springfield Nuclear Power Plant",
            job_title="Executive Engineer",
            desired_contact_cadence_days=90,
            summary="Former coworker of Homer's.",
            contact=ContactInfo(
                emails=["grimey@snpp.example"],
                phones=["+1-555-0100"],
                address="742 Evergreen Terrace",
                messaging={"signal": "grimey.99"},
            ),
            facts=[
                Fact(
                    id="fact-1",
                    category="Work & Education",
                    date="1997",
                    confidence="high",
                    text="Worked briefly at the plant.",
                    sources=[{"type": "email", "id": "msg-1"}],
                ),
                Fact(id="fact-2", category="Life Events", date="1997-05", text="Passed away."),
            ],
            interactions=[
                Interaction(
                    id="int-1",
                    date="1997-04-15",
                    channel="in-person",
                    connection_level=3,
                    summary="Met at the plant.",
                    sources=[{"type": "note", "id": "n-1"}],
                )
            ],
        )
        _create(repo, domain)
        back = mapping.person_to_domain(repo.require("P0007"))
        assert back.model_dump() == domain.model_dump()

    def test_partial_precision_dates_preserved(self, scope):
        repo = PeopleRepository(scope)
        domain = _person(
            "P0001",
            "Date Person",
            facts=[
                Fact(id="fact-1", category="Life Events", date="2019", text="year only"),
                Fact(id="fact-2", category="Life Events", date="2019-06", text="month"),
                Fact(id="fact-3", category="Life Events", date="2019-06-15", text="day"),
            ],
        )
        _create(repo, domain)
        back = mapping.person_to_domain(repo.require("P0001"))
        assert [f.date.to_string() for f in back.facts] == ["2019", "2019-06", "2019-06-15"]

    def test_birthday_without_year(self, scope):
        repo = PeopleRepository(scope)
        _create(repo, _person("P0001", "No Year", birthday="04-01"))
        back = mapping.person_to_domain(repo.require("P0001"))
        assert back.birthday.to_string() == "04-01"


class TestArchiveLifecycle:
    def test_archive_hides_from_default_list_and_restore_brings_back(self, scope):
        repo = PeopleRepository(scope)
        row = _create(repo, _person("P0001", "Archie"))

        repo.set_archived(row, True, reason="left the company")
        repo.session.flush()
        assert repo.count(archived=False) == 0
        assert repo.count(archived=True) == 1
        assert repo.get("P0001", include_archived=False) is None
        assert repo.get("P0001", include_archived=True) is not None

        repo.set_archived(row, False)
        repo.session.flush()
        assert repo.count(archived=False) == 1

    def test_archive_records_a_revision(self, scope):
        repo = PeopleRepository(scope)
        row = _create(repo, _person("P0001", "Archie"))
        repo.set_archived(row, True, reason="moved away")
        repo.session.flush()

        revs = RevisionRepository(scope).for_person(row.id)
        archived_rev = next(r for r in revs if r.field == "archived_at")
        assert archived_rev.new_value is True
        assert archived_rev.reason == "moved away"
        assert archived_rev.actor_type == "owner"


class TestRevisions:
    def test_replace_logs_field_level_changes(self, scope):
        repo = PeopleRepository(scope)
        row = _create(repo, _person("P0001", "Old Name", company="OldCo"))

        updated = _person("P0001", "New Name", company="NewCo", summary="now has a summary")
        repo.replace(row, updated, reason="corrections")
        repo.session.flush()

        revs = {r.field: r for r in RevisionRepository(scope).for_person(row.id)}
        assert revs["name"].old_value == "Old Name" and revs["name"].new_value == "New Name"
        assert revs["company"].old_value == "OldCo" and revs["company"].new_value == "NewCo"
        assert revs["summary"].new_value == "now has a summary"

    def test_replace_preserves_related_person_links(self, scope):
        repo = PeopleRepository(scope)
        _create(repo, _person("P0001", "Homer"))
        row = _create(
            repo,
            _person(
                "P0002",
                "Bart",
                facts=[
                    Fact(
                        id="fact-1",
                        category="Family",
                        text="Homer's son.",
                        related_person_id="P0001",
                    )
                ],
            ),
        )
        repo.link_related_person_ids([row])
        repo.session.flush()
        assert repo.require("P0002").facts[0].related_person_id is not None

        repo.replace(row, mapping.person_to_domain(row))
        repo.session.flush()
        fact = repo.require("P0002").facts[0]
        assert fact.related_person_public_id == "P0001"
        assert fact.related_person_id is not None


class TestInlineFactEditing:
    def test_add_update_remove_fact_allocates_ids_and_logs_revisions(self, scope):
        repo = PeopleRepository(scope)
        row = _create(repo, _person("P0001", "Homer"))

        fid = repo.add_fact(row, category="Interests", text="Enjoys donuts", confidence="medium")
        repo.session.flush()
        assert fid == "fact-1"
        assert mapping.person_to_domain(repo.require("P0001")).facts[0].text == "Enjoys donuts"

        repo.add_fact(row, category="Work & Education", text="Safety inspector")
        repo.session.flush()
        repo.remove_fact(row, "fact-1", reason="wrong")
        repo.session.flush()
        # next id keeps climbing -- freed ids are not reused
        assert repo.add_fact(row, category="Family", text="Married to Marge") == "fact-3"

        repo.update_fact(row, "fact-2", text="Nuclear safety inspector", confidence="high")
        repo.session.flush()
        back = {f.id: f for f in mapping.person_to_domain(repo.require("P0001")).facts}
        assert back["fact-2"].text == "Nuclear safety inspector"

        revs = {(r.entity_type, r.field) for r in RevisionRepository(scope).for_person(row.id)}
        assert ("fact", "__created__") in revs
        assert ("fact", "__removed__") in revs
        assert ("fact", "text") in revs

    def test_add_fact_partial_date_precision(self, scope):
        repo = PeopleRepository(scope)
        row = _create(repo, _person("P0001", "P"))
        repo.add_fact(row, category="Life Events", text="Started a new job", date="2021")
        repo.add_fact(row, category="Life Events", text="Moved house", date="2021-06")
        repo.session.flush()
        dates = [f.date.to_string() for f in mapping.person_to_domain(repo.require("P0001")).facts]
        assert dates == ["2021", "2021-06"]

    def test_related_person_reference_validated_against_workspace(self, scope, other_scope):
        repo = PeopleRepository(scope)
        other = PeopleRepository(other_scope)
        row = _create(repo, _person("P0001", "Homer"))
        _create(other, _person("P0001", "Someone Else"))  # same public id, other workspace

        with pytest.raises(RelatedPersonInvalid):
            repo.add_fact(row, category="Family", text="x", related_person_id="P0001")  # self
        with pytest.raises(RelatedPersonInvalid):
            repo.add_fact(row, category="Family", text="x", related_person_id="P0404")  # missing
        with pytest.raises(RelatedPersonInvalid):
            repo.add_fact(row, category="Family", text="x", related_person_id="not-an-id")

        target = _create(repo, _person("P0002", "Bart"))
        repo.add_fact(row, category="Family", text="Homer's son", related_person_id="P0002")
        repo.session.flush()
        fact = repo.require("P0001").facts[0]
        assert fact.related_person_id == target.id

    def test_remove_unknown_fact_raises(self, scope):
        repo = PeopleRepository(scope)
        row = _create(repo, _person("P0001", "P"))
        with pytest.raises(LocalRecordNotFound):
            repo.remove_fact(row, "fact-9")


class TestInlineInteractionEditing:
    def test_add_update_remove_interaction(self, scope):
        repo = PeopleRepository(scope)
        row = _create(repo, _person("P0001", "P"))

        iid = repo.add_interaction(row, date="2024-05-01", channel="phone", summary="Caught up")
        repo.session.flush()
        assert iid == "int-1"

        repo.update_interaction(row, "int-1", summary="Long catch-up call", connection_level=4)
        repo.session.flush()
        i = mapping.person_to_domain(repo.require("P0001")).interactions[0]
        assert i.summary == "Long catch-up call" and i.connection_level == 4

        # occurred_on_latest tracks a date edit (drives overdue queries)
        repo.update_interaction(row, "int-1", date="2024-06")
        repo.session.flush()
        orm_i = repo.require("P0001").interactions[0]
        assert orm_i.occurred_on_latest.isoformat() == "2024-06-30"

        repo.remove_interaction(row, "int-1", reason="duplicate")
        repo.session.flush()
        assert mapping.person_to_domain(repo.require("P0001")).interactions == []

    def test_clear_connection_level(self, scope):
        repo = PeopleRepository(scope)
        row = _create(repo, _person("P0001", "P"))
        repo.add_interaction(row, date="2024-01-01", connection_level=3, summary="x")
        repo.session.flush()
        repo.update_interaction(row, "int-1", clear_connection_level=True)
        repo.session.flush()
        assert mapping.person_to_domain(repo.require("P0001")).interactions[0].connection_level is None
