"""ScreenScraper source (metadata + media).

Robust lookup: source_ids['ss'] (gameid) > the ROM md5 (hash match — very accurate) >
name. Needs a registered dev softname: SS_DEV_ID / SS_DEV_PASSWORD (+ SS_USER/SS_PASS)
in .env. Fills missing fields only.
"""
from __future__ import annotations
import httpx
from pathlib import Path
from app.core.config import settings
from app.db.models import Game, MediaAsset
import structlog

log = structlog.get_logger()

API = "https://api.screenscraper.fr/api2/jeuInfos.php"
# RA console id -> ScreenScraper systemeid (verified against systemesListe.php). SS has the
# best coverage of obscure/homebrew systems, so it's the fallback where LBDB/IGDB come up empty.
SS_SYSTEM = {
    # Sega
    1: 1, 11: 2, 15: 21, 10: 19, 9: 20, 39: 22, 40: 23, 33: 109,
    # Nintendo
    7: 3, 3: 4, 2: 14, 16: 13, 19: 16, 4: 9, 6: 10, 5: 12, 18: 15, 28: 11, 24: 211, 81: 106,
    78: 15,   # DSi (RA 78): SS has no DSi system — scrape DSi games under Nintendo DS (15)
    # Sony
    12: 57, 21: 58, 41: 61,
    # Atari
    25: 26, 51: 41, 13: 28, 17: 27, 77: 171,
    # NEC
    8: 31, 76: 114, 49: 72, 47: 221,   # 49 PC-FX -> SS 72; 47 PC-8801 -> SS 221
    # Everything else (SS is the strong source for these)
    57: 80, 44: 48, 45: 115, 37: 65, 73: 94, 63: 207, 69: 90, 74: 281, 23: 104,
    29: 113, 46: 102, 14: 82, 53: 45, 71: 263, 72: 262, 80: 216, 38: 86,
    43: 29, 56: 70,   # 43 3DO -> SS 29, 56 Neo Geo CD -> SS 70
}   # unmapped (not in SS): 75 Elektor TV Games Computer

# Shared-console sub-systems (see LBDB_PLATFORM_BY_FOLDER) — folder override wins.
SS_SYSTEM_BY_FOLDER = {
    "n64dd": 122, "satellaview": 107, "sufami": 108, "supergrafx": 105,
    "ngp": 25, "wonderswancolor": 46, "msx2": 116,   # console 14->82 (NGPC), 53->45 (WS base)
    "neogeo": 142,   # console 27 (Arcade) -> SS Neo-Geo AES (142) for cartridge box art, not MVS/arcade
    # The rest of the console-27 family. RA files ALL arcade under console 27, so without a
    # folder override ss_system_for() returned None and scrape_ss bailed on the first line with
    # "no SS system for console 27" — 0 matches in about a second per system, which reads like
    # "ScreenScraper doesn't have these" rather than "we never asked" (2026-08-15).
    # IDs read from SS's own systemesListe.php, not guessed; all are children of parent 75.
    "arcade": 75,        # generic Arcade
    "naomi": 56,
    "naomi2": 230,
    "atomiswave": 53,
    "neogeomvs": 68,     # MVS cabinet art, distinct from the AES cartridge above
}


def ss_system_for(folder: str | None, console_id: int | None) -> int | None:
    return SS_SYSTEM_BY_FOLDER.get(folder or "") or SS_SYSTEM.get(console_id)
# ScreenScraper media type -> RomFleet media_type
_MEDIA = {"box-2D": "boxfront", "box-2D-back": "boxback", "ss": "screenshot",
          "sstitle": "titlescreen", "wheel": "logo", "video": "video", "fanart": "background",
          "support-2D": "cart"}
_REGION_RANK = {"us": 0, "wor": 1, "ss": 2, "eu": 3, "jp": 4}


def _first(items, langs=("en",)):
    """SS returns lists of {langue|region, text}; pick preferred lang/region else first."""
    if not items:
        return None
    for key in langs:
        for it in items:
            if it.get("langue") == key or it.get("region") == key:
                return it.get("text")
    return items[0].get("text")


