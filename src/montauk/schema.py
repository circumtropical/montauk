"""Fixed narrative categories and confidence semantics (spec sections 11, 14).

Phase 1 deliberately uses a fixed schema to prevent category proliferation
(spec section 11). This module is the single source of truth for that list;
markdown_store.py, models.py, and the MCP tool schemas all import it from
here rather than redeclaring it.
"""

from __future__ import annotations

from enum import Enum
from typing import Final

CATEGORIES: Final[tuple[str, ...]] = (
    "Family",
    "Work & Education",
    "Interests",
    "Relationship with User",
    "Life Events",
    "General Notes",
)

GENERAL_NOTES_CATEGORY: Final[str] = "General Notes"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
