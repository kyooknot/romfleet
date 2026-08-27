"""IGDB source (metadata + media). Twitch-OAuth authenticated.

Rich covers/screenshots/artworks + metadata with stable numeric ids. Needs a free
Twitch dev app: IGDB_CLIENT_ID / IGDB_CLIENT_SECRET in .env. Fills missing fields only.
"""
from __future__ import annotations
import re
import httpx
from datetime import datetime, timezone
from pathlib import Path
from app.core.config import settings
from app.db.models import Game, MediaAsset
import structlog

log = structlog.get_logger()

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API = "https://api.igdb.com/v4"
IMG = "https://images.igdb.com/igdb/image/upload"

# RA console id -> IGDB platform id (ids verified against IGDB /platforms).
IGDB_PLATFORM = {1: 29, 11: 64, 15: 35, 4: 33, 6: 22, 5: 24, 7: 18, 3: 19, 28: 87,
                 16: 21, 2: 4, 19: 5, 24: 166,  # GameCube, N64, Wii, Pokemon Mini
                 12: 7, 21: 8, 41: 38,         # PS1, PS2, PSP
                 39: 32, 40: 23,               # Saturn, Dreamcast
                 57: 127,                      # Fairchild Channel F
                 # Atari family
                 25: 59, 51: 60, 13: 61, 17: 62, 77: 410,  # 2600, 7800, Lynx, Jaguar, Jaguar CD
                 # NEC / SNK / Sega 8-bit
                 8: 86, 76: 150, 33: 84,       # TurboGrafx-16, TurboGrafx-CD, SG-1000
                 43: 50, 49: 274, 56: 136,     # 3DO, PC-FX, Neo Geo CD
                 75: 505,                      # Elektor TV Games Computer
                 14: 120, 53: 57,              # Neo Geo Pocket (Color), WonderSwan
                 # Home computers / other consoles
                 37: 25, 29: 27, 46: 70,       # Amstrad CPC, MSX, Vectrex
                 38: 75, 47: 125,              # Apple II, PC-8800 Series
                 44: 68, 45: 67, 23: 133,      # ColecoVision, Intellivision, Odyssey 2
                 81: 51, 18: 20, 78: 159,      # Famicom Disk System, Nintendo DS, DSi
                 71: 438, 63: 415, 69: 408}    # Arduboy, Watara Supervision, Mega Duck
                 # Intentionally unmapped (not in IGDB): WASM-4, Uzebox, Interton VC 4000, Elektor TVGC

# Sub-systems sharing one RA console with a different base (see LBDB_PLATFORM_BY_FOLDER).
IGDB_PLATFORM_BY_FOLDER = {
    "n64dd": 416, "satellaview": 306, "supergrafx": 128,
    "ngp": 119, "wonderswancolor": 123, "msx2": 53,
    "neogeo": 80,   # console 27 (Arcade) -> IGDB Neo Geo AES (80) for cartridge box art
}


def igdb_platform_for(folder: str | None, console_id: int | None) -> int | None:
    """Resolve a folder's IGDB platform id: per-folder override first, then the console-id map."""
    return IGDB_PLATFORM_BY_FOLDER.get(folder or "") or IGDB_PLATFORM.get(console_id)
_CAT = re.compile(r"\s*\((?:Homebrew|Hack|Unlicensed|Prototype|Demo|Test Kit|Subset|Bonus|Multi)\)\s*$", re.I)
_FIELDS = ("fields name,summary,first_release_date,genres.name,cover.image_id,"
           "screenshots.image_id,artworks.image_id,"
           "involved_companies.company.name,involved_companies.developer,involved_companies.publisher")


class IgdbScraper:
    def __init__(self):
        from app.core.credentials import cred
        self.cid = cred("igdb_client_id")
        self.secret = cred("igdb_client_secret")
        self.http = httpx.Client(timeout=30, follow_redirects=True)
        self._token = None

    @property
    def configured(self) -> bool:
        return bool(self.cid and self.secret)

    def _auth(self) -> bool:
        if self._token:
            return True
        if not self.configured:
            return False
        r = self.http.post(TOKEN_URL, params={"client_id": self.cid, "client_secret": self.secret,
                                              "grant_type": "client_credentials"})
        self._token = r.json().get("access_token") if r.status_code == 200 else None
        return bool(self._token)

    def _query(self, body: str) -> list:
        if not self._auth():
            return []
        r = self.http.post(f"{API}/games", content=body,
                           headers={"Client-ID": self.cid, "Authorization": f"Bearer {self._token}"})
        return r.json() if r.status_code == 200 and isinstance(r.json(), list) else []

    def search(self, name: str, platform_id: int | None) -> dict | None:
        # No platform mapping -> skip (an empty filter would emit `platforms=(None)`,
        # which IGDB rejects with a 400; a platform-less search returns wrong-system hits).
        if not platform_id:
            return None
        safe = name.replace('"', "").strip()
        if not safe:
            return None
        rows = self._query(f'search "{safe}"; {_FIELDS}; where platforms=({platform_id}); limit 5;')
        return rows[0] if rows else None

    def by_id(self, igdb_id: int) -> dict | None:
        rows = self._query(f'{_FIELDS}; where id={igdb_id};')
        return rows[0] if rows else None

    def close(self):
        self.http.close()


def _companies(hit: dict, want_dev: bool) -> str | None:
    out = [c["company"]["name"] for c in hit.get("involved_companies", [])
           if c.get("developer" if want_dev else "publisher") and c.get("company", {}).get("name")]
    return "; ".join(dict.fromkeys(out)) or None


def scrape_igdb(db, game: Game, igdb: IgdbScraper, platform_id: int, media_store: Path) -> dict:
    pin = (game.source_ids or {}).get("igdb")
    hit = igdb.by_id(int(pin)) if pin else igdb.search(_CAT.sub("", game.title).strip(), platform_id)
    if not hit:
        return {"matched": False}
    from app.services.scraper_util import apply_fields, set_source, save_media
    set_source(game, "igdb", hit["id"], hit.get("name"))
    date = None
    if hit.get("first_release_date"):
        date = datetime.fromtimestamp(hit["first_release_date"], timezone.utc).strftime("%Y-%m-%d")
    filled = apply_fields(game, {
        "description": hit.get("summary"),
        "genre": "; ".join(g["name"] for g in hit.get("genres", []) if g.get("name")) or None,
        "developer": _companies(hit, True), "publisher": _companies(hit, False),
        "release_date": date})
    game.metadata_source = game.metadata_source or "igdb"

    jobs = []
    if hit.get("cover"):
        jobs.append(("boxfront", "t_cover_big", hit["cover"]["image_id"]))
    for sc in hit.get("screenshots", [])[:2]:
        jobs.append(("screenshot", "t_screenshot_huge", sc["image_id"]))
    for art in hit.get("artworks", [])[:1]:
        jobs.append(("background", "t_1080p", art["image_id"]))
    imgs = 0
    for mt, size, image_id in jobs:
        if save_media(db, game, mt, "igdb", f"{IMG}/{size}/{image_id}.jpg", media_store, igdb.http, ext=".jpg"):
            imgs += 1
    return {"matched": True, "igdb_id": hit["id"], "images": imgs, "filled": filled}
