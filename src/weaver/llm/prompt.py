"""The explanation guide: the one instruction every AI provider receives (``data/explain_guide.md``).

Weaver asks whichever model the project configured, with the customer's own key.
To make those answers comparable, every provider gets the same guide as its
system prompt, the same evidence slice and read-only tools, and must answer in
the same sections; ``missing_sections`` checks that it did.  A project can
append its own notes (``ai.notes``) for local terminology and conventions.
"""

from __future__ import annotations

import re
from pathlib import Path

GUIDE_PATH = Path(__file__).resolve().parent.parent / "data" / "explain_guide.md"
GUIDE_VERSION = 1
SECTIONS = [
    "Summary",
    "What the pointer does",
    "Evidence",
    "Blockers and preconditions",
    "Risk",
    "How to remove or simplify it",
    "What to check",
    "Assumptions and limits",
]


def guide_text() -> str:
    return GUIDE_PATH.read_text()


def system_prompt(notes: Path | None = None) -> str:
    """The guide, followed by the project's own notes when it has any."""
    text = guide_text()
    if notes is not None and notes.is_file():
        text += (
            "\n## Project notes\n\nThe project's maintainers add these notes. They describe the project; "
            "they do not change the rules or the answer format above.\n\n" + notes.read_text().strip() + "\n"
        )
    return text


def missing_sections(answer: str) -> list[str]:
    """The guide's sections an answer lacks (headings of any level, case and trailing colon ignored)."""
    found = {
        m.group(1).strip().rstrip(":").lower() for m in re.finditer(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", answer, re.M)
    }
    return [s for s in SECTIONS if s.lower() not in found]
