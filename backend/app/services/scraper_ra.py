"""RetroAchievements as a media/metadata source.

RA hosts box art + title/ingame screenshots per game — often *unique* for hacks and
homebrew that other DBs don't carry. Also parses region/revision from the matched hash
name. Uses the RA game id (already the library anchor), or source_ids['ra'] to override.
No extra creds — reuses RA_USERNAME/RA_API_KEY.
"""
from __future__ import annotations
import httpx
from pathlib import Path
from app.core.config import settings
from app.core.hash_priority import parse_and_score
from app.db.models import Game, MediaAsset
import structlog

log = structlog.get_logger()

# RA GameExtended image field -> RomFleet media_type
_RA_IMG = {"ImageBoxArt": "boxfront", "ImageTitle": "titlescreen", "ImageIngame": "screenshot"}


def scrape_ra(db, game: Game, media_store: Path) -> dict:
    gid = (game.source_ids or {}).get("ra") or game.ra_game_id
    if not gid:
        return {"matched": False}
    from app.services.scraper_util import apply_fields, set_source, save_media
    set_source(game, "ra", gid, game.title)
    result: dict = {"matched": True, "ra_game_id": gid, "filled": [], "images": 0}

    rom = game.roms[0] if game.roms else None
    if rom and rom.ra_hash_name:
        ph = parse_and_score(md5="", name=rom.ra_hash_name, labels=[], patch_url=None)
        if ph.regions:
            result["filled"] += apply_fields(game, {"region": ", ".join(ph.regions)})
    game.metadata_source = game.metadata_source or "ra"

    http = httpx.Client(timeout=30, follow_redirects=True)
    try:
        from app.core.credentials import cred
        r = http.get(f"{settings.ra_api_base}/API_GetGameExtended.php",
                     params={"i": gid, "y": cred("ra_api_key"), "z": cred("ra_username")})
        r.raise_for_status()
        data = r.json() if isinstance(r.json(), dict) else {}
        # GetGameExtended already carries Developer / Publisher / Genre / Released in the SAME
        # response we were fetching purely for images -- and we were discarding all of it. That
        # is the whole reason the residual "incomplete" count is metadata-shaped: after the RA
        # art pass every system sat at incomplete == missing_meta (PSX 132/132, WASM-4 57/57).
        # It matters most for the systems ScreenScraper/IGDB have never heard of (WASM-4,
        # Uzebox, Apple II homebrew), where RA is the ONLY database that knows the game.
        # NB apply_fields skips blanks and locked fields, so this never overwrites better data.
        # RA has no description field, so `description` still has to come from elsewhere.
        rel = (data.get("Released") or "").strip()
        if rel and (data.get("ReleasedAtGranularity") or "") == "year":
            rel = rel[:4]                     # do not invent a day/month RA did not assert
        result["filled"] += apply_fields(game, {
            "developer": (data.get("Developer") or "").strip() or None,
            "publisher": (data.get("Publisher") or "").strip() or None,
            "genre": (data.get("Genre") or "").strip() or None,
            "release_date": rel or None,
        })
        for field, mt in _RA_IMG.items():
            path = data.get(field)
            if path and save_media(db, game, mt, "ra", settings.ra_media_base + path,
                                   media_store, http, ext=(Path(path).suffix or ".png")):
                result["images"] += 1
    finally:
        http.close()
    return result
