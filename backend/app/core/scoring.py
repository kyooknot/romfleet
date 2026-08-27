"""Priority-scoring profile — the user-editable "quality profile" for ROM selection.

Like Radarr/Sonarr quality profiles: an ordered region-preference list plus modifiers.
Stored in the `settings` table under key 'scoring'. `score_parsed()` turns a ParsedHash
(from core.hash_priority) into a score using this config — lower = more preferred.
Returns None for a hash the profile EXCLUDES (never selectable as primary).
"""
from __future__ import annotations
from app.db.models import Setting
from app.core.hash_priority import ParsedHash, _version_as_int

DEFAULT_SCORING = {
    # most-preferred first; "Other" catches anything unlisted
    "region_order": ["USA", "World", "Europe", "Australia", "Japan", "Korea", "Brazil", "China", "Other"],
    "prefer_higher_revision": True,
    "english_translation": "accept",          # accept | exclude
    "english_translation_after": "Australia",  # rank English translations just after this region
    "nonenglish_translation": "last",          # last | exclude
    "msu_penalty": True,                        # prefer the plain release over an MSU-1 build
    "nointro_over_rapatches": True,
}

_GAP = 10_000


def get_scoring(db) -> dict:
    row = db.get(Setting, "scoring")
    cfg = dict(DEFAULT_SCORING)
    if row and row.value:
        cfg.update(row.value)
    return cfg


def save_scoring(db, cfg: dict) -> dict:
    merged = {**DEFAULT_SCORING, **(cfg or {})}
    row = db.get(Setting, "scoring")
    if row:
        row.value = merged
    else:
        db.add(Setting(key="scoring", value=merged))
    db.commit()
    return merged


def score_parsed(parsed: ParsedHash, cfg: dict) -> int | None:
    order = cfg.get("region_order") or DEFAULT_SCORING["region_order"]
    rank = {r: i for i, r in enumerate(order)}
    other = rank.get("Other", len(order))
    best = min((rank.get(r, other) for r in (parsed.regions or ["Other"])), default=other)
    score = best * _GAP

    if parsed.is_translation and parsed.is_english_translation:
        if cfg.get("english_translation") == "exclude":
            return None
        after = rank.get(cfg.get("english_translation_after", "Australia"), other)
        score = min(score, int((after + 0.5) * _GAP))
        score -= _version_as_int(parsed.translation_version) * 10
    elif parsed.is_translation:
        if cfg.get("nonenglish_translation") == "exclude":
            return None
        score = (len(order) + 5) * _GAP           # rank last
        score -= _version_as_int(parsed.translation_version) * 10

    if cfg.get("msu_penalty", True) and parsed.is_msu:
        score += _GAP // 2
    if cfg.get("prefer_higher_revision", True):
        score -= parsed.revision * 100
    if cfg.get("nointro_over_rapatches", True) and parsed.label == "rapatches":
        score += 1
    return max(-2_000_000_000, min(2_000_000_000, score))
