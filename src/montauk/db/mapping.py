"""Translation between the Phase 1 pydantic domain models
(:mod:`montauk.models`) and the PostgreSQL ORM rows (:mod:`montauk.db.models`).

The pydantic ``Person``/``Fact``/``Interaction`` stay the currency of the
domain and service layer; PostgreSQL is the canonical *storage*. Every
lossless Phase 1 field round-trips through here (spec 30.2).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from ..dates import Birthday, FlexDate
from ..models import ContactInfo, Fact, Interaction, Person, Source
from ..schema import Confidence
from . import models as orm

_LOCAL_ID_NUM_RE = re.compile(r"-(\d+)$")


def local_id_sort_key(local_id: str) -> tuple[int, str]:
    m = _LOCAL_ID_NUM_RE.search(local_id)
    return (int(m.group(1)) if m else 10**9, local_id)


# --- FlexDate <-> (text, precision, latest) ---------------------------


def flexdate_columns(value: FlexDate | None) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    return value.to_string(), value.precision.value


def flexdate_from_text(text: str | None) -> FlexDate | None:
    if not text:
        return None
    return FlexDate.parse(text)


def flexdate_required(text: str) -> FlexDate:
    return FlexDate.parse(text)


# --- ContactInfo <-> person_contact_methods --------------------------


def _normalize_contact_value(kind: str, value: str) -> str:
    v = value.strip()
    if kind == "email":
        return v.casefold()
    if kind == "phone":
        return re.sub(r"[^\d+]", "", v)
    return " ".join(v.split()).casefold()


def contact_methods_from_info(
    info: ContactInfo, *, workspace_id: object, person_id: object
) -> list[orm.PersonContactMethod]:
    rows: list[orm.PersonContactMethod] = []
    pos = 0
    for email in info.emails:
        rows.append(
            orm.PersonContactMethod(
                workspace_id=workspace_id,
                person_id=person_id,
                kind="email",
                value=email,
                value_normalized=_normalize_contact_value("email", email),
                position=pos,
            )
        )
        pos += 1
    for phone in info.phones:
        rows.append(
            orm.PersonContactMethod(
                workspace_id=workspace_id,
                person_id=person_id,
                kind="phone",
                value=phone,
                value_normalized=_normalize_contact_value("phone", phone),
                position=pos,
            )
        )
        pos += 1
    if info.address:
        rows.append(
            orm.PersonContactMethod(
                workspace_id=workspace_id,
                person_id=person_id,
                kind="address",
                value=info.address,
                value_normalized=_normalize_contact_value("address", info.address),
                position=pos,
            )
        )
        pos += 1
    for platform, handle in info.messaging.items():
        rows.append(
            orm.PersonContactMethod(
                workspace_id=workspace_id,
                person_id=person_id,
                kind="messaging",
                label=platform,
                value=handle,
                value_normalized=_normalize_contact_value("messaging", handle),
                position=pos,
            )
        )
        pos += 1
    return rows


def contact_info_from_methods(methods: Iterable[orm.PersonContactMethod]) -> ContactInfo:
    ordered = sorted(methods, key=lambda m: m.position)
    emails = [m.value for m in ordered if m.kind == "email"]
    phones = [m.value for m in ordered if m.kind == "phone"]
    address = next((m.value for m in ordered if m.kind == "address"), None)
    messaging = {m.label: m.value for m in ordered if m.kind == "messaging" and m.label}
    return ContactInfo(emails=emails, phones=phones, address=address, messaging=messaging)


# --- ORM Person -> domain Person -------------------------------------


def person_to_domain(row: orm.Person) -> Person:
    birthday = None
    if row.birthday_month is not None and row.birthday_day is not None:
        birthday = Birthday(month=row.birthday_month, day=row.birthday_day, year=row.birthday_year)

    facts = [
        Fact(
            id=f.local_id,
            category=f.category,
            date=flexdate_from_text(f.date_text),
            confidence=Confidence(f.confidence),
            text=f.text,
            related_person_id=f.related_person_public_id,
            sources=[Source(type=s.source_type, id=s.source_ref) for s in f.sources],
        )
        for f in sorted(row.facts, key=lambda f: local_id_sort_key(f.local_id))
    ]
    interactions = [
        Interaction(
            id=i.local_id,
            date=flexdate_required(i.date_text),
            channel=i.channel,
            connection_level=i.connection_level,
            summary=i.summary,
            sources=[Source(type=s.source_type, id=s.source_ref) for s in i.sources],
        )
        for i in sorted(row.interactions, key=lambda i: local_id_sort_key(i.local_id))
    ]
    return Person(
        id=row.public_id,
        name=row.name,
        aliases=[a.alias for a in sorted(row.aliases, key=lambda a: a.position)],
        birthday=birthday,
        location=row.location,
        company=row.company,
        job_title=row.job_title,
        desired_contact_cadence_days=row.desired_contact_cadence_days,
        summary=row.summary,
        contact=contact_info_from_methods(row.contact_methods),
        facts=facts,
        interactions=interactions,
    )
