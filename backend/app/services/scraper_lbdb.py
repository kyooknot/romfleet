"""LaunchBox GamesDB scraper.

Matches a RomFleet Game to a local LaunchBox.Metadata.db entry (replicating
LaunchBox's CompareName normalization — validated to ~96% on real data), then
copies metadata + downloads US-region media from images.launchbox-app.com.

No credentials required: the metadata DB is a local SQLite file and the image
CDN is public. Respects Game.locked_fields (user edits win over scrape).
"""
from __future__ import annotations
import re
import sqlite3
import unicodedata
import httpx
from pathlib import Path
from app.core.config import settings
from app.db.models import Game, MediaAsset
import structlog

log = structlog.get_logger()

CDN = "https://images.launchbox-app.com/"

# RA decorates titles with ~Homebrew~ / ~Unlicensed~ / ~Hack~ / ~Prototype~ etc.,
# which RomFleet stores as a trailing "(Homebrew)"/"(Hack)"/… suffix. Strip either form.
_RA_PREFIX = re.compile(r"^~[^~]+~\s*")
_RA_SUFFIX = re.compile(r"\s*\((?:Homebrew|Hack|Unlicensed|Prototype|Demo|Test Kit|Subset|Bonus|Multi)\)\s*$", re.I)

_ROMANS = ['I','II','III','IV','V','VI','VII','VIII','IX','X','XI','XII','XIII','XIV','XV',
           'XVI','XVII','XVIII','XIX','XX','XXI','XXII','XXIII','XXIV','XXV','XXVI','XXVII','XXVIII','XXIX','XXX']
_ROMAN = {r: str(i + 1) for i, r in enumerate(_ROMANS)}
_STOP = {"THE", "A", "AN", "AND"}

# RomFleet media_type  <-  LaunchBox ImageType
_IMG_TYPE_MAP = {
    "Box - Front": "boxfront", "Box - Back": "boxback", "Clear Logo": "logo",
    "Screenshot - Game Title": "titlescreen", "Screenshot - Gameplay": "screenshot",
    "Fanart - Background": "background", "Disc": "disc", "Cart - Front": "cart",
}
# US-first region rank (lower = preferred)
_REGION_RANK = {"United States": 0, "North America": 1, "World": 2, None: 3, "": 3,
                "United Kingdom": 4, "Europe": 5, "Australia": 6, "Canada": 7}

# RA console id -> LaunchBox Platform name (extend as systems fan out)
# RA console id -> LaunchBox platform name (exact names verified against the LBDB).
LBDB_PLATFORM = {
    # Sega
    1: "Sega Genesis", 11: "Sega Master System", 15: "Sega Game Gear",
    10: "Sega 32X", 9: "Sega CD", 39: "Sega Saturn", 40: "Sega Dreamcast", 33: "Sega SG-1000",
    # Nintendo
    7: "Nintendo Entertainment System", 3: "Super Nintendo Entertainment System",
    2: "Nintendo 64", 16: "Nintendo GameCube", 19: "Nintendo Wii",
    4: "Nintendo Game Boy", 6: "Nintendo Game Boy Color", 5: "Nintendo Game Boy Advance",
    18: "Nintendo DS", 28: "Nintendo Virtual Boy", 24: "Nintendo Pokemon Mini",
    # Sony
    12: "Sony Playstation", 21: "Sony Playstation 2", 41: "Sony PSP",
    # Atari
    25: "Atari 2600", 51: "Atari 7800", 13: "Atari Lynx",
    17: "Atari Jaguar", 77: "Atari Jaguar CD",
    57: "Fairchild Channel F",
    # NEC / SNK / other
    8: "NEC TurboGrafx-16", 76: "NEC TurboGrafx-CD", 49: "NEC PC-FX",
    43: "3DO Interactive Multiplayer", 56: "SNK Neo Geo CD",
    44: "ColecoVision", 45: "Mattel Intellivision",
    38: "Apple II", 47: "NEC PC-8801",
    37: "Amstrad CPC", 73: "Emerson Arcadia 2001", 63: "Watara Supervision",
    69: "Mega Duck", 74: "Interton VC 4000", 23: "Magnavox Odyssey 2",
    81: "Nintendo Famicom Disk System", 29: "Microsoft MSX", 46: "GCE Vectrex",
    75: "Elektor TV Games Computer",
    78: "Nintendo DS",  # DSi (RA 78): LBDB has no DSi platform — DSi games live under Nintendo DS
    71: "Arduboy", 72: "WASM-4", 80: "Uzebox",
    53: "WonderSwan", 14: "SNK Neo Geo Pocket Color",
}

