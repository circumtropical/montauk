"""Derived, fully disposable SQLite index over the canonical Markdown store
(spec section 17). Every row here must be re-derivable from Markdown alone;
deleting this database and rebuilding must produce an equivalent index
(spec section 37 acceptance criterion). Never store anything here that
isn't recomputable -- e.g. agent credentials live in a separate,
non-derived store (auth.py), never in this file.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .markdown_store import MarkdownStore
from .models import Person
from .reconciliation import ScanResult

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS people (
  person_id                    TEXT PRIMARY KEY,
  name                         TEXT NOT NULL,
  aliases_json                 TEXT NOT NULL DEFAULT '[]',
  birthday_month               INTEGER,
  birthday_day                 INTEGER,
  birthday_year                INTEGER,
  location                     TEXT,
  company                      TEXT,
  job_title                    TEXT,
  desired_contact_cadence_days INTEGER,
  summary                      TEXT,
  contact_emails_json          TEXT NOT NULL DEFAULT '[]',
  contact_phones_json          TEXT NOT NULL DEFAULT '[]',
  contact_address              TEXT,
  contact_messaging_json       TEXT NOT NULL DEFAULT '{}',
  last_interaction_at          TEXT,
  file_path                    TEXT NOT NULL,
  content_hash                 TEXT NOT NULL,
  updated_at                   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_people_name ON people(name);
CREATE INDEX IF NOT EXISTS idx_people_cadence ON people(desired_contact_cadence_days);
CREATE INDEX IF NOT EXISTS idx_people_birthday ON people(birthday_month, birthday_day);

CREATE TABLE IF NOT EXISTS index_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# days_since_last_interaction is deliberately NOT a column (spec section
# 17): it must be computed at query time from last_interaction_at so it
# never goes stale.

_COLUMNS = (
    "person_id",
    "name",
    "aliases_json",
    "birthday_month",
    "birthday_day",
    "birthday_year",
    "location",
    "company",
    "job_title",
    "desired_contact_cadence_days",
    "summary",
    "contact_emails_json",
    "contact_phones_json",
    "contact_address",
    "contact_messaging_json",
    "last_interaction_at",
    "file_path",
    "content_hash",
    "updated_at",
)


def compute_content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class ReconcileStats:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0


def _row_params(person: Person, *, file_path: str, content_hash: str, updated_at: str) -> dict:
    birthday = person.birthday
    last_interaction = person.last_interaction_date()
    return {
        "person_id": person.id,
        "name": person.name,
        "aliases_json": json.dumps(person.aliases),
        "birthday_month": birthday.month if birthday else None,
        "birthday_day": birthday.day if birthday else None,
        "birthday_year": birthday.year if birthday else None,
        "location": person.location,
        "company": person.company,
        "job_title": person.job_title,
        "desired_contact_cadence_days": person.desired_contact_cadence_days,
        "summary": person.summary,
        "contact_emails_json": json.dumps(person.contact.emails),
        "contact_phones_json": json.dumps(person.contact.phones),
        "contact_address": person.contact.address,
        "contact_messaging_json": json.dumps(person.contact.messaging),
        "last_interaction_at": last_interaction.latest().isoformat() if last_interaction else None,
        "file_path": file_path,
        "content_hash": content_hash,
        "updated_at": updated_at,
    }


class SqliteIndex:
    """Connection + operations over the derived relational index
    (data/index/relationships.sqlite). Single-process, single-writer use
    is assumed -- see write_queue.py for the cross-request serialization
    guarantee this relies on.
    """

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SqliteIndex:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- single-row operations, used by mutation tools (later steps) --

    def upsert_person(self, person: Person, *, file_path: str, content_hash: str) -> None:
        params = _row_params(
            person,
            file_path=file_path,
            content_hash=content_hash,
            updated_at=dt.datetime.now(dt.UTC).isoformat(),
        )
        columns = ", ".join(_COLUMNS)
        placeholders = ", ".join(f":{c}" for c in _COLUMNS)
        updates = ", ".join(f"{c} = excluded.{c}" for c in _COLUMNS if c != "person_id")
        self._conn.execute(
            f"INSERT INTO people ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(person_id) DO UPDATE SET {updates}",
            params,
        )
        self._conn.commit()

    def remove_person(self, person_id: str) -> None:
        self._conn.execute("DELETE FROM people WHERE person_id = ?", (person_id,))
        self._conn.commit()

    def get_row(self, person_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM people WHERE person_id = ?", (person_id,)).fetchone()
        return dict(row) if row else None

    def list_all(self) -> list[dict]:
        return [dict(row) for row in self._conn.execute("SELECT * FROM people ORDER BY person_id")]

    def all_person_ids(self) -> set[str]:
        return {row["person_id"] for row in self._conn.execute("SELECT person_id FROM people")}

    # -- meta ------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM index_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO index_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    # -- bulk rebuild / reconciliation -----------------------------------

    def rebuild_from_scan(self, store: MarkdownStore, scan_result: ScanResult) -> None:
        """Full rebuild: drop and re-derive every row from an already-
        validated scan of the canonical Markdown store. Used by
        `montauk rebuild-index` and directly targets the "delete and
        rebuild produces an equivalent index" acceptance criterion.
        """
        self._conn.execute("DELETE FROM people")
        now = dt.datetime.now(dt.UTC).isoformat()
        for person_id, person in scan_result.valid.items():
            path = store.person_path(person_id)
            params = _row_params(
                person, file_path=str(path), content_hash=compute_content_hash(path), updated_at=now
            )
            columns = ", ".join(_COLUMNS)
            placeholders = ", ".join(f":{c}" for c in _COLUMNS)
            self._conn.execute(f"INSERT INTO people ({columns}) VALUES ({placeholders})", params)
        self._conn.commit()
        self.set_meta("last_reconciliation_at", now)
        self.set_meta("schema_version", "1")

    def reconcile(self, store: MarkdownStore, scan_result: ScanResult) -> ReconcileStats:
        """Incremental reconciliation: only re-derive rows whose on-disk
        content hash changed since the last scan; remove rows for people
        no longer active (archived, deleted, or now-invalid)."""
        existing_hashes = {
            row["person_id"]: row["content_hash"]
            for row in self._conn.execute("SELECT person_id, content_hash FROM people")
        }
        now = dt.datetime.now(dt.UTC).isoformat()
        seen: set[str] = set()
        inserted = updated = unchanged = 0
        for person_id, person in scan_result.valid.items():
            seen.add(person_id)
            path = store.person_path(person_id)
            content_hash = compute_content_hash(path)
            if person_id not in existing_hashes:
                self.upsert_person(person, file_path=str(path), content_hash=content_hash)
                inserted += 1
            elif existing_hashes[person_id] != content_hash:
                self.upsert_person(person, file_path=str(path), content_hash=content_hash)
                updated += 1
            else:
                unchanged += 1
        stale_ids = set(existing_hashes) - seen
        for person_id in stale_ids:
            self.remove_person(person_id)
        self.set_meta("last_reconciliation_at", now)
        return ReconcileStats(inserted=inserted, updated=updated, unchanged=unchanged, removed=len(stale_ids))
