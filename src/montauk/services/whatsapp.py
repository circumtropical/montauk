"""Parser for a WhatsApp chat text export (spec 15).

WhatsApp exports carry no message IDs and vary by platform and locale.
This parser handles the two common header shapes --

    [2024-01-15, 9:41:23 AM] Alice: hello         (iOS)
    1/15/24, 9:41 AM - Alice: hello               (Android)

-- plus 24-hour times, D/M vs M/D date order (decided per file), multi-line
messages, system notices, and media placeholders (kept as a placeholder,
never the binary).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

PARSER_VERSION = "wa-1"

# The LRM / LTR marks WhatsApp injects before dates and media lines.
_INVISIBLE = str.maketrans({"‎": "", "‏": "", "﻿": ""})

# date: 1-4 digit / 1-4 digit / 2-4 digit, with -, / or . separators
_DATE = r"(\d{1,4})[/.\-](\d{1,4})[/.\-](\d{2,4})"
_TIME = r"(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s? ?([APap][.\s]?[Mm]\.?))?"

_IOS = re.compile(rf"^\[{_DATE},\s+{_TIME}\]\s+(.*)$")
_ANDROID = re.compile(rf"^{_DATE},\s+{_TIME}\s+-\s+(.*)$")

_MEDIA_RE = re.compile(
    r"^(?:‎)?(?:<Media omitted>|image omitted|video omitted|audio omitted|"
    r"sticker omitted|GIF omitted|Contact card omitted|document omitted|"
    r"This message was edited|.*\.(?:vcf|webp)\s*\(file attached\))\s*$",
    re.IGNORECASE,
)
_SYSTEM_PATTERNS = (
    "Messages and calls are end-to-end encrypted",
    "You deleted this message",
    "This message was deleted",
    "created group",
    "changed the subject",
    "changed this group's icon",
    "changed their phone number",
    "added you",
    "left",
    "joined using this group's invite link",
    "Missed voice call",
    "Missed video call",
    "turned on disappearing messages",
    "turned off disappearing messages",
)


@dataclass
class ParsedMessage:
    sent_at: dt.datetime
    sender: str | None
    text: str
    is_system: bool = False
    media_omitted: bool = False


@dataclass
class ParsedTranscript:
    messages: list[ParsedMessage]
    participants: list[str]  # distinct non-system senders, first-seen order
    warnings: list[str] = field(default_factory=list)

    @property
    def date_range(self) -> tuple[dt.datetime, dt.datetime] | None:
        if not self.messages:
            return None
        return self.messages[0].sent_at, self.messages[-1].sent_at


def _headers(lines: list[str]) -> list[tuple[re.Match[str], bool]]:
    out: list[tuple[re.Match[str], bool]] = []
    for line in lines:
        if m := _IOS.match(line):
            out.append((m, True))
        elif m := _ANDROID.match(line):
            out.append((m, False))
    return out


def _decide_day_first(headers: list[tuple[re.Match[str], bool]], warnings: list[str]) -> bool:
    firsts = [int(m.group(1)) for m, _ in headers]
    seconds = [int(m.group(2)) for m, _ in headers]
    first_gt12 = any(v > 12 for v in firsts)
    second_gt12 = any(v > 12 for v in seconds)
    if first_gt12 and not second_gt12:
        return True
    if second_gt12 and not first_gt12:
        return False
    if first_gt12 and second_gt12:
        warnings.append("ambiguous date order in this export; assumed day/month/year")
        return True
    return False  # all components <= 12 -> assume US-style month/day/year


def _parse_time(hh: str, mm: str, ss: str | None, ampm: str | None) -> tuple[int, int, int]:
    h, m, s = int(hh), int(mm), int(ss or 0)
    if ampm:
        pm = ampm.strip().lower().startswith("p")
        h = h % 12 + (12 if pm else 0)
    return h, m, s


def _parse_header(m: re.Match[str], *, day_first: bool) -> tuple[dt.datetime, str] | None:
    a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    day, month = (a, b) if day_first else (b, a)
    if a > 31 and b <= 12:  # leading component is actually a 4-digit year (YYYY-MM-DD)
        year, month, day = a, b, y
    else:
        year = y + 2000 if y < 100 else y
    try:
        h, mi, s = _parse_time(m.group(4), m.group(5), m.group(6), m.group(7))
        return dt.datetime(year, month, day, h, mi, s, tzinfo=dt.UTC), m.group(8)
    except ValueError:
        return None


def _classify(sender: str | None, body: str) -> tuple[bool, bool]:
    if _MEDIA_RE.match(body):
        return False, True
    if sender is None:
        return True, False
    return any(p in body for p in _SYSTEM_PATTERNS), False


def parse_whatsapp_export(content: bytes) -> ParsedTranscript:
    text = content.decode("utf-8", "replace").translate(_INVISIBLE)
    lines = text.replace("\r\n", "\n").split("\n")
    warnings: list[str] = []
    headers = _headers(lines)
    if not headers:
        return ParsedTranscript([], [], ["no recognizable WhatsApp messages in this file"])
    day_first = _decide_day_first(headers, warnings)

    messages: list[ParsedMessage] = []
    seen: dict[str, None] = {}
    unparsed = 0
    for line in lines:
        m = _IOS.match(line) or _ANDROID.match(line)
        if m is None:
            if messages and line.strip():  # continuation of the previous message
                messages[-1].text += "\n" + line
            continue
        parsed = _parse_header(m, day_first=day_first)
        if parsed is None:
            unparsed += 1
            continue
        sent_at, rest = parsed
        sender: str | None
        if ": " in rest and not rest.startswith("http"):
            raw_sender, body = rest.split(": ", 1)
            sender = raw_sender.strip() or None
        else:
            sender, body = None, rest
        is_system, media = _classify(sender, body)
        if sender is not None and not is_system:
            seen.setdefault(sender, None)
        messages.append(ParsedMessage(sent_at, sender, body, is_system=is_system, media_omitted=media))

    if unparsed:
        warnings.append(f"{unparsed} message header(s) had an unparseable date/time and were skipped")
    return ParsedTranscript(messages, list(seen), warnings)