# Sub-systems that share ONE RA console with a different base platform: the console-keyed map
# can only name one, so the other folder needs its own platform (else it scrapes against the
# base and matches nothing — e.g. a WonderSwan Color game vs the "WonderSwan" catalog). Folder
# override wins; the base folder still resolves via LBDB_PLATFORM by console id.
LBDB_PLATFORM_BY_FOLDER = {
    "n64dd": "Nintendo 64DD",
    "satellaview": "Nintendo Satellaview",
    "supergrafx": "PC Engine SuperGrafx",
    "ngp": "SNK Neo Geo Pocket",              # console 14 base map is the Color variant (ngpc)
    "wonderswancolor": "WonderSwan Color",
    "msx2": "Microsoft MSX2",
    "neogeo": "SNK Neo Geo AES",              # console 27 (Arcade) -> AES for cartridge box art
    "neogeomvs": "SNK Neo Geo MVS",           # console 27 (Arcade) -> MVS for arcade marquee/flyer art
}


def lbdb_platform_for(folder: str | None, console_id: int | None) -> str | None:
    """Resolve a folder's LBDB platform: per-folder override first, then the console-id map."""
    return LBDB_PLATFORM_BY_FOLDER.get(folder or "") or LBDB_PLATFORM.get(console_id)


def _norm(s: str, drop_articles: bool = True) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii").upper()
    s = s.replace("'", "").replace("`", "")            # straight apostrophes: no space
    s = "".join(ch if ch.isalnum() else " " for ch in s)
    out = []
    for t in s.split():
        if drop_articles and t in _STOP:
            continue
        out.append(_ROMAN.get(t, t))
    return " ".join(out)


def _region_rank(r: str | None) -> int:
    return _REGION_RANK.get(r, 8)


class LbdbScraper:
    def __init__(self, db_path: str | None = None):
        self.path = db_path or settings.lbdb_path
        self._con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        self._con.row_factory = sqlite3.Row
        self._idx: dict[str, int] = {}      # normalized name -> DatabaseID
        self._platform: str | None = None

    def list_platforms(self, like: str) -> list[str]:
        cur = self._con.execute("SELECT DISTINCT Platform FROM Games WHERE Platform LIKE ?", (f"%{like}%",))
        return [r[0] for r in cur.fetchall()]

    @staticmethod
    def _keys(name: str):
        # spaced (article kept/dropped) + space-collapsed variants. Collapsed recovers
        # "16-Bit" (-> "16 BIT") vs "16BIT" and similar digit/letter spacing splits.
        a, b = _norm(name, False), _norm(name, True)
        return {a, b, a.replace(" ", ""), b.replace(" ", "")}

    def load_platform(self, platform: str):
        """Build the match index (spaced + collapsed, article-kept + dropped) for one platform."""
        self._platform = platform
        self._idx = {}
        for row in self._con.execute(
                "SELECT CompareName, DatabaseID FROM Games WHERE Platform=?", (platform,)):
            for k in self._keys(row["CompareName"]):
                self._idx.setdefault(k, row["DatabaseID"])
        for row in self._con.execute(
                "SELECT gat.AltNameCompareValue AS cn, gat.DatabaseID AS did "
                "FROM GameAlternateTitles gat JOIN Games g ON g.DatabaseID=gat.DatabaseID "
                "WHERE g.Platform=?", (platform,)):
            for k in self._keys(row["cn"]):
                self._idx.setdefault(k, row["did"])
        return len(self._idx)

    def match(self, title: str) -> int | None:
        base = _RA_SUFFIX.sub("", _RA_PREFIX.sub("", title))
        b, a = _norm(base, True), _norm(base, False)
        for k in (b, a, b.replace(" ", ""), a.replace(" ", "")):   # spaced first, then collapsed
            if k in self._idx:
                return self._idx[k]
        return None

    def search_by_name(self, platform: str, q: str, limit: int = 15) -> list[dict]:
        rows = self._con.execute(
            "SELECT DatabaseID, Name FROM Games WHERE Platform=? AND Name LIKE ? ORDER BY Name LIMIT ?",
            (platform, f"%{q}%", limit)).fetchall()
        return [{"id": r["DatabaseID"], "name": r["Name"]} for r in rows]

    def game_row(self, dbid: int) -> sqlite3.Row | None:
        cur = self._con.execute("SELECT * FROM Games WHERE DatabaseID=?", (dbid,))
        return cur.fetchone()

    def best_images(self, dbid: int) -> dict[str, tuple[str, str]]:
        """{media_type: (FileName, Region)} — best US-region image per wanted type."""
        by_type: dict[str, tuple[int, str, str]] = {}
        for row in self._con.execute(
                "SELECT Type, Region, FileName FROM GameImages WHERE DatabaseId=?", (dbid,)):
            mt = _IMG_TYPE_MAP.get(row["Type"])
            if not mt:
                continue
            rank = _region_rank(row["Region"])
            if mt not in by_type or rank < by_type[mt][0]:
                by_type[mt] = (rank, row["FileName"], row["Region"])
        return {mt: (fn, reg) for mt, (rk, fn, reg) in by_type.items()}

    def close(self):
        self._con.close()


