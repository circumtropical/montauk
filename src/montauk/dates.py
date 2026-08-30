"""Variable-precision dates for facts/interactions (YYYY-MM-DD / YYYY-MM / YYYY)
and birthdays (YYYY-MM-DD with an optional year), per spec sections 10, 12, 26.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema

_FULL_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_YEAR_RE = re.compile(r"^(\d{4})$")
_MONTH_DAY_RE = re.compile(r"^(\d{2})-(\d{2})$")


class DatePrecision(str, Enum):
    DAY = "day"
    MONTH = "month"
    YEAR = "year"


def _require_valid_calendar_date(year: int, month: int, day: int) -> None:
    try:
        dt.date(year, month, day)
    except ValueError as exc:
        raise ValueError(f"invalid calendar date {year:04d}-{month:02d}-{day:02d}: {exc}") from exc


def _require_valid_month(month: int) -> None:
    if not (1 <= month <= 12):
        raise ValueError(f"invalid month {month}: must be 1-12")


@dataclass(frozen=True, slots=True)
class FlexDate:
    """A date with variable precision: full day, year-month, or year only."""

    year: int
    month: int | None = None
    day: int | None = None

    def __post_init__(self) -> None:
        if not (1 <= self.year <= 9999):
            raise ValueError(f"invalid year {self.year}: must be 1-9999")
        if self.day is not None and self.month is None:
            raise ValueError("a FlexDate with a day must also have a month")
        if self.month is not None:
            _require_valid_month(self.month)
        if self.day is not None:
            assert self.month is not None
            _require_valid_calendar_date(self.year, self.month, self.day)

    @property
    def precision(self) -> DatePrecision:
        if self.day is not None:
            return DatePrecision.DAY
        if self.month is not None:
            return DatePrecision.MONTH
        return DatePrecision.YEAR

    @classmethod
    def parse(cls, value: Any) -> FlexDate:
        """Parse YAML front-matter/body date values: a native date/datetime
        (PyYAML auto-resolves unquoted YYYY-MM-DD to `datetime.date`), a bare
        int year, or a YYYY-MM-DD / YYYY-MM / YYYY string.
        """
        if isinstance(value, FlexDate):
            return value
        if isinstance(value, dt.datetime):
            value = value.date()
        if isinstance(value, dt.date):
            return cls(value.year, value.month, value.day)
        if isinstance(value, bool):
            raise ValueError(f"invalid date value: {value!r}")
        if isinstance(value, int):
            return cls(value)
        if isinstance(value, str):
            s = value.strip()
            if not s:
                raise ValueError("date string is empty")
            if m := _FULL_RE.match(s):
                y, mo, d = (int(g) for g in m.groups())
                return cls(y, mo, d)
            if m := _MONTH_RE.match(s):
                y, mo = (int(g) for g in m.groups())
                return cls(y, mo)
            if m := _YEAR_RE.match(s):
                return cls(int(m.group(1)))
            raise ValueError(f"unrecognized date format {s!r}: expected YYYY-MM-DD, YYYY-MM, or YYYY")
        raise ValueError(f"unsupported date value type {type(value).__name__}: {value!r}")

    def to_string(self) -> str:
        if self.day is not None:
            return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"
        if self.month is not None:
            return f"{self.year:04d}-{self.month:02d}"
        return f"{self.year:04d}"

    def earliest(self) -> dt.date:
        """The earliest calendar date consistent with this precision."""
        return dt.date(self.year, self.month or 1, self.day or 1)

    def latest(self) -> dt.date:
        """The latest calendar date consistent with this precision.

        Used to anchor cadence/overdue comparisons: a partial-precision
        interaction date is given the benefit of the doubt (treated as
        having happened as recently as the precision allows) rather than
        being flagged overdue purely because of imprecise recording.
        """
        if self.day is not None:
            return dt.date(self.year, self.month, self.day)  # type: ignore[arg-type]
        if self.month is not None:
            last_day = calendar.monthrange(self.year, self.month)[1]
            return dt.date(self.year, self.month, last_day)
        return dt.date(self.year, 12, 31)

    def __lt__(self, other: FlexDate) -> bool:
        return self.latest() < other.latest()

    def __le__(self, other: FlexDate) -> bool:
        return self.latest() <= other.latest()

    def __gt__(self, other: FlexDate) -> bool:
        return self.latest() > other.latest()

    def __ge__(self, other: FlexDate) -> bool:
        return self.latest() >= other.latest()

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.no_info_plain_validator_function(
            cls.parse,
            json_schema_input_schema=core_schema.str_schema(),
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda v: v.to_string(), return_schema=core_schema.str_schema()
            ),
        )


@dataclass(frozen=True, slots=True)
class Birthday:
    """A birthday: month and day are always known; year is optional."""

    month: int
    day: int
    year: int | None = None

    def __post_init__(self) -> None:
        _require_valid_month(self.month)
        if self.year is not None:
            if not (1 <= self.year <= 9999):
                raise ValueError(f"invalid birthday year {self.year}: must be 1-9999")
            _require_valid_calendar_date(self.year, self.month, self.day)
        else:
            # No year known: validate day against a leap year so Feb 29
            # birthdays remain representable without pinning a year.
            _require_valid_calendar_date(2000, self.month, self.day)

    @classmethod
    def parse(cls, value: Any) -> Birthday:
        if isinstance(value, Birthday):
            return value
        if isinstance(value, dt.datetime):
            value = value.date()
        if isinstance(value, dt.date):
            return cls(value.month, value.day, value.year)
        if isinstance(value, str):
            s = value.strip()
            if not s:
                raise ValueError("birthday string is empty")
            if m := _FULL_RE.match(s):
                y, mo, d = (int(g) for g in m.groups())
                return cls(mo, d, y)
            if m := _MONTH_DAY_RE.match(s):
                mo, d = (int(g) for g in m.groups())
                return cls(mo, d, None)
            raise ValueError(f"unrecognized birthday format {s!r}: expected YYYY-MM-DD or MM-DD")
        raise ValueError(f"unsupported birthday value type {type(value).__name__}: {value!r}")

    def to_string(self) -> str:
        if self.year is not None:
            return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"
        return f"{self.month:02d}-{self.day:02d}"

    def next_occurrence(self, today: dt.date) -> dt.date:
        """The next occurrence of this birthday on or after `today`.

        A Feb 29 birthday is observed on Feb 28 in non-leap years.
        """
        for candidate_year in (today.year, today.year + 1):
            day = self.day
            if self.month == 2 and self.day == 29 and not calendar.isleap(candidate_year):
                day = 28
            occurrence = dt.date(candidate_year, self.month, day)
            if occurrence >= today:
                return occurrence
        raise AssertionError("unreachable: next year's occurrence is always >= today")

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.no_info_plain_validator_function(
            cls.parse,
            json_schema_input_schema=core_schema.str_schema(),
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda v: v.to_string(), return_schema=core_schema.str_schema()
            ),
        )
