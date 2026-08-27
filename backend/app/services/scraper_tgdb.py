"""TheGamesDB source (supplementary metadata + media).

REST API v1 (api.thegamesdb.net) with the UUID apikey. Used as a failover / per-game
source — its monthly quota is limited, so it is NOT for bulk-all-games. Fills missing
unlocked fields (overview/players/rating/release) and downloads media.
"""
from __future__ import annotations
import httpx
from pathlib import Path
from app.core.config import settings
from app.db.models import Game, MediaAsset
from app.services.scraper_lbdb import _norm, _RA_PREFIX, _RA_SUFFIX
import structlog

log = structlog.get_logger()

BASE = "https://api.thegamesdb.net/v1"

# RA console id -> TheGamesDB platform id
TGDB_PLATFORM = {
    1: 18, 11: 35, 15: 20, 4: 4, 6: 41, 5: 5, 7: 7, 3: 6, 28: 4918,
    16: 2, 2: 3, 19: 9, 24: 4957,  # GameCube, N64, Wii, Pokemon Mini
    12: 10, 21: 11, 41: 13,       # PS1, PS2, PSP
    39: 17, 40: 16,               # Saturn, Dreamcast
    57: 4928,                     # Fairchild Channel F
    43: 25,                       # 3DO
}
# TGDB image (type, side) -> RomFleet media_type
_IMG_MAP = {
    ("boxart", "front"): "boxfront", ("boxart", "back"): "boxback",
    ("fanart", None): "background", ("clearlogo", None): "logo",
    ("screenshot", None): "screenshot", ("banner", None): "marquee",
    ("titlescreen", None): "titlescreen",
}


class TgdbScraper:
    def __init__(self, key: str | None = None):
        from app.core.credentials import cred
        self.key = key or cred("tgdb_api_key")
        self.http = httpx.Client(timeout=30, follow_redirects=True)
        self._cache: dict = {}

    @property
    def configured(self) -> bool:
        return bool(self.key)

    def search(self, name: str, platform_id: int) -> dict | None:
        r = self.http.get(f"{BASE}/Games/ByGameName", params={
            "apikey": self.key, "name": name, "filter[platform]": platform_id,
            "fields": "players,overview,rating,platform,genres,publishers,developers"})
        r.raise_for_status()
        games = (r.json().get("data") or {}).get("games") or []
        if not games:
            return None
        want = _norm(name, True)
        for g in games:                       # prefer an exact normalized-title match
            if _norm(g.get("game_title", ""), True) == want:
                return g
        return games[0]

    def by_id(self, game_id: int) -> dict | None:
        r = self.http.get(f"{BASE}/Games/ByGameID", params={
            "apikey": self.key, "id": game_id,
            "fields": "players,overview,rating,platform,genres,publishers,developers"})
        r.raise_for_status()
        games = (r.json().get("data") or {}).get("games") or []
        return games[0] if games else None

    def _lookup(self, kind: str) -> dict:
        """id->name map for developers|publishers|genres (cached per process)."""
        if kind not in self._cache:
            try:
                r = self.http.get(f"{BASE}/{kind.capitalize()}", params={"apikey": self.key})
                data = (r.json().get("data") or {}).get(kind) or {}
                self._cache[kind] = {str(k): v.get("name") for k, v in data.items()}
            except Exception:
                self._cache[kind] = {}
        return self._cache[kind]

    def names(self, kind: str, ids) -> str | None:
        m = self._lookup(kind)
        out = [m.get(str(i)) for i in (ids or []) if m.get(str(i))]
        return "; ".join(out) if out else None

    def images(self, game_id: int):
        r = self.http.get(f"{BASE}/Games/Images", params={
            "apikey": self.key, "games_id": game_id,
            "filter[type]": "boxart,fanart,clearlogo,screenshot,banner,titlescreen"})
        r.raise_for_status()
        data = r.json().get("data") or {}
        base = data.get("base_url", {}).get("original", "https://cdn.thegamesdb.net/images/original/")
        return base, (data.get("images") or {}).get(str(game_id), [])

    def close(self):
        self.http.close()


def scrape_tgdb(db, game: Game, tgdb: TgdbScraper, platform_id: int, media_store: Path) -> dict:
    tgid = (game.source_ids or {}).get("tgdb")
    if tgid:                                      # id override wins over fuzzy match
        hit = tgdb.by_id(int(tgid))
    else:
        name = _RA_SUFFIX.sub("", _RA_PREFIX.sub("", game.title)).strip()
        hit = tgdb.search(name, platform_id)
    if not hit:
        return {"matched": False}
    from app.services.scraper_util import apply_fields, set_source, save_media
    set_source(game, "tgdb", hit["id"], hit.get("game_title"))
    filled = apply_fields(game, {
        "description": hit.get("overview"), "players": str(hit.get("players") or "") or None,
        "rating": hit.get("rating"), "release_date": (hit.get("release_date") or "")[:10] or None,
        "developer": tgdb.names("developers", hit.get("developers")),
        "publisher": tgdb.names("publishers", hit.get("publishers")),
        "genre": tgdb.names("genres", hit.get("genres"))})
    game.metadata_source = game.metadata_source or "tgdb"

    base, images = tgdb.images(hit["id"])
    imgs, seen = 0, set()
    for im in images:
        mt = _IMG_MAP.get((im.get("type"), im.get("side")))
        if not mt or mt in seen:
            continue
        seen.add(mt)
        if save_media(db, game, mt, "tgdb", base + im["filename"], media_store, tgdb.http):
            imgs += 1
    return {"matched": True, "tgdb_id": hit["id"], "images": imgs, "filled": filled}
