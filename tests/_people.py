"""Rich fictional Person fixtures for the retrieval-engine tests.

These used to be parsed from Markdown files under examples/; the Markdown
storage format is gone, so they are plain domain objects now.
"""

from __future__ import annotations

from montauk.models import ContactInfo, Fact, Interaction, Person, Source


def priya() -> Person:
    """A dating-app connection with ~15 facts across every category and
    three interactions -- exercises category order, date precision,
    confidence, a structured relationship reference, and long summaries."""
    return Person(
        id="P0001",
        name="Priya Raman",
        aliases=["Priya"],
        location="Portland, Maine",
        job_title="Pediatric nurse",
        company="Maine Medical Center",
        desired_contact_cadence_days=7,
        summary=(
            "Dating-app connection Alex has been getting to know. After an easy, low-pressure "
            "first date at Fort Williams -- a cliff walk and climbing the old stone tower -- "
            "Priya sent a flurry of enthusiastic follow-up texts. Alex finds her genuinely "
            "likeable but is still unsure about romantic spark. A second date is loosely planned."
        ),
        contact=ContactInfo(messaging={"hinge": "priya.r"}),
        facts=[
            Fact(
                id="fact-1",
                category="Relationship with User",
                date="2026-07",
                text="Alex met Priya on the dating app Hinge in July 2026; the first exchange of "
                "messages was about rock climbing.",
            ),
            Fact(
                id="fact-2",
                category="Relationship with User",
                date="2026-07",
                related_person_id="P0002",
                text="Marco Feldman, a mutual friend, vouched for Priya to Alex before the first date.",
            ),
            Fact(
                id="fact-3",
                category="Interests",
                text="Priya boulders at a climbing gym two or three times a week and talked about a "
                "past trip to the Red River Gorge she wants to repeat.",
            ),
            Fact(
                id="fact-4",
                category="Interests",
                text="Priya said she likes sour beers and cannot stand IPAs.",
            ),
            Fact(
                id="fact-5",
                category="General Notes",
                text="Priya has two rescue dogs and had to leave the first date by about 6pm to get "
                "home and let them out.",
            ),
            Fact(
                id="fact-6",
                category="Work & Education",
                text="Priya is a pediatric nurse at Maine Medical Center and works three twelve-hour "
                "shifts most weeks, so her days off move around.",
            ),
            Fact(
                id="fact-7",
                category="Life Events",
                date="2025",
                confidence="medium",
                text="Priya moved to Portland from Denver about a year ago, after a breakup.",
            ),
            Fact(
                id="fact-8",
                category="Relationship with User",
                date="2026-08-20",
                text="On the first date they climbed the stone tower at Fort Williams; at the top "
                'Priya said, "I could stay up here all afternoon."',
            ),
            Fact(
                id="fact-9",
                category="Relationship with User",
                date="2026-08-20",
                confidence="medium",
                text="Alex found the first date easy and low-pressure but was not sure yet whether "
                "there was real romantic spark.",
            ),
            Fact(
                id="fact-10",
                category="Relationship with User",
                date="2026-08-20",
                text="An awkward moment on the first date: Priya asked whether Alex was fully over "
                "his last relationship.",
            ),
            Fact(
                id="fact-11",
                category="Relationship with User",
                date="2026-08-20",
                text="After the first date Priya waited about two hours and then sent four texts in "
                "a row about wanting to do it again soon.",
            ),
            Fact(
                id="fact-12",
                category="General Notes",
                confidence="medium",
                text="Priya is vegetarian and named a favorite Thai place near Munjoy Hill.",
            ),
            Fact(
                id="fact-13",
                category="Family",
                text="Priya is close with her younger brother, who still lives in Denver.",
            ),
            Fact(
                id="fact-14",
                category="Interests",
                confidence="low",
                text="Priya mentioned she is slowly learning the banjo; she picked it up during the "
                "pandemic.",
            ),
            Fact(
                id="fact-15",
                category="Life Events",
                text="Priya's birthday is in March; she has not said the exact day or the year.",
            ),
        ],
        interactions=[
            Interaction(
                id="int-1",
                date="2026-08-20",
                channel="in-person",
                connection_level=3,
                summary="First date at Fort Williams Park in Cape Elizabeth. They walked the "
                "cliff path, climbed the old stone tower, and got coffee afterward. "
                "Priya left around 6pm to let her dogs out.",
                sources=[Source(type="conversation", id="conv-2026-08-20")],
            ),
            Interaction(
                id="int-2",
                date="2026-08-20",
                channel="text",
                summary="Later the same night Priya sent a burst of texts saying she had had "
                "a great time and wanted to plan something for the next week.",
            ),
            Interaction(
                id="int-3",
                date="2026-08-27",
                channel="phone",
                connection_level=3,
                summary="Short call to set up a second date; they landed on a climbing-gym "
                "day pass followed by dinner.",
            ),
        ],
    )
