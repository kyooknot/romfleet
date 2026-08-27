"""EmuMovies video-snap source (reverse-engineered API, ported from a private predecessor repo).

login -> search.aspx (exact match) -> Video_MP4[_HD] URL -> download. No metadata,
video only. Needs EMU_USER / EMU_PASS in .env. The product key is Emby's public one.
"""
from __future__ import annotations
import re
import httpx
import xml.etree.ElementTree as ET
from pathlib import Path
from app.core.config import settings
from app.db.models import Game, MediaAsset
import structlog

log = structlog.get_logger()

BASE = "https://api.gamesdbase.com"
PRODUCT = "4D8621EE919A13EB6E89B7EDCA6424FC33D6"   # Emby's public product key

# RA console id -> EmuMovies system "Lookup" value (underscored). Extend as systems fan out.
# RA console id -> EmuMovies system Lookup (exact codes verified via getsystems.aspx).
EMU_SYSTEM = {
    # Sega
    1: "Sega_Genesis", 11: "Sega_Master_System", 15: "Sega_Game_Gear",
    10: "Sega_32X", 9: "Sega_CD", 39: "Sega_Saturn", 40: "Sega_Dreamcast",
    33: "Sega_SG-1000",
    # Nintendo
    7: "Nintendo_NES", 3: "Nintendo SNES",   # SNES: EmuMovies name is "Nintendo SNES" (not the long form)
    2: "Nintendo_N64", 16: "Nintendo_GameCube", 19: "Nintendo_Wii",
    4: "Nintendo_Game_Boy", 6: "Nintendo_Game_Boy_Color", 5: "Nintendo_Game_Boy_Advance",
    18: "Nintendo_DS", 28: "Nintendo_Virtual_Boy", 24: "Nintendo_Pokemon_Mini",
    # Sony
    12: "Sony_Playstation", 21: "Sony_Playstation_2", 41: "Sony_PSP",
    # Atari / NEC / other
    25: "Atari_2600", 51: "Atari_7800", 13: "Atari_Lynx",
    17: "Atari_Jaguar", 77: "Atari_Jaguar_CD",
    57: "Fairchild_Channel_F",
    8: "NEC_TurboGrafx_16", 76: "NEC_TurboGrafx_CD", 49: "NEC PC-FX",
    44: "Coleco_Vision", 45: "Mattel_Intellivision", 53: "Bandai_WonderSwan",
    29: "Microsoft MSX",
    43: "Panasonic 3DO", 56: "SNK Neo Geo CD", 142: "SNK Neo Geo MVS", 27: "MAME",
}
# Shared-console sub-systems: folder override wins (naomi/atomiswave share console 27, etc.)
EMU_SYSTEM_BY_FOLDER = {
    "naomi": "Sega Naomi", "naomi2": "Sega Naomi 2", "atomiswave": "Sammy Atomiswave",
    "msx2": "Microsoft MSX2", "supergrafx": "NEC SuperGrafx",
    "wonderswancolor": "Bandai WonderSwan Color",
}


def emu_system_for(folder: str | None, console_id: int | None) -> str | None:
    return EMU_SYSTEM_BY_FOLDER.get(folder or "") or EMU_SYSTEM.get(console_id)
# RA category suffix the UI stores; strip before searching
_CAT = re.compile(r"\s*\((?:Homebrew|Hack|Unlicensed|Prototype|Demo|Test Kit|Subset|Bonus|Multi)\)\s*$", re.I)


class EmuMovies:
    def __init__(self, user: str | None = None, pw: str | None = None):
        from app.core.credentials import cred
        self.user = user or cred("emu_user")
        self.pw = pw or cred("emu_pass")
        self.sid: str | None = None
        self.http = httpx.Client(timeout=30, follow_redirects=True)

    @property
    def configured(self) -> bool:
        return bool(self.user and self.pw)

    def login(self) -> bool:
        if not self.configured:
            return False
        r = self.http.get(f"{BASE}/login.aspx",
                          params={"user": self.user, "api": self.pw, "product": PRODUCT})
        try:
            res = ET.fromstring(r.text).find(".//Result")
            self.sid = res.get("Session") if res is not None else None
        except Exception:
            self.sid = None
        return bool(self.sid)

    def search_video(self, name: str, system_lookup: str) -> str | None:
        for media in ("Video_MP4_HD", "Video_MP4"):
            r = self.http.get(f"{BASE}/search.aspx", params={
                "search": name, "system": system_lookup, "media": media, "sessionid": self.sid})
            try:
                res = ET.fromstring(r.text).find(".//Result")
            except Exception:
                res = None
            if res is not None and (res.get("Found") == "True") and res.get("URL"):
                return res.get("URL")
        return None

    def close(self):
        self.http.close()


def scrape_video(db, game: Game, emu: EmuMovies, media_store: Path, folder: str | None = None) -> dict:
    """Pull a video snap. Skips if a LOCKED video exists; a downloaded snap auto-locks."""
    from app.services.scraper_util import set_source, save_media
    if any(m.media_type == "video" and m.is_locked for m in game.media):
        return {"video": "exists"}
    lookup = emu_system_for(folder, game.console_id)
    if not lookup:
        return {"video": None, "error": f"no EmuMovies system for console {game.console_id}"}
    name = (game.source_ids or {}).get("emumovies") or _CAT.sub("", game.title).strip()
    url = emu.search_video(name, lookup)
    if not url:
        return {"video": None}
    if save_media(db, game, "video", "emumovies", url, media_store, emu.http, ext=".mp4"):
        set_source(game, "emumovies", name, name)
        return {"video": "downloaded"}
    return {"video": "exists"}
