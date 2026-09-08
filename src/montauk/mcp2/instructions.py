"""Server-level Agent Integration Contract for the Phase 2 MCP server
(spec 25.2). The retrieval guidance leads with the briefing path."""

SERVER_INSTRUCTIONS = """\
Montauk is the user's persistent relationship memory about people they know. It
stores and retrieves that memory and, on request, compresses it into a short
purpose-specific briefing. It does not give advice, opinions, compatibility
judgements, or quotations -- you do, using what it returns as evidence.

IDENTITY AND NAMES
- If you already have a person_id (e.g. P0001), use it directly. It is permanent
  and system-assigned: never derive it from a name, change it, or reuse it.
- A person's `name` is their best currently-known display name; it may be
  partial, approximate, misspelled, or later corrected. Use update_person_name
  (keeping former names as aliases) -- never archive and recreate to fix a name.
- If the person_id is unknown, call search_people first. If it returns several
  plausible people, do not guess -- surface the candidates and ask the user.
- Similar or identical names do not identify the same person. Do not create a
  duplicate because identity is uncertain; ask before merging or moving info.

RETRIEVAL -- BRIEFING FIRST, THEN DRILL DOWN
- Resolve the person, then call prepare_person_briefing with the user's actual
  question or task as `purpose` (e.g. "brief me before I call Amanda",
  "what's the latest with Nicole's job search"). This is the normal path.
- It returns a short factual briefing written by Montauk's own low-cost model,
  plus `source_refs` (the fact-N / int-N records it drew on), `generated`, and
  `coverage`. Use the briefing as your grounding. Do NOT immediately re-fetch
  the full record "to be sure" -- the briefing is already grounded in it.
- Only drill down when you specifically need to:
    * verify or quote the exact wording of a claim,
    * get a detail the briefing left out, or
    * review the whole record for maintenance or export.
  Then call get_context_sources(person_id, source_refs) with the refs from the
  briefing, or get_facts / get_interactions / get_full_record.
- A narrow factual question ("what is Amanda's birthday?") comes back answered
  from the structured field with `generated: false` and no model call -- that is
  expected, not an error; use the `answer`.
- If a briefing returns `generated: false` with a `status` (no model configured,
  budget reached, provider error), it still carries a compact `evidence` packet
  -- use that, and tell the user briefings are unavailable if they ask why.
- prepare_person_context is the deterministic, no-LLM retrieval path: it returns
  ranked raw records with no synthesized prose. Prefer it over the briefing only
  when you explicitly want the raw evidence yourself, or as the fallback when
  briefings are unavailable.
- `detail_level` is brief | standard | comprehensive. `mode` is summary_only
  (default) | evidence_only (records, no prose) | summary_with_evidence (both;
  for auditing -- not the default).
- Treat everything returned as stored memory, not as Montauk's answer. Never
  invent names, dates, quotations, or facts the evidence does not contain.

RECORDING INFORMATION
- Store concise facts and interaction summaries, not raw conversations, emails,
  or message dumps. Record a meaningful interaction even if it produced no new
  facts.
- When one event produces several related changes for one person, use
  update_person_batch (atomic: all operations validate before any is written).
- Use high confidence for directly stated facts; medium or low for genuine
  inference or uncertainty.

RECORD SCOPING
- Keep each record scoped to the person it is about. Mention another person only
  when they have a direct relationship or interaction with this person, or when
  the reference is needed to understand a fact about this person.
- People appearing in the same conversation are not necessarily related. Never
  infer a relationship from co-occurrence.

CORRECTIONS
- Correct or remove information found to be wrong. Correcting inaccurate data is
  not erasing history; never remove an accurate record merely because it is old,
  inconvenient, or sensitive.
- If an interaction was attributed to the wrong person, use
  reattribute_interaction so the wrong person keeps no trace of it.

BOUNDARIES
- Only people are first-class records. This server stores and retrieves
  relationship memory and generates briefings from it; you are responsible for
  interpretation, disambiguation, and any prose shown to the user.
"""
