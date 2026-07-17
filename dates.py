"""
Minute Man v4 — due-date parsing (shared by crud.py at save time and the
schema v1→v2 migration backfill).

The rule (02-DATA-MODEL-V2, "Overdue definition"): `by_when` is free text
("Today", "This arvo", "Thursday", "Not specified — needs a date"). We parse
ONLY unambiguous forms into `actions.due_date`, anchored to the meeting's
`meeting_date`; everything else stays NULL — never guess.

Parse table (documented here as the spec requires):

  meeting_date anchor — tried in this order, else no anchor:
    * ISO            "2026-07-17"
    * NZ long form   "17 July 2026"  (what the front-end writes by default)
    * NZ slash form  "17/07/2026"

  by_when — normalised (lowercase, trimmed); ISO date anywhere in the text
  wins; otherwise the FIRST WORD must be the keyword (so "Today, after smoko"
  parses but "This arvo" does not):
    * "YYYY-MM-DD" anywhere      -> that date (needs no anchor)
    * first word "today"         -> meeting_date
    * first word "tomorrow"      -> meeting_date + 1 day
    * first word a weekday name  -> the next such weekday ON OR AFTER the
      meeting date ("Thursday" said on a Thursday means that day)
    * anything else              -> None  (e.g. "This arvo", "Before EOD",
      "Not specified — needs a date", "During welding")

Overdue = status "open" AND due_date < today. NULL due_date is never overdue
(the UI shows those amber "no date", not red).
"""

import re
from datetime import date, datetime, timedelta

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def parse_meeting_date(meeting_date_str: str | None) -> date | None:
    """Best-effort parse of the meeting_date string (kept free-text for API
    parity). Returns None when unparseable."""
    if not meeting_date_str:
        return None
    s = str(meeting_date_str).strip()
    for fmt in ("%Y-%m-%d", "%d %B %Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    m = _ISO_RE.search(s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def parse_due_date(by_when: str | None, meeting_date_str: str | None) -> date | None:
    """Parse `by_when` into a real date per the table above, or None."""
    if not by_when:
        return None
    text = str(by_when).strip().lower()
    if not text:
        return None

    # 1) an explicit ISO date anywhere in the text needs no anchor
    m = _ISO_RE.search(text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    anchor = parse_meeting_date(meeting_date_str)
    if anchor is None:
        return None  # relative terms can't be resolved without an anchor

    first = re.split(r"[\s,;.!]+", text, maxsplit=1)[0]
    if first == "today":
        return anchor
    if first == "tomorrow":
        return anchor + timedelta(days=1)
    if first in _WEEKDAYS:
        ahead = (_WEEKDAYS[first] - anchor.weekday()) % 7
        return anchor + timedelta(days=ahead)
    return None
