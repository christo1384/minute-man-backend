"""
Minute Man v4 — site/person normalisation & match-or-create.

The normalisation model (02-DATA-MODEL-V2): free text stays the source of
truth on the existing columns (`meetings.site_name`, `actions.who`,
`attendees.name`). The v2 `sites` / `people` tables sit alongside with
nullable FK columns. Matching is EXACT after normalisation (lowercase,
trimmed, whitespace collapsed) — no fuzzy auto-merging, ever; near-matches
are a UI affair for a later version.

Used by:
  * the v1→v2 Alembic migration (backfill of existing rows), and
  * crud.py at save time (new records get matched-or-created).
"""

import re

# Owner placeholder used by the engine when an action has no named person —
# never becomes a `people` row.
UNASSIGNED = "unassigned — needs an owner"


def normalize(text: str | None) -> str:
    """Lowercase, trim, collapse internal whitespace."""
    return re.sub(r"\s+", " ", str(text or "").strip()).lower()


def is_real_person(name: str | None) -> bool:
    n = normalize(name)
    return bool(n) and n != UNASSIGNED


class Matcher:
    """In-memory exact matcher over canonical names + aliases.

    Seed it with existing rows, then feed raw strings: `lookup` returns the
    row id or None; `remember` registers a newly created row. Alias tracking:
    a raw variant that normalises to an existing entry but differs from its
    canonical string is recorded so callers can persist it in `aliases`.
    """

    def __init__(self):
        self._by_norm: dict[str, int] = {}
        self._canonical: dict[int, str] = {}
        self._aliases: dict[int, list[str]] = {}

    def seed(self, row_id: int, canonical: str, aliases: list[str] | None = None):
        self._canonical[row_id] = canonical
        self._aliases[row_id] = list(aliases or [])
        self._by_norm[normalize(canonical)] = row_id
        for a in aliases or []:
            self._by_norm.setdefault(normalize(a), row_id)

    def lookup(self, raw: str) -> int | None:
        return self._by_norm.get(normalize(raw))

    def note_variant(self, row_id: int, raw: str) -> bool:
        """Record a new raw spelling for an existing row. Returns True when
        the aliases list changed (caller should persist it)."""
        raw = str(raw).strip()
        if raw == self._canonical.get(row_id) or raw in self._aliases.get(row_id, []):
            return False
        self._aliases.setdefault(row_id, []).append(raw)
        self._by_norm.setdefault(normalize(raw), row_id)
        return True

    def aliases(self, row_id: int) -> list[str]:
        return list(self._aliases.get(row_id, []))
