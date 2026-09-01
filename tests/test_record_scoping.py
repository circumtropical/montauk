"""Record-scoped relevance (spec section 15.1): a source may be about
several people, but a Montauk record is about exactly one. These tests
inspect the actually-published MCP instructions and registered tool
descriptions, and exercise the structural safeguards that back the
natural-language rule.
"""

import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport

from _helpers import call, call_expecting_error, running_session
from montauk.server import SERVER_INSTRUCTIONS, create_server

NARRATIVE_MUTATION_TOOLS = ("add_fact", "update_fact", "update_summary", "update_person_batch")


class TestServerInstructions:
    @pytest.mark.asyncio
    async def test_record_scoping_section_is_discoverable_over_the_wire(self):
        server = create_server()
        async with InMemoryTransport(server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                result = await session.initialize()
        assert result.instructions == SERVER_INSTRUCTIONS
        assert "RECORD SCOPING" in result.instructions

    def test_instructions_state_records_are_scoped_to_one_person(self):
        assert "scoped to the person of record" in SERVER_INSTRUCTIONS

    def test_instructions_reject_relationship_inference_from_co_occurrence(self):
        text = SERVER_INSTRUCTIONS.lower()
        assert "co-occurrence" in text
        assert "never infer a relationship from co-occurrence" in text
        assert "not necessarily related" in text

    def test_instructions_direct_agents_to_separate_unrelated_subjects(self):
        assert (
            "separate the information by subject and update each person independently"
            in SERVER_INSTRUCTIONS
        )

    def test_instructions_tell_agents_to_omit_or_clarify_uncertain_relevance(self):
        assert "If relevance is uncertain, omit the reference or ask the user to clarify" in (
            SERVER_INSTRUCTIONS
        )


class TestToolDescriptions:
    @pytest.mark.asyncio
    async def test_narrative_mutation_tools_carry_the_record_scoping_rule(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            tools = {t.name: t for t in (await session.list_tools()).tools}
            for name in NARRATIVE_MUTATION_TOOLS:
                description = tools[name].description
                assert "directly relevant to the specified person_id" in description, name
                assert (
                    "merely because they appeared in the same conversation or source event"
                    in description
                ), name

    @pytest.mark.asyncio
    async def test_create_person_summary_is_scoped_to_the_new_person(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            tools = {t.name: t for t in (await session.list_tools()).tools}
            description = tools["create_person"].description.lower()
            assert "initial summary must describe only this person" in description

    @pytest.mark.asyncio
    async def test_batch_description_includes_the_separate_batch_rule(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            tools = {t.name: t for t in (await session.list_tools()).tools}
            description = tools["update_person_batch"].description
            assert "create a separate batch for each person" in description

    @pytest.mark.asyncio
    async def test_record_interaction_description_excludes_unrelated_people(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            tools = {t.name: t for t in (await session.list_tools()).tools}
            description = tools["record_interaction"].description
            assert "Unrelated people discussed in the source material must not be included" in (
                description
            )
            assert "person of record actually interacted with them" in description


class TestScenarioA_UnrelatedPeopleInOneSource:
    """Source discusses Alice, Bob, Carol -- none know one another; target is Alice.
    A correctly-scoped update to Alice must be possible and must leave the other
    records completely untouched."""

    @pytest.mark.asyncio
    async def test_scoped_update_to_alice_does_not_touch_bob_or_carol(self, tmp_path):
        async with running_session(tmp_path) as (session, ctx):
            await call(session, "create_person", name="Alice Nguyen")
            await call(session, "create_person", name="Bob Jones")
            await call(session, "create_person", name="Carol Smith")
            bob_before = ctx.store.person_path("bob-jones").read_text()
            carol_before = ctx.store.person_path("carol-smith").read_text()

            await call(
                session,
                "update_person_batch",
                person_id="alice-nguyen",
                operations=[
                    {"op": "update_summary", "summary": "Started a new job at a robotics startup."},
                    {"op": "add_fact", "category": "Work & Education", "text": "Joined a robotics startup."},
                ],
            )

            alice_record = await call(session, "get_full_record", person_id="alice-nguyen")
            assert "Bob" not in alice_record and "Carol" not in alice_record
            alice_facts = await call(session, "get_facts", person_id="alice-nguyen")
            assert all(f["related_person_id"] is None for f in alice_facts)

            assert ctx.store.person_path("bob-jones").read_text() == bob_before
            assert ctx.store.person_path("carol-smith").read_text() == carol_before


class TestScenarioB_LegitimateDirectInteraction:
    """Alice met Bob at a conference and Bob introduced her to a prospective
    customer. Alice's record may reference Bob because the interaction is
    established; a structured reference still gets normal person-ID validation."""

    @pytest.mark.asyncio
    async def test_interaction_and_related_fact_referencing_bob_are_allowed(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Alice Nguyen")
            await call(session, "create_person", name="Bob Jones")

            await call(
                session,
                "record_interaction",
                person_id="alice-nguyen",
                date="2026-05-10",
                summary="Met Bob Jones at the RoboConf conference; Bob introduced her to a prospective customer.",
            )
            result = await call(
                session,
                "add_fact",
                person_id="alice-nguyen",
                category="Work & Education",
                text="Introduced to a prospective customer by Bob Jones at RoboConf.",
                related_person_id="bob-jones",
            )
            assert result["index_update_status"] == "ok"

            facts = await call(session, "get_facts", person_id="alice-nguyen")
            assert facts[0]["related_person_id"] == "bob-jones"

    @pytest.mark.asyncio
    async def test_structured_reference_to_nonexistent_person_is_rejected(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Alice Nguyen")
            text = await call_expecting_error(
                session,
                "add_fact",
                person_id="alice-nguyen",
                category="Work & Education",
                text="Something about someone with no record.",
                related_person_id="carol-smith",
            )
            assert "VALIDATION_ERROR" in text

    @pytest.mark.asyncio
    async def test_structured_reference_must_be_a_person_id_not_a_name(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Alice Nguyen")
            text = await call_expecting_error(
                session,
                "add_fact",
                person_id="alice-nguyen",
                category="Family",
                text="Married to Bob.",
                related_person_id="Bob Jones",
            )
            assert "VALIDATION_ERROR" in text

    @pytest.mark.asyncio
    async def test_structured_reference_cannot_point_at_the_record_itself(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Alice Nguyen")
            text = await call_expecting_error(
                session,
                "add_fact",
                person_id="alice-nguyen",
                category="General Notes",
                text="Self reference.",
                related_person_id="alice-nguyen",
            )
            assert "VALIDATION_ERROR" in text


class TestScenarioC_SharedDiscussionWithoutSharedRelationship:
    """User mentions Alice's promotion and, in the same conversation, Bob's
    medical appointment, then asks to update Alice. Alice's record gets the
    promotion and nothing about Bob or the medical detail."""

    @pytest.mark.asyncio
    async def test_only_the_subject_relevant_fact_lands_on_alice(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Alice Nguyen")

            await call(session, "update_summary", person_id="alice-nguyen", summary="Promoted to engineering manager.")

            person = await call(session, "get_person", person_id="alice-nguyen")
            assert person["summary"] == "Promoted to engineering manager."
            record = await call(session, "get_full_record", person_id="alice-nguyen")
            assert "Bob" not in record and "medical" not in record.lower()


class TestScenarioD_SeparateUpdatesFromOneSource:
    """One source has independently record-worthy info about unrelated Alice and
    Bob. The batch API structurally cannot span both, and the guidance says to
    make a separate batch per person."""

    @pytest.mark.asyncio
    async def test_batch_targets_exactly_one_person_and_guidance_says_to_split(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            tools = {t.name: t for t in (await session.list_tools()).tools}
            schema = tools["update_person_batch"].input_schema
            assert schema["required"] == ["person_id", "operations"]
            # No second-person parameter exists; a batch cannot name two subjects.
            assert set(schema["properties"]) <= {"person_id", "operations"}
            assert "create a separate batch for each person" in tools["update_person_batch"].description

    @pytest.mark.asyncio
    async def test_two_separate_batches_keep_each_record_clean(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Alice Nguyen")
            await call(session, "create_person", name="Bob Jones")

            await call(
                session,
                "update_person_batch",
                person_id="alice-nguyen",
                operations=[{"op": "add_fact", "category": "Life Events", "text": "Bought a house."}],
            )
            await call(
                session,
                "update_person_batch",
                person_id="bob-jones",
                operations=[{"op": "add_fact", "category": "Life Events", "text": "Adopted a dog."}],
            )

            alice_record = await call(session, "get_full_record", person_id="alice-nguyen")
            bob_record = await call(session, "get_full_record", person_id="bob-jones")
            assert "dog" not in alice_record and "Bob" not in alice_record
            assert "house" not in bob_record and "Alice" not in bob_record


class TestScenarioE_AmbiguousRelevance:
    """Source mentions Alice and Bob together but does not establish whether they
    know one another. The published guidance points the agent at omit-or-clarify,
    not an inferred relationship."""

    def test_guidance_points_at_omit_or_clarify_not_inference(self):
        assert "If relevance is uncertain, omit the reference or ask the user to clarify" in (
            SERVER_INSTRUCTIONS
        )
        assert "Never infer a relationship from co-occurrence" in SERVER_INSTRUCTIONS


class TestRegression:
    @pytest.mark.asyncio
    async def test_valid_spouse_relationship_fact_still_works(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            await call(session, "create_person", name="Marge Simpson")
            result = await call(
                session,
                "add_fact",
                person_id="homer-simpson",
                category="Family",
                text="Married to Marge Simpson.",
                related_person_id="marge-simpson",
            )
            assert result["changed_ids"] == ["fact-1"]

    @pytest.mark.asyncio
    async def test_valid_relationship_fact_via_batch_still_works(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            await call(session, "create_person", name="Ned Flanders")
            await call(
                session,
                "update_person_batch",
                person_id="homer-simpson",
                operations=[
                    {
                        "op": "add_fact",
                        "category": "General Notes",
                        "text": "Next-door neighbour.",
                        "related_person_id": "ned-flanders",
                    }
                ],
            )
            facts = await call(session, "get_facts", person_id="homer-simpson")
            assert facts[0]["related_person_id"] == "ned-flanders"

    @pytest.mark.asyncio
    async def test_reference_to_archived_person_is_still_permitted(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            await call(session, "create_person", name="Frank Grimes")
            await call(session, "archive_person", person_id="frank-grimes")
            result = await call(
                session,
                "add_fact",
                person_id="homer-simpson",
                category="Work & Education",
                text="Worked with Frank Grimes at the plant.",
                related_person_id="frank-grimes",
            )
            assert result["changed_ids"] == ["fact-1"]

    @pytest.mark.asyncio
    async def test_interaction_with_no_new_facts_still_records(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            result = await call(session, "record_interaction", person_id="homer-simpson", date="2026-08-20")
            assert result["changed_ids"] == ["int-1"]
