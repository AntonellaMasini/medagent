"""Medical specialty value object backed by Cigna's official catalog.

Loaded from `data/cigna_specialties.json`, which was scraped via the Cigna
client portal's autocomplete API
(`/dm/api/auto-complete/specialties-medicalacts`). 194 entries split across
three types: SPECIALTY (65), SUB_SPECIALTY (57), MEDICAL_ACT (72).

Data quirk worth knowing: ~32 of the 194 entries — including critically
common ones like `PSICOLOGIA` and `ODONTOLOGIA` — have **empty catalogIds**
in Cigna's response. They also use no accents in `language_ES`, suggesting
they come from an older internal data tier. Because of this we use the
**name** (the unique, always-present `language_ES` value) as our canonical
identifier, with `catalog_id` kept as optional informational metadata.

This file's `search()` does Spanish-only free-text matching (substring,
common-prefix, small synonym dict). It's intentionally cheap and
deterministic so the common case ("psicólogo", "dermatóloga", "cardio")
costs nothing. Symptom-style queries ("me duele la cabeza") fall through
to None and are handled by a future LLM matcher (see issue #15).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

_DEFAULT_CATALOG_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "cigna_specialties.json"
)


class SpecialtyType(str, Enum):
    SPECIALTY = "SPECIALTY"
    SUB_SPECIALTY = "SUB_SPECIALTY"
    MEDICAL_ACT = "MEDICAL_ACT"


@dataclass(frozen=True)
class Specialty:
    """One row of Cigna's specialty/medical-act catalog.

    `name` is the canonical key (unique across all 194 entries; persisted on
    the Appointment row as a snapshot). `catalog_id` is Cigna's official ID
    when they have one, "" otherwise — informational, not the identifier.
    """

    name: str  # language_ES, e.g. "DERMATOLOGÍA" or "PSICOLOGIA" (sometimes no accents)
    type: SpecialtyType
    catalog_id: str = ""

    def __str__(self) -> str:
        return self.name


# ---- normalization helpers ----

_ACCENT_MAP = str.maketrans("áéíóúüÁÉÍÓÚÜñÑ", "aeiouuAEIOUUnN")


def _normalize(text: str) -> str:
    """Lowercase + strip accents + collapse whitespace."""
    return " ".join(text.translate(_ACCENT_MAP).lower().split())


def _common_prefix_len(a: str, b: str) -> int:
    i = 0
    for x, y in zip(a, b):
        if x != y:
            break
        i += 1
    return i


def _match_score(query: str, name: str) -> int:
    """Score how well `query` matches `name` (both pre-normalized). 0 = no match.

    Tiers, highest first. Anchored matches only — a query must align with the
    start of the name or the start of some word, with optional gender-suffix
    tolerance on the prefix. Bare substring matches (e.g. "un" inside
    "FUNCIONAL") are deliberately excluded; they generate too much noise from
    short Spanish stopwords.

      200 — exact match
      100 — full name starts with query
       90 — first word starts with query (head word match)
       80 — non-first word starts with query
       70 — gender-suffix-tolerant on the FIRST (head) word
            (catches "traumatologo" → head word "traumatologia")
       50 — gender-suffix-tolerant on a non-first word
            (catches "ginecologo" → "OBSTETRICIA Y GINECOLOGÍA")
    """
    if query == name:
        return 200
    if name.startswith(query):
        return 100
    words = name.split()
    if words and words[0].startswith(query):
        return 90
    for word in words[1:]:
        if word.startswith(query):
            return 80
    if words:
        common = _common_prefix_len(query, words[0])
        if common >= 5 and len(query) - common <= 2:
            return 70
    for word in words[1:]:
        common = _common_prefix_len(query, word)
        if common >= 5 and len(query) - common <= 2:
            return 50
    return 0


# ---- synonym dict ----
# Etymologically-different terms that wouldn't match by substring/prefix.
# Each value is the canonical `name` (language_ES) — same key the catalog
# uses. Keep this small; the long tail is the LLM matcher's job (issue #15).
_SYNONYMS: dict[str, str] = {
    # Practitioner nouns whose stem differs from the specialty name
    "dentista": "ODONTOLOGIA",
    "oculista": "OFTALMOLOGÍA",
    "ojos": "OFTALMOLOGÍA",
    "internista": "MEDICINA INTERNA",
    "orl": "OTORRINOLARINGOLOGÍA",
    "garganta": "OTORRINOLARINGOLOGÍA",
    # -terapeuta / -ología pairs (suffix delta > 2 chars, so the
    # gender-tolerant tier won't pick them up)
    "fisioterapeuta": "FISIOTERAPIA",
    "kinesiologo": "FISIOTERAPIA",
    "kinesiologa": "FISIOTERAPIA",
    "logopeda": "LOGOPEDIA Y FONIATRÍA",
    # Slang
    "terapeuta": "PSICOLOGIA",
    "loquero": "PSICOLOGIA",
}


# ---- the catalog ----

@dataclass
class SpecialtyCatalog:
    """In-memory index over the Cigna specialty catalog.

    Constructed once at module load. Tests can build their own from a fixture.
    """

    entries: tuple[Specialty, ...]

    def __post_init__(self) -> None:
        # Build lookups
        self._by_name: dict[str, Specialty] = {e.name: e for e in self.entries}
        self._by_norm_name: dict[str, Specialty] = {
            _normalize(e.name): e for e in self.entries
        }
        self._by_catalog_id: dict[str, Specialty] = {
            e.catalog_id: e for e in self.entries if e.catalog_id
        }

    # ---- direct lookups ----

    def find_by_name(self, name: str) -> Specialty | None:
        # Exact match first, then accent-insensitive.
        if (hit := self._by_name.get(name)) is not None:
            return hit
        return self._by_norm_name.get(_normalize(name))

    def find_by_catalog_id(self, catalog_id: str) -> Specialty | None:
        if not catalog_id:
            return None
        return self._by_catalog_id.get(catalog_id)

    # ---- free-text search ----

    _TYPE_PRIORITY = {
        SpecialtyType.SPECIALTY: 0,
        SpecialtyType.SUB_SPECIALTY: 1,
        SpecialtyType.MEDICAL_ACT: 2,
    }

    def search(self, query: str) -> Specialty | None:
        """Spanish free-text → Specialty, or None if no confident match.

        Strategy, in order:
          1. Synonym dict (etymologically-different aliases like "dentista").
          2. Exact match (accent-insensitive).
          3. Score every catalog entry against the query via `_match_score`.
          4. Prefer SPECIALTY > SUB_SPECIALTY > MEDICAL_ACT.
          5. Keep only the top-scoring candidates within that type.
          6. If multiple, pick the substantially-shorter one (≥4 chars shorter
             than the runner-up — typically the more general entry, e.g.
             "MEDICINA GENERAL" over "CIRUGÍA GENERAL Y DEL APARATO DIGESTIVO").
          7. Otherwise return None — the LLM matcher (issue #15) can take a
             swing on the ambiguous tail.
        """
        if not query:
            return None
        q = _normalize(query)
        # Whitespace-only inputs collapse to "" here, which would otherwise
        # score 100 against every entry (str.startswith("") is True). Bail
        # explicitly so the length-gap tiebreaker isn't load-bearing.
        if not q:
            return None

        # 1. Synonym lookup
        if (target := _SYNONYMS.get(q)) is not None:
            return self.find_by_name(target)

        # 2. Score everything. (Exact match becomes tier 200; we don't
        # short-circuit on it so that the SPECIALTY priority still wins
        # over a same-named MEDICAL_ACT — e.g. "obstetricia" picks
        # OBSTETRICIA Y GINECOLOGÍA, not the MEDICAL_ACT also called
        # OBSTETRICIA.)
        scored: list[tuple[Specialty, int]] = []
        for entry in self.entries:
            score = _match_score(q, _normalize(entry.name))
            if score > 0:
                scored.append((entry, score))
        if not scored:
            return None

        # 4. Pick the best type that has any match
        best_type = min(
            {entry.type for entry, _ in scored},
            key=lambda t: self._TYPE_PRIORITY[t],
        )
        scored = [(c, s) for c, s in scored if c.type == best_type]

        # 5. Keep only top-scoring candidates
        top_score = max(s for _, s in scored)
        top = [c for c, s in scored if s == top_score]

        if len(top) == 1:
            return top[0]

        # 6. Multiple at top score: substantially-shorter wins (avoids picking
        # one of many siblings, e.g. "infantil" matching all *_INFANTIL entries).
        top.sort(key=lambda c: len(c.name))
        if len(top[1].name) - len(top[0].name) >= 4:
            return top[0]

        # 7. Genuinely ambiguous
        return None


# ---- module-level singleton ----

_default_catalog: SpecialtyCatalog | None = None


def _load_from_file(path: Path) -> SpecialtyCatalog:
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = tuple(
        Specialty(
            name=item["language_ES"],
            type=SpecialtyType(item["type"]),
            catalog_id=item.get("catalogId") or "",
        )
        for item in data
    )
    return SpecialtyCatalog(entries=entries)


def get_catalog() -> SpecialtyCatalog:
    """Lazy-load the default catalog from data/cigna_specialties.json."""
    global _default_catalog
    if _default_catalog is None:
        _default_catalog = _load_from_file(_DEFAULT_CATALOG_PATH)
    return _default_catalog


def normalize_specialty(query: str) -> Specialty | None:
    """Public entry point used across the app. Thin wrapper around catalog.search."""
    return get_catalog().search(query)


__all__ = [
    "Specialty",
    "SpecialtyCatalog",
    "SpecialtyType",
    "get_catalog",
    "normalize_specialty",
]
