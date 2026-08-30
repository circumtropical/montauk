---
id: homer-simpson
name: Homer Simpson
aliases:
  - Homer J. Simpson
  - Homie
birthday: 1956-05-12
location: "742 Evergreen Terrace, Springfield"
company: Springfield Nuclear Power Plant
job_title: Safety Inspector, Sector 7G
desired_contact_cadence_days: 7
summary: Old friend from the power plant and the neighborhood.
contact:
  emails:
    - chunkylover53@aol.com
  phones:
    - "+1-939-555-0113"
  messaging:
    telegram: "@chunkylover53"
---

# Homer Simpson

## Family

- id: fact-1
  date: 1980
  confidence: high
  text: Married to Marge Simpson.
  related_person_id: marge-simpson
  sources:
    - type: manual
      id: onboarding

- id: fact-2
  date: 1990
  confidence: high
  text: Father of three -- Bart, Lisa, and Maggie.
  sources:
    - type: manual
      id: onboarding

## Work & Education

- id: fact-3
  date: 2026-01
  confidence: medium
  text: Has been passed over for a promotion at the plant again; seems annoyed about it.
  sources:
    - type: agent
      id: personal-assistant

## Interests

- id: fact-4
  confidence: high
  text: Enjoys bowling with his league team, the Pin Pals.

## Relationship with User

- id: fact-5
  date: 1985
  confidence: high
  text: Next-door neighbor and long-time drinking buddy from Moe's.

## Life Events

- id: fact-6
  date: 1999-01
  confidence: medium
  text: Briefly became an astronaut candidate; did not end up going to space.

## General Notes

- id: fact-7
  confidence: low
  text: May or may not know how to use email correctly.

## Interactions

### int-1
- date: 2026-08-20
- channel: in-person
- connection_level: 5
- summary: Ran into him at Moe's, talked about the game.

### int-2
- date: 2026-08-25
- channel: phone
- connection_level: 3
- summary: Quick call about the neighborhood block party.
- sources:
  - type: agent
    id: personal-assistant
