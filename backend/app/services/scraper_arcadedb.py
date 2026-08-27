"""ArcadeDB (adb.arcadeitalia.net) scraper — the ArcadeDatabase by motoschifo.
Keyed by MAME/FBNeo romname (= our arcade zip filenames). Best arcade-specific media.
Attribution required: "Arcade Database by motoschifo". Single connection per IP — the
bulk loop is already serial, so no concurrency here."""
from __future__ import annotations
import httpx
from pathlib import Path
from app.db.models import Game

BASE = "https://adb.arcadeitalia.net/service_scraper.php"

# ArcadeDB media field -> RomFleet media_type (order = download priority; video HD before SD)
_MEDIA = [
    ("url_image_marquee", "marquee"),
    ("url_image_flyer", "boxfront"),
    ("url_image_title", "titlescreen"),
    ("url_image_ingame", "screenshot"),
    ("url_image_cabinet", "background"),
    ("url_video_shortplay_hd", "video"),
    ("url_video_shortplay", "video"),
]
_EXT = {"video": ".mp4"}   # images are PNG


class ArcadeDB:
    def __init__(self):
        self.http = httpx.Client(timeout=45, follow_redirects=True,
                                 headers={"User-Agent": "RomFleet/0.1 (+ArcadeDatabase by motoschifo)"})

    @property
    def configured(self) -> bool:
        return True   # public, no auth

    def lookup(self, romname: str) -> dict | None:
        try:
            r = self.http.get(BASE, params={"ajax": "query_mame", "game_name": romname, "use_parent": 1})
            if r.status_code != 200:
                return None
            res = r.json().get("result") or []
        except Exception:  # noqa
            return None
        j = res[0] if res else None
        # ArcadeDB returns an empty-ish stub for unknown romnames — require a real title
        return j if (j and j.get("title")) else None

    def close(self):
        self.http.close()


def _romname(game: Game) -> str | None:
    r = game.roms[0] if game.roms else None
    if r and r.filename:
        return r.filename.rsplit(".", 1)[0]
    return None


def scrape_arcadedb(db, game: Game, adb: ArcadeDB, media_store: Path, folder: str | None = None) -> dict:
    from app.services.scraper_util import apply_fields, set_source, save_media
    rom = _romname(game)
    if not rom:
        return {"matched": False, "error": "no romname"}
    j = adb.lookup(rom)
    if not j:
        return {"matched": False}
    if j.get("game_name"):
        set_source(game, "arcadedb", j["game_name"], j.get("title"))
    mfr = (j.get("manufacturer") or "").strip() or None
    filled = apply_fields(game, {
        "description": (j.get("history") or "").strip() or None,
        "developer": mfr, "publisher": mfr,
        "genre": (j.get("genre") or "").strip() or None,
        "players": str(j["players"]) if j.get("players") else None,
        "release_date": str(j["year"]) if j.get("year") else None})
    game.metadata_source = game.metadata_source or "arcadedb"
    imgs = 0
    done: set = set()
    for field, mt in _MEDIA:
        url = j.get(field)
        if not url or mt in done:
            continue
        if save_media(db, game, mt, "arcadedb", url, media_store, adb.http, ext=_EXT.get(mt, ".png")):
            imgs += 1
            done.add(mt)
    return {"matched": True, "images": imgs, "filled": filled}
