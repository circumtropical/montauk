"""The reference dataset the dashboard end-to-end tests run against.

Three active people (two sharing a display name) plus one archived
person, with aliases, every contact field, all six fact categories,
partial-precision dates, low/medium confidence, structured relationship
references (to an active and an archived person), and sourced records.
Fictional data only.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from montauk.db import ids as id_alloc
from montauk.db.repositories import Actor, PeopleRepository, WorkspaceScope
from montauk.models import ContactInfo, Fact, Interaction, Person, Source
from montauk.services.workspace import get_or_create_workspace

_DANA = Person(
    id="P0001",
    name="Dana Whitfield",
    aliases=["Dee", "D. Whitfield"],
    birthday="1985-07-02",
    location="Portland, OR",
    company="Nimbus Robotics",
    job_title="Staff Engineer",
    desired_contact_cadence_days=45,
    summary="College friend; robotics engineer in Portland.",
    contact=ContactInfo(
        emails=["dana@example.com", "dana.whitfield@nimbus.example"],
        phones=["+1-503-555-0142"],
        address="88 SE Alder St, Portland OR",
        messaging={"signal": "dana.99", "matrix": "@dana:example.org"},
    ),
    facts=[
        Fact(
            id="fact-1",
            category="Family",
            confidence="medium",
            text="Has a younger sibling who lives in Seattle.",
        ),
        Fact(
            id="fact-2",
            category="Work & Education",
            date="2007",
            text="Studied mechanical engineering at Oregon State.",
            sources=[Source(type="conversation", id="conv-2019-05")],
        ),
        Fact(
            id="fact-3",
            category="Work & Education",
            date="2021-03",
            text="Joined Nimbus Robotics as a staff engineer.",
        ),
        Fact(id="fact-4", category="Interests", text="Trail running and homemade pasta."),
        Fact(id="fact-5", category="Relationship with User", text="Met as college roommates in 2005."),
        Fact(id="fact-6", category="Life Events", date="2018-09-15", text="Got married in Hood River."),
    ],
    interactions=[
        Interaction(
            id="int-1",
            date="2023-11-04",
            channel="phone",
            connection_level=4,
            summary="Long catch-up call about the new job and a planned trip.",
            sources=[Source(type="note", id="note-114")],
        ),
        Interaction(id="int-2", date="2024-02", channel="text", summary="Quick happy-birthday exchange."),
    ],
)

_DANA_2 = Person(
    id="P0002",
    name="Dana Whitfield",
    summary="Neighbour two doors down; unrelated to the other Dana.",
    facts=[
        Fact(
            id="fact-1",
            category="Relationship with User",
            text="Neighbour since 2022; waters the plants during trips.",
        ),
    ],
    interactions=[Interaction(id="int-1", date="2024-05-20", summary="Borrowed a ladder.")],
)

_MARCO = Person(
    id="P0003",
    name="Marco Reyes",
    birthday="03-14",
    summary="Former teammate; now freelancing.",
    facts=[
        Fact(
            id="fact-1",
            category="Work & Education",
            date="2019",
            related_person_id="P0001",
            text="Worked with Dana at Nimbus Robotics.",
        ),
        Fact(
            id="fact-2",
            category="Relationship with User",
            confidence="low",
            text="Possibly moving back to the area next year.",
        ),
        Fact(
            id="fact-3",
            category="General Notes",
            related_person_id="P0009",
            text="Introduced by a mutual friend who has since moved away.",
        ),
    ],
    interactions=[
        Interaction(
            id="int-1",
            date="2024-01-08",
            channel="email",
            connection_level=2,
            summary="Asked for a contractor recommendation.",
        ),
    ],
)

_PRIYA = Person(
    id="P0009",
    name="Priya Anand",
    aliases=["Pri"],
    company="Old Town Studio",
    job_title="Designer",
    summary="Mutual friend who moved abroad; record archived.",
    facts=[
        Fact(
            id="fact-1",
            category="Work & Education",
            date="2016-06",
            text="Ran a small design studio downtown.",
        ),
        Fact(
            id="fact-2",
            category="Life Events",
            date="2022",
            confidence="medium",
            text="Moved to Lisbon. Lost regular contact.",
        ),
    ],
    interactions=[
        Interaction(id="int-1", date="2021-12-31", summary="Final in-person goodbye before the move."),
    ],
)


def seed_golden(session_maker: sessionmaker[Session], *, workspace_name: str = "Personal") -> None:
    with session_maker() as session:
        ws = get_or_create_workspace(session, workspace_name)
        session.flush()
        repo = PeopleRepository(WorkspaceScope(session, ws.id, Actor("owner", "owner@example.com")))
        rows = [repo.create(p) for p in (_DANA, _DANA_2, _MARCO, _PRIYA)]
        repo.set_archived(rows[-1], True, reason="moved abroad")
        # Second pass: resolve the structured relationship references now
        # that every referenced person exists.
        repo.link_related_person_ids(rows)
        # The allocator is at 0 (ids were assigned explicitly); advance it
        # past P0009 with the same P0004..P0008 gap the reference set has,
        # so the next dashboard-created person gets P0010+.
        id_alloc.ensure_high_water_at_least(session, ws.id, 40)
        session.commit()
