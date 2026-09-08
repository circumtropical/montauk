"""Builders for synthetic Phase 1 deployment directories used by the
migration golden tests (spec 32.6). Fictional data only."""

from __future__ import annotations

import json
from pathlib import Path

GOLDEN_PEOPLE = {
    # Ordinary active person: aliases + every contact field + several
    # categories, confidences, and date precisions + source types.
    "P0001": """\
---
id: P0001
name: Dana Whitfield
aliases:
  - Dee
  - D. Whitfield
birthday: 1985-07-02
location: Portland, OR
company: Nimbus Robotics
job_title: Staff Engineer
desired_contact_cadence_days: 45
summary: College friend; robotics engineer in Portland.
contact:
  emails:
    - dana@example.com
    - dana.whitfield@nimbus.example
  phones:
    - "+1-503-555-0142"
  address: 88 SE Alder St, Portland OR
  messaging:
    signal: dana.99
    matrix: "@dana:example.org"
---

# Dana Whitfield

## Family

- id: fact-1
  confidence: medium
  text: Has a younger sibling who lives in Seattle.

## Work & Education

- id: fact-2
  date: 2007
  confidence: high
  text: Studied mechanical engineering at Oregon State.
  sources:
    - type: conversation
      id: conv-2019-05
- id: fact-3
  date: 2021-03
  confidence: high
  text: Joined Nimbus Robotics as a staff engineer.

## Interests

- id: fact-4
  confidence: high
  text: Trail running and homemade pasta.

## Relationship with User

- id: fact-5
  confidence: high
  text: Met as college roommates in 2005.

## Life Events

- id: fact-6
  date: 2018-09-15
  confidence: high
  text: Got married in Hood River.

## General Notes

## Interactions

### int-1
- date: 2023-11-04
- channel: phone
- connection_level: 4
- summary: Long catch-up call about the new job and a planned trip.
- sources:
    - type: note
      id: note-114

### int-2
- date: 2024-02
- channel: text
- summary: Quick happy-birthday exchange.
""",
    # Second active person with the SAME display name (duplicate names allowed).
    "P0002": """\
---
id: P0002
name: Dana Whitfield
summary: Neighbour two doors down; unrelated to the other Dana.
---

# Dana Whitfield

## Family

## Work & Education

## Interests

## Relationship with User

- id: fact-1
  confidence: high
  text: Neighbour since 2022; waters the plants during trips.

## Life Events

## General Notes

## Interactions

### int-1
- date: 2024-05-20
- summary: Borrowed a ladder.
""",
    # Person with a relationship to an ACTIVE person and to an ARCHIVED person,
    # birthday with no year, low-confidence fact.
    "P0003": """\
---
id: P0003
name: Marco Reyes
birthday: 03-14
summary: Former teammate; now freelancing.
---

# Marco Reyes

## Family

## Work & Education

- id: fact-1
  date: 2019
  confidence: high
  text: Worked with Dana at Nimbus Robotics.
  related_person_id: P0001

## Interests

## Relationship with User

- id: fact-2
  confidence: low
  text: Possibly moving back to the area next year.

## Life Events

## General Notes

- id: fact-3
  confidence: high
  text: Introduced by a mutual friend who has since moved away.
  related_person_id: P0009

## Interactions

### int-1
- date: 2024-01-08
- channel: email
- connection_level: 2
- summary: Asked for a contractor recommendation.
""",
}

GOLDEN_ARCHIVED = {
    # Archived person, referenced by P0003.
    "P0009": """\
---
id: P0009
name: Priya Anand
aliases:
  - Pri
company: Old Town Studio
job_title: Designer
summary: Mutual friend who moved abroad; record archived.
---

# Priya Anand

## Family

## Work & Education

- id: fact-1
  date: 2016-06
  confidence: high
  text: Ran a small design studio downtown.

## Interests

## Relationship with User

## Life Events

- id: fact-2
  date: 2022
  confidence: medium
  text: Moved to Lisbon. Lost regular contact.

## General Notes

## Interactions

### int-1
- date: 2021-12-31
- summary: Final in-person goodbye before the move.
""",
}


def write_phase1(root: Path, *, people: dict[str, str], archived: dict[str, str], high_water: int) -> Path:
    root = Path(root)
    (root / "people").mkdir(parents=True, exist_ok=True)
    (root / "archive").mkdir(parents=True, exist_ok=True)
    for pid, body in people.items():
        (root / "people" / f"{pid}.md").write_text(body, encoding="utf-8")
    for pid, body in archived.items():
        (root / "archive" / f"{pid}.md").write_text(body, encoding="utf-8")
    (root / "person-id-sequence.json").write_text(
        json.dumps({"last_allocated": high_water}, indent=2) + "\n", encoding="utf-8"
    )
    return root


def write_golden(root: Path, *, high_water: int = 40) -> Path:
    """The clean reference deployment: 3 active + 1 archived, a high ID
    allocator value with gaps (P0004..P0008 never existed)."""
    return write_phase1(root, people=GOLDEN_PEOPLE, archived=GOLDEN_ARCHIVED, high_water=high_water)