class SsScraper:
    def __init__(self):
        self.http = httpx.Client(timeout=30, follow_redirects=True)

    @property
    def configured(self) -> bool:
        from app.core.credentials import cred
        return bool(cred("ss_dev_id") and cred("ss_dev_password") and cred("ss_user"))

    def _base(self) -> dict:
        from app.core.credentials import cred
        return {"devid": cred("ss_dev_id"), "devpassword": cred("ss_dev_password"),
                "softname": cred("ss_softname") or "RomFleet", "ssid": cred("ss_user"),
                "sspassword": cred("ss_pass"), "output": "json"}

    def lookup(self, systeme: int, gameid=None, md5=None, name=None) -> dict | None:
        p = self._base()
        p["systemeid"] = systeme
        if gameid:
            p["gameid"] = gameid
        elif md5:
            p["md5"] = md5
        elif name:
            p["romnom"] = name
        r = self.http.get(API, params=p)
        if r.status_code != 200:
            return None
        try:
            return (r.json().get("response") or {}).get("jeu")
        except Exception:
            return None

    def close(self):
        self.http.close()


def scrape_ss(db, game: Game, ss: SsScraper, media_store: Path, folder: str | None = None) -> dict:
    systeme = ss_system_for(folder, game.console_id)
    if not systeme:
        return {"matched": False, "error": f"no SS system for console {game.console_id}"}
    pin = (game.source_ids or {}).get("ss")
    rom = game.roms[0] if game.roms else None
    if pin:
        jeu = ss.lookup(systeme, gameid=pin)
    else:
        # md5 is most accurate, but our computed_hash is the RA hash — for systems where that
        # differs from SS's file md5 (FDS headerless, NDS rahash, disc systems) it always misses,
        # so fall back to a title lookup.
        jeu = ss.lookup(systeme, md5=rom.computed_hash) if (rom and rom.computed_hash) else None
        if not jeu and game.title:
            jeu = ss.lookup(systeme, name=game.title)
    if not jeu:
        return {"matched": False}
    from app.services.scraper_util import apply_fields, set_source, save_media
    if jeu.get("id"):
        set_source(game, "ss", jeu["id"], _first(jeu.get("noms"), ("us", "wor")))
    filled = apply_fields(game, {
        "description": _first(jeu.get("synopsis"), ("en",)),
        "developer": (jeu.get("developpeur") or {}).get("text"),
        "publisher": (jeu.get("editeur") or {}).get("text"),
        "genre": "; ".join(filter(None, (_first(g.get("noms")) for g in jeu.get("genres", [])))) or None,
        "release_date": _first(jeu.get("dates"), ("us", "wor")),
        # players/rating were never mapped, so ScreenScraper — the one source that HAS them for
        # obscure platforms — could not fill the exact two fields it was being run to fill.
        # A megacd pass matched 81 of 82 games and filled 0 (2026-08-15).
        # `note` is SS's own 0-20 review score, NOT an age rating; `rating` here means ESRB,
        # the same thing scraper_lbdb maps from its "ESRB" column.
        "players": (jeu.get("joueurs") or {}).get("text"),
        "rating": next((c.get("text") for c in (jeu.get("classifications") or [])
                        if (c.get("type") or "").upper() == "ESRB"), None)})
    game.metadata_source = game.metadata_source or "ss"

    best: dict = {}
    for md in jeu.get("medias", []):
        mt = _MEDIA.get(md.get("type"))
        if not mt or not md.get("url"):
            continue
        rank = _REGION_RANK.get(md.get("region"), 9)
        if mt not in best or rank < best[mt][0]:
            best[mt] = (rank, md["url"], md.get("format", "png"))
    imgs = 0
    for mt, (rank, url, fmt) in best.items():
        if save_media(db, game, mt, "ss", url, media_store, ss.http, ext="." + fmt):
            imgs += 1
    return {"matched": True, "ss_id": jeu.get("id"), "images": imgs, "filled": filled}
