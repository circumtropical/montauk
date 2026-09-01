"""Purpose-specific person-context retrieval (spec amendment: purpose-specific
person context retrieval).

Pure, deterministic, and independently testable: given one canonical
``Person`` (the source of truth), a stated purpose, and an optional
``SemanticIndex``, select the materially relevant summary / facts /
relationships / interactions, rank them with hybrid lexical + semantic
signals, deduplicate and diversify, and trim to a token budget --
returning selected canonical text substantially verbatim.

This module never calls an LLM, never rewrites canonical wording, and
never crosses person boundaries.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from dataclasses import dataclass, field
from functools import lru_cache

from .models import Person
from .schema import CATEGORIES
from .semantic_index import SemanticIndex
from .tokens import estimate_tokens

DETAIL_LEVELS = ("brief", "standard", "comprehensive")

SUMMARY_RECORD_ID = "person-summary"

_PRESENT_STATE_SECTIONS = {"Interests", "Work & Education", "Relationship with User"}

# --- purpose analysis -------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]+|\d{4}(?:-\d{2}){0,2}")
_QUOTED_RE = re.compile(r"[\"“‘']([^\"”’']{2,})[\"”’']")
_STOPWORDS = frozenset(
    ["a", "an", "the", "and", "or", "but", "if", "is", "are", "was", "were", "be", "been", "being", "to", "of", "in", "on", "at", "for", "with", "about", "what", "who", "whom", "whose", "when", "where", "why", "how", "do", "does", "did", "i", "you", "he", "she", "they", "we", "me", "my", "your", "his", "her", "their", "our", "this", "that", "these", "those", "good", "give", "tell", "remind", "know", "think", "would", "could", "should", "have", "has", "had", "get", "got", "make", "made", "take", "see", "say", "said", "really", "very", "just", "from", "as", "it", "its"]
)

_TEMPORAL_HINTS = re.compile(
    r"\b(when|how long|last (?:time|contact|talk|spoke|reach)|since|ago|recent|overdue|"
    r"waited|been a while|catch up|reconnect|reach out|touch base|days?|weeks?|months?)\b",
    re.IGNORECASE,
)
_EVENT_HINTS = re.compile(
    r"\b(said|say|talk(?:ed)?|discuss(?:ed)?|happen(?:ed)?|did|went|meet(?:ing)?|met|"
    r"conversation|told|mentioned|on top of|during|last time)\b",
    re.IGNORECASE,
)
_PRESENT_STATE_HINTS = re.compile(
    r"\b(who is|remind me who|how (?:do|did) i know|like|likes|enjoy|enjoys|prefer|"
    r"favou?rite|gift|present|interested in|into|works? (?:at|as)|job|hobb|match|compatible|"
    r"good match|date idea|second date|first date)\b",
    re.IGNORECASE,
)
_PREFERENCE_HINTS = re.compile(
    r"\b(gift|present|surprise|like|likes|love|loves|enjoy|enjoys|hobb|interest|into|"
    r"favou?rite|prefer|fun|activity|do together|second date|date idea)\b",
    re.IGNORECASE,
)
_ADVISORY_HINTS = re.compile(
    r"\b(match|compatible|compatibility|good (?:match|fit|idea)|should i|do you think|"
    r"gift|worth it|red flag|chemistry|spark)\b",
    re.IGNORECASE,
)
_BRIEFING_HINTS = re.compile(
    r"\b(brief(?:ing)?|everything|full (?:picture|rundown|profile)|catch me up|overview|"
    r"summar(?:y|ise|ize)|remind me who|who \S+ is and how|complete (?:briefing|picture|rundown))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PurposeAnalysis:
    raw: str
    normalized: str
    terms: tuple[str, ...]
    quoted_phrases: tuple[str, ...]
    proper_nouns: tuple[str, ...]
    is_temporal: bool
    wants_events: bool
    wants_present_state: bool
    wants_preferences: bool
    is_advisory: bool
    is_briefing: bool

    @property
    def has_lexical_query(self) -> bool:
        return bool(self.terms or self.quoted_phrases)


def _normalize(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'")
    text = re.sub(r"(\w)'s\b", r"\1", text)  # possessive -> stem ("Nicole's" -> "Nicole")
    text = re.sub(r"\bn't\b", " not", text)
    return " ".join(text.split())


def analyze_purpose(purpose: str, *, subject_name: str | None = None) -> PurposeAnalysis:
    raw = purpose.strip()
    normalized = _normalize(raw)
    quoted = tuple(m.group(1).strip() for m in _QUOTED_RE.finditer(raw))
    # The person of record's own name is not a discriminating query term --
    # it appears in most of their records. Drop it (and its parts).
    subject_tokens = {t.lower() for t in _WORD_RE.findall(subject_name or "")}
    tokens = _WORD_RE.findall(normalized)
    terms = tuple(
        t for t in tokens if t.lower() not in _STOPWORDS and t.lower() not in subject_tokens and len(t) > 1
    )
    proper = tuple(
        t
        for t in _WORD_RE.findall(raw)
        if t[:1].isupper() and t.lower() not in _STOPWORDS and t.lower() not in subject_tokens
    )
    is_briefing = bool(_BRIEFING_HINTS.search(raw))
    return PurposeAnalysis(
        raw=raw,
        normalized=normalized,
        terms=terms,
        quoted_phrases=quoted,
        proper_nouns=proper,
        is_temporal=bool(_TEMPORAL_HINTS.search(raw)),
        wants_events=bool(_EVENT_HINTS.search(raw)),
        wants_present_state=bool(_PRESENT_STATE_HINTS.search(raw)) or is_briefing,
        wants_preferences=bool(_PREFERENCE_HINTS.search(raw)),
        is_advisory=bool(_ADVISORY_HINTS.search(raw)),
        is_briefing=is_briefing,
    )


# --- context units --------------------------------------------------------


@dataclass
class ContextUnit:
    kind: str  # "summary" | "fact" | "relationship" | "interaction"
    record_id: str
    text: str
    section: str | None = None
    date: str | None = None
    confidence: str | None = None
    related_person_id: str | None = None
    channel: str | None = None
    _sort_key: int = 0
    lexical: float = 0.0
    semantic: float = 0.0
    score: float = 0.0

    def approx_tokens(self) -> int:
        # text plus a small allowance for the structured wrapper keys
        return estimate_tokens(self.text) + 6


def _local_id_num(local_id: str) -> int:
    m = re.search(r"-(\d+)$", local_id or "")
    return int(m.group(1)) if m else 0


def build_units(person: Person) -> list[ContextUnit]:
    units: list[ContextUnit] = []
    if person.summary:
        units.append(
            ContextUnit(kind="summary", record_id=SUMMARY_RECORD_ID, text=" ".join(person.summary.split()))
        )
    for fact in person.facts:
        units.append(
            ContextUnit(
                # A fact with a structured person reference is a relationship
                # record; other facts (incl. plain Family facts) stay facts.
                kind="relationship" if fact.related_person_id else "fact",
                record_id=fact.id,
                text=" ".join(fact.text.split()),
                section=fact.category,
                date=fact.date.to_string() if fact.date else None,
                confidence=fact.confidence.value,
                related_person_id=fact.related_person_id,
                _sort_key=_local_id_num(fact.id),
            )
        )
    for interaction in person.interactions:
        if not interaction.summary:
            continue
        units.append(
            ContextUnit(
                kind="interaction",
                record_id=interaction.id,
                text=" ".join(interaction.summary.split()),
                date=interaction.date.to_string(),
                channel=interaction.channel,
                _sort_key=_local_id_num(interaction.id),
            )
        )
    return units


# --- lexical retrieval (ephemeral in-memory FTS5) -------------------------


@lru_cache(maxsize=1)
def _fts5_available() -> bool:
    try:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE VIRTUAL TABLE _t USING fts5(x)")
        con.close()
        return True
    except sqlite3.OperationalError:
        return False


def _fts_match_query(analysis: PurposeAnalysis) -> str:
    parts: list[str] = []
    for phrase in analysis.quoted_phrases:
        cleaned = phrase.replace('"', " ").strip()
        if cleaned:
            parts.append(f'"{cleaned}"')
    for term in analysis.terms:
        cleaned = term.replace('"', "").strip("-'")
        if cleaned:
            parts.append(f'"{cleaned}"')
    return " OR ".join(dict.fromkeys(parts))


def lexical_scores(units: list[ContextUnit], analysis: PurposeAnalysis) -> dict[str, float]:
    if not analysis.has_lexical_query or not units:
        return {}
    if not _fts5_available():
        return _fallback_lexical_scores(units, analysis)
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE u USING fts5(rid UNINDEXED, body, tokenize='porter unicode61')")
        con.executemany("INSERT INTO u (rid, body) VALUES (?, ?)", [(u.record_id, u.text) for u in units])
        query = _fts_match_query(analysis)
        if not query:
            return {}
        try:
            rows = con.execute(
                "SELECT rid, bm25(u) AS s FROM u WHERE u MATCH ? ORDER BY s", (query,)
            ).fetchall()
        except sqlite3.OperationalError:
            return _fallback_lexical_scores(units, analysis)
    finally:
        con.close()
    if not rows:
        return {}
    # bm25(): more negative == more relevant. Flip and scale to best == 1.0
    # (units with no match are simply absent -> score 0).
    relevance = {rid: -s for rid, s in rows}
    hi = max(relevance.values()) or 1.0
    return {rid: max(0.05, v / hi) for rid, v in relevance.items()}


def _fallback_lexical_scores(units: list[ContextUnit], analysis: PurposeAnalysis) -> dict[str, float]:
    """Used only when the sqlite build lacks FTS5: plain normalized
    term-overlap, still lexical, just not BM25-weighted."""
    wanted = {t.lower() for t in analysis.terms}
    for phrase in analysis.quoted_phrases:
        wanted.update(w.lower() for w in phrase.split())
    if not wanted:
        return {}
    raw: dict[str, float] = {}
    for unit in units:
        body = set(re.findall(r"[a-z0-9']+", unit.text.lower()))
        overlap = len(wanted & body)
        if overlap:
            raw[unit.record_id] = overlap / len(wanted)
    if not raw:
        return {}
    hi = max(raw.values()) or 1.0
    return {rid: max(0.05, v / hi) for rid, v in raw.items()}


# --- semantic retrieval --------------------------------------------------


def semantic_scores(
    person_id: str,
    analysis: PurposeAnalysis,
    semantic_index: SemanticIndex,
    *,
    limit: int = 60,
    similarity_threshold: float = 0.22,
    top_records: int = 12,
) -> dict[str, float]:
    matches = semantic_index.search_person(
        analysis.normalized or analysis.raw,
        person_id,
        limit=limit,
        similarity_threshold=similarity_threshold,
    )
    if not matches:
        return {}
    per_record: dict[str, float] = {}
    for m in matches:  # already filtered to cosine >= threshold
        record_id = SUMMARY_RECORD_ID if m.local_id is None else m.local_id
        per_record[record_id] = max(per_record.get(record_id, 0.0), m.score)
    # Keep only the strongest matches: in a topically homogeneous record
    # (all facts about one dating contact) almost everything clears a low
    # absolute threshold, so focus on the top handful and scale to best.
    ordered = sorted(per_record.items(), key=lambda kv: -kv[1])[:top_records]
    hi = ordered[0][1] or 1.0
    lo = ordered[-1][1]
    span = (hi - lo) or hi
    return {k: 0.1 + 0.9 * ((v - lo) / span) for k, v in ordered}


# --- ranking -----------------------------------------------------------

_KIND_ORDER = {"summary": 0, "relationship": 1, "fact": 2, "interaction": 3}


@dataclass
class RankWeights:
    lexical: float = 0.5
    semantic: float = 0.5
    exact_hit: float = 0.35
    event_pref: float = 0.15
    present_pref: float = 0.12
    preference_pref: float = 0.22
    recency: float = 0.15
    summary_identity: float = 0.25
    low_confidence_penalty: float = 0.04


_INTEREST_SECTIONS = {"Interests", "General Notes"}


def _exact_hit(unit: ContextUnit, analysis: PurposeAnalysis) -> bool:
    hay = unit.text.lower()
    for phrase in analysis.quoted_phrases:
        if phrase.lower() in hay:
            return True
    for noun in analysis.proper_nouns:
        if re.search(rf"\b{re.escape(noun.lower())}\b", hay):
            return True
    for term in analysis.terms:
        if len(term) >= 4 and re.search(rf"\b{re.escape(term.lower())}", hay):
            return True
    return False


def rank_units(
    units: list[ContextUnit],
    analysis: PurposeAnalysis,
    *,
    semantic_available: bool,
    now: dt.date,
    weights: RankWeights | None = None,
) -> list[ContextUnit]:
    w = weights or RankWeights()
    w_lex = w.lexical if semantic_available else 1.0
    w_sem = w.semantic if semantic_available else 0.0

    dated = sorted(
        (u for u in units if u.kind == "interaction" and u.date),
        key=lambda u: u.date or "",
        reverse=True,
    )
    recency_rank = {u.record_id: i for i, u in enumerate(dated)}

    for unit in units:
        base = w_lex * unit.lexical + w_sem * unit.semantic
        boost = 0.0
        if _exact_hit(unit, analysis):
            boost += w.exact_hit
        if analysis.wants_events and unit.kind == "interaction":
            boost += w.event_pref
        if analysis.wants_present_state and unit.kind in ("summary", "fact", "relationship"):
            boost += w.present_pref
            if unit.section in _PRESENT_STATE_SECTIONS:
                boost += w.present_pref / 2
        if analysis.wants_preferences and unit.section in _INTEREST_SECTIONS:
            boost += w.preference_pref
        if analysis.is_advisory and unit.section == "Relationship with User":
            boost += w.present_pref
        if analysis.is_temporal and unit.record_id in recency_rank and dated:
            boost += w.recency * (1 - recency_rank[unit.record_id] / max(1, len(dated)))
        if unit.kind == "summary" and (analysis.is_briefing or analysis.wants_present_state):
            boost += w.summary_identity
        # A tentative read is exactly what an advisory question wants, so
        # don't penalize medium/low confidence there.
        if unit.confidence in ("medium", "low") and not analysis.is_advisory:
            boost -= w.low_confidence_penalty
        unit.score = base + boost

    return sorted(
        units,
        key=lambda u: (-u.score, _KIND_ORDER.get(u.kind, 9), u._sort_key),
    )


# --- dedupe & diversify ----------------------------------------------


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", text.lower()))


def dedupe(units: list[ContextUnit], *, near_threshold: float = 0.85) -> list[ContextUnit]:
    kept: list[ContextUnit] = []
    seen_exact: set[str] = set()
    for unit in units:  # already score-sorted; first occurrence wins
        norm = " ".join(unit.text.lower().split())
        if norm in seen_exact:
            continue
        u_tokens = _tokens(unit.text)
        dup = False
        for prior in kept:
            if prior.kind != unit.kind and not (
                {prior.kind, unit.kind} <= {"fact", "relationship"}
            ):
                continue
            p_tokens = _tokens(prior.text)
            union = u_tokens | p_tokens
            if union and len(u_tokens & p_tokens) / len(union) >= near_threshold:
                dup = True
                break
        if dup:
            continue
        seen_exact.add(norm)
        kept.append(unit)
    return kept


def diversify_for_briefing(units: list[ContextUnit]) -> list[ContextUnit]:
    """Round-robin across evidence categories so a broad briefing spans
    identity / relationship / interests / history / recent interactions
    rather than 20 facts from one section."""
    buckets: dict[str, list[ContextUnit]] = {}
    for unit in units:
        if unit.kind == "summary":
            key = "summary"
        elif unit.kind == "relationship":
            key = "relationship"
        elif unit.kind == "interaction":
            key = "interaction"
        else:
            key = f"fact:{unit.section or 'General Notes'}"
        buckets.setdefault(key, []).append(unit)

    order = ["summary", "relationship", *[f"fact:{c}" for c in CATEGORIES], "interaction"]
    ordered_keys = [k for k in order if k in buckets] + [k for k in buckets if k not in order]
    result: list[ContextUnit] = []
    while any(buckets[k] for k in ordered_keys):
        for k in ordered_keys:
            if buckets[k]:
                result.append(buckets[k].pop(0))
    return result


# --- result ---------------------------------------------------------


@dataclass
class PersonContextResult:
    person_id: str
    person_name: str
    purpose: str
    detail_level: str
    summary: ContextUnit | None
    facts: list[ContextUnit]
    relationships: list[ContextUnit]
    interactions: list[ContextUnit]
    semantic_available: bool
    semantic_note: str | None
    truncated: bool
    returned_items: int
    additional_matching_items: int
    approximate_tokens: int
    budget_tokens: int
    temporal: dict = field(default_factory=dict)
    is_temporal: bool = False
    wants_events: bool = False
    is_briefing: bool = False

    def _show_fact_date(self, unit: ContextUnit) -> bool:
        return bool(unit.date) and (
            self.is_temporal or self.detail_level == "comprehensive" or unit.section == "Life Events"
        )

    def _show_section(self) -> bool:
        return self.detail_level != "brief" or self.is_briefing

    def _fact_payload(self, unit: ContextUnit) -> dict:
        out: dict = {"id": unit.record_id, "text": unit.text}
        if unit.related_person_id:
            out["related_person_id"] = unit.related_person_id
        if self._show_section() and unit.section:
            out["section"] = unit.section
        if self._show_fact_date(unit):
            out["date"] = unit.date
        if unit.confidence and unit.confidence != "high":
            out["confidence"] = unit.confidence
        return out

    def _interaction_payload(self, unit: ContextUnit) -> dict:
        out: dict = {"id": unit.record_id, "text": unit.text}
        if unit.date and (self.is_temporal or self.wants_events or self.detail_level != "brief"):
            out["date"] = unit.date
        if unit.channel and (self.wants_events or self.detail_level == "comprehensive"):
            out["channel"] = unit.channel
        return out

    def to_payload(self) -> dict:
        retrieval: dict = {
            "semantic_available": self.semantic_available,
            "truncated": self.truncated,
            "returned_items": self.returned_items,
            "additional_matching_items": self.additional_matching_items,
            "approximate_tokens": self.approximate_tokens,
            "budget_tokens": self.budget_tokens,
        }
        if self.semantic_note:
            retrieval["note"] = self.semantic_note
        payload: dict = {
            "person": {"id": self.person_id, "name": self.person_name},
            "purpose": self.purpose,
            "detail_level": self.detail_level,
            "summary": (
                {"id": self.summary.record_id, "text": self.summary.text} if self.summary else None
            ),
            "facts": [self._fact_payload(u) for u in self.facts],
            "relationships": [self._fact_payload(u) for u in self.relationships],
            "interactions": [self._interaction_payload(u) for u in self.interactions],
            "retrieval": retrieval,
        }
        if self.temporal:
            payload["temporal"] = self.temporal
        return payload


# Per-detail-level selection: how strict the relevance gate is (as a
# fraction of the top-ranked unit's score) and the soft item cap.
_SELECTION = {
    "brief": (0.55, 5),
    "standard": (0.42, 10),
    "comprehensive": (0.0, 10_000),
}
_ABS_FLOOR = 0.12
_STRONG_LEXICAL = 0.6


def build_person_context(
    person: Person,
    purpose: str,
    *,
    detail_level: str = "standard",
    budget_tokens: int,
    semantic_index: SemanticIndex | None = None,
    semantic_stale_reason: str | None = None,
    now: dt.date | None = None,
    lexical_enabled: bool = True,
) -> PersonContextResult:
    now = now or dt.date.today()
    analysis = analyze_purpose(purpose, subject_name=person.name)
    units = build_units(person)
    narrow_lexical = (
        analysis.has_lexical_query
        and not (detail_level == "comprehensive" or analysis.is_briefing or analysis.wants_present_state)
    )

    lex = lexical_scores(units, analysis) if lexical_enabled else {}
    semantic_available = semantic_index is not None and semantic_stale_reason is None
    semantic_note = semantic_stale_reason
    sem: dict[str, float] = {}
    if semantic_available:
        try:
            broad = detail_level == "comprehensive" or analysis.is_briefing or analysis.is_advisory
            sem = semantic_scores(
                person.id,
                analysis,
                semantic_index,
                similarity_threshold=0.22,
                top_records=40 if broad else 12,
            )  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 -- provider failure must not fail retrieval
            semantic_available = False
            semantic_note = f"semantic provider unavailable ({type(exc).__name__}); lexical results only"
            sem = {}
    elif semantic_index is None:
        semantic_note = semantic_note or "semantic index not configured; lexical results only"

    for unit in units:
        unit.lexical = lex.get(unit.record_id, 0.0)
        unit.semantic = sem.get(unit.record_id, 0.0)

    ranked = rank_units(units, analysis, semantic_available=semantic_available, now=now)

    keep_all = detail_level == "comprehensive" or analysis.is_briefing
    floor_frac, item_cap = _SELECTION[detail_level]
    top_score = ranked[0].score if ranked else 0.0
    narrow_gate = max(_ABS_FLOOR, floor_frac * top_score)

    def _has_signal(u: ContextUnit) -> bool:
        return u.lexical > 0 or u.semantic > 0 or _exact_hit(u, analysis)

    def _passes(u: ContextUnit) -> bool:
        if keep_all:
            return True
        matched_lexically = u.lexical > 0 or _exact_hit(u, analysis)
        if u.lexical >= _STRONG_LEXICAL:  # exact term hit: high precision, always keep
            return True
        if narrow_lexical:
            # a specific factual lookup: require an actual term/phrase hit
            # (or an unmistakable semantic hit), not just topical similarity
            return (matched_lexically or u.semantic >= 0.7) and u.score >= narrow_gate
        if u.kind == "summary" and analysis.wants_present_state:
            return True
        # broad / present-state / advisory: any real signal, ranked & capped
        return _has_signal(u) and u.score >= _ABS_FLOOR

    relevant = [u for u in ranked if _passes(u)]
    if not relevant:  # nothing matched: fall back to a light identity view
        relevant = [u for u in ranked if u.kind in ("summary", "relationship")][:3]

    relevant = dedupe(relevant)
    if keep_all:
        relevant = diversify_for_briefing(relevant)

    # A narrow exact lookup can skip the person summary when the summary
    # itself wasn't a match (spec: omit it when it would just spend budget).
    if not keep_all and not analysis.wants_present_state:
        relevant = [
            u for u in relevant if not (u.kind == "summary" and u.lexical == 0 and u.semantic == 0)
        ]

    selected: list[ContextUnit] = []
    used = 0
    additional = 0
    for unit in relevant:
        cost = unit.approx_tokens()
        over_cap = len(selected) >= item_cap
        fits = used + cost <= budget_tokens
        if not selected or fits and not over_cap:  # always return at least the single best unit
            selected.append(unit)
            used += cost
        else:
            additional += 1

    truncated = additional > 0

    summary_unit = next((u for u in selected if u.kind == "summary"), None)
    facts = [u for u in selected if u.kind == "fact"]
    relationships = [u for u in selected if u.kind == "relationship"]
    interactions = [u for u in selected if u.kind == "interaction"]

    temporal = _temporal_block(person, analysis, detail_level, now)

    return PersonContextResult(
        person_id=person.id,
        person_name=person.name,
        purpose=analysis.raw,
        detail_level=detail_level,
        summary=summary_unit,
        facts=facts,
        relationships=relationships,
        interactions=interactions,
        semantic_available=semantic_available,
        semantic_note=semantic_note,
        truncated=truncated,
        returned_items=len(selected),
        additional_matching_items=additional,
        approximate_tokens=used,
        budget_tokens=budget_tokens,
        temporal=temporal,
        is_temporal=analysis.is_temporal,
        wants_events=analysis.wants_events,
        is_briefing=analysis.is_briefing,
    )


def _temporal_block(
    person: Person, analysis: PurposeAnalysis, detail_level: str, now: dt.date
) -> dict:
    if not (analysis.is_temporal or detail_level == "comprehensive"):
        return {}
    last = person.last_interaction_date()
    if last is None:
        return {"last_recorded_interaction": None}
    latest = max(person.interactions, key=lambda i: i.date.latest())
    block: dict = {
        "last_recorded_interaction": last.to_string(),
        "supporting_interaction_id": latest.id,
    }
    # Only compute elapsed days when the supporting date is full precision.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", last.to_string()):
        block["days_since_last_recorded_interaction"] = (now - dt.date.fromisoformat(last.to_string())).days
    else:
        block["days_since_last_recorded_interaction"] = None
        block["note"] = "last interaction date is not full-precision; elapsed days not computed"
    return block