_META_MAP = {  # Game attr <- LBDB Games column
    "developer": "Developer", "publisher": "Publisher", "release_date": "ReleaseDate",
    "description": "Overview", "genre": "Genres", "players": "MaxPlayers", "rating": "ESRB",
    "category": "ReleaseType",   # LBDB "Game Type": Released/Homebrew/ROM Hack/Unlicensed/…
}


def scrape_game(db, game: Game, lbdb: LbdbScraper, media_store: Path,
                http: httpx.Client, only_missing: bool = False) -> dict:
    """Scrape one game from LBDB. only_missing=True fills blanks only (per-game 'fill'
    button); False overwrites unlocked fields (bulk scrape). Returns a status dict."""
    _pin = (game.source_ids or {}).get("lbdb")   # id override wins over fuzzy match
    dbid = int(_pin) if _pin else lbdb.match(game.title)
    if not dbid:
        return {"game_id": game.id, "matched": False}
    row = lbdb.game_row(dbid)
    if row is None:
        return {"game_id": game.id, "matched": False, "error": f"LBDB id {dbid} not found"}
    from app.services.scraper_util import apply_fields, set_source, save_media
    set_source(game, "lbdb", dbid, row["Name"] if "Name" in row.keys() else None)
    vals = {}
    for attr, col in _META_MAP.items():
        v = row[col] if col in row.keys() else None
        vals[attr] = (str(v)[:10] if attr == "release_date" and v else v)
    filled = apply_fields(game, vals)   # sets unlocked fields + locks them
    game.metadata_source = game.metadata_source or "lbdb"
    # community/crowd score (0-5) — reference data, refreshes on scrape (not a locked
    # field); seeds the display/Batocera rating when no personal star grade is set.
    cr = row["CommunityRating"] if "CommunityRating" in row.keys() else None
    if cr:
        try:
            game.community_rating = round(float(cr), 2)
        except (TypeError, ValueError):
            pass

    imgs = 0
    for mt, (filename, region) in lbdb.best_images(dbid).items():
        if save_media(db, game, mt, "lbdb", CDN + filename, media_store, http):
            imgs += 1
    return {"game_id": game.id, "matched": True, "dbid": dbid, "images": imgs, "filled": filled}


def scrape_system(folder: str, limit: int | None = None, platform: str | None = None) -> dict:
    """Scrape every library game in a system from LBDB (metadata + US media)."""
    from pathlib import Path as _P
    from app.core.systems import get_system_by_folder
    from app.db.session import SessionLocal
    from app.db.models import Game, RomFile

    sysc = get_system_by_folder(folder)
    if not sysc:
        raise ValueError(f"unknown system folder: {folder}")
    platform = platform or lbdb_platform_for(folder, sysc.ra_console_id)
    if not platform:
        raise ValueError(f"no LBDB platform mapping for console {sysc.ra_console_id}; pass --platform")

    lbdb = LbdbScraper()
    idx_n = lbdb.load_platform(platform)
    media_store = _P(settings.canonical_path) / "media"
    db = SessionLocal()
    http = httpx.Client(headers={"User-Agent": "RomFleet/0.1"}, follow_redirects=True)
    matched = missed = images = 0
    try:
        game_ids = [r[0] for r in db.query(RomFile.game_id).filter(
            RomFile.system_folder == folder, RomFile.game_id.isnot(None)).distinct().all()]
        games = db.query(Game).filter(Game.id.in_(game_ids)).all()
        if limit:
            games = games[:limit]
        for g in games:
            r = scrape_game(db, g, lbdb, media_store, http)
            if r["matched"]:
                matched += 1
                images += r.get("images", 0)
            else:
                missed += 1
        db.commit()
    finally:
        db.close(); http.close(); lbdb.close()
    return {"system": folder, "platform": platform, "lbdb_index": idx_n,
            "games": len(games), "matched": matched, "missed": missed, "images": images}
