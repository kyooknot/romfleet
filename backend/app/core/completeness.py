"""Media/metadata completeness profile — what makes a game "complete", per category.

Like the priority-scoring profile: a default requirement set plus per-category overrides
(a game's `category` = LBDB ReleaseType: Released / Homebrew / ROM Hack / Unlicensed / …).
Anything NOT required is "bonus" — nice to have but doesn't affect the incomplete count.
Stored in the `settings` table under key 'completeness'.
"""
from __future__ import annotations
from app.db.models import Setting

# selectable requirement options (order = UI order)
ALL_MEDIA = ["boxfront", "boxback", "titlescreen", "screenshot", "logo",
             "background", "marquee", "video", "cart", "disc"]
ALL_META = ["developer", "publisher", "genre", "release_date", "description",
            "region", "players", "rating"]

DEFAULT_COMPLETENESS = {
    # normal releases: box art + title screen + gameplay shot + core metadata
    "default": {"media": ["boxfront", "titlescreen", "screenshot"],
                "meta": ["developer", "publisher", "genre", "release_date", "description"]},
    # homebrew / hacks / aftermarket: just the screenshots (box art is bonus)
    "categories": {
        "Homebrew":   {"media": ["titlescreen", "screenshot"], "meta": ["developer", "release_date", "description"]},
        "ROM Hack":   {"media": ["titlescreen", "screenshot"], "meta": ["release_date", "description"]},
        "Unlicensed": {"media": ["titlescreen", "screenshot"], "meta": ["developer", "release_date", "description"]},
    },
}


def get_completeness(db) -> dict:
    row = db.get(Setting, "completeness")
    cfg = {"default": dict(DEFAULT_COMPLETENESS["default"]),
           "categories": dict(DEFAULT_COMPLETENESS["categories"])}
    if row and row.value:
        cfg["default"] = row.value.get("default", cfg["default"])
        cfg["categories"] = row.value.get("categories", cfg["categories"])
    return cfg


def save_completeness(db, cfg: dict) -> dict:
    merged = {"default": cfg.get("default") or DEFAULT_COMPLETENESS["default"],
              "categories": cfg.get("categories") or {}}
    row = db.get(Setting, "completeness")
    if row:
        row.value = merged
    else:
        db.add(Setting(key="completeness", value=merged))
    db.commit()
    return merged


def profile_for(cfg: dict, category: str | None) -> dict:
    return (cfg.get("categories") or {}).get(category or "") or cfg.get("default", {})
