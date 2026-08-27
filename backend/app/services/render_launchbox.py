"""LaunchBox / BigBox renderer.

Turns the curated canonical library (Game + matched RomFile + MediaAsset rows) into a
LaunchBox export *bundle*: per-platform Data/Platforms/<Platform>.xml files plus a
media tree (Images/, Videos/, Manuals/), ready to be copied onto a LaunchBox Windows
install.

This module never touches a live LaunchBox instance — it only writes files under
`output_dir`. LaunchBox 13.x is .NET 9 and rewrites its own Platform XML on exit, so
the operator must close LaunchBox/BigBox on that instance before copying a fresh
bundle in (see docs/CANONICAL-SYNC.md §4). Only `RomFile.match_status == "matched"`
roms are ever rendered — the curated set, never a raw folder dump.

Schema notes (verified against a real LaunchBox-exported <Game> block + community
docs/forums — see the render report for sources):
  - Root element is `<LaunchBox>` containing repeated `<Game>` and, for multi-disc
    games, `<AdditionalApplication>` elements.
  - `<Game><ID>` is a GUID (LaunchBox's internal primary key). We derive it
    *deterministically* (uuid5 of the RomFleet game id) so re-renders keep the same
    identity — a re-copy of the bundle doesn't look like brand-new games to LaunchBox
    (preserves play counts/favorites the user sets locally).
  - There is NO `EmulatorId`/`Emulator` field on `<Game>` in the real schema — the
    default emulator for a platform is configured once in LaunchBox's top-level
    `Data\\Platforms.xml` / `Data\\Emulators.xml` (platform<->emulator association),
    not per game. That file is intentionally out of scope here (see report).
  - Multi-disc games: the lowest-numbered disc becomes the `<Game>` (ApplicationPath);
    the remaining discs become `<AdditionalApplication>` entries linked by `<GameID>`.
    (M3U is cleaner for RetroArch cores that support disc-swap — see report — but
    AdditionalApplication is what's implemented here per spec, and is the only option
    for non-RetroArch multi-disc systems like GameCube/Dolphin.)
  - Media is matched to a game purely by `<Title>` text (LaunchBox convention — there
    is no image-path field in the Game XML): `Images/<Platform>/<LB media
    folder>/<sanitized Title>-01.<ext>`. Videos/Manuals are top-level siblings of
    Images (`Videos/<Platform>/...`, `Manuals/<Platform>/...`), NOT nested under
    Images/ — see report for this correction vs. the literal task template.
"""
from __future__ import annotations

import difflib
import json
import re
import shutil
import uuid
import xml.sax.saxutils as sax
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload
import structlog

from app.core.systems import get_system_by_folder, load_systems, SystemConfig
from app.db.session import SessionLocal
from app.db.models import Game, RomFile, MediaAsset

log = structlog.get_logger()

# ------------------------------------------------------------------------------------
# folder -> LaunchBox platform name
# ------------------------------------------------------------------------------------
# Folders whose RA console is shared with a sibling folder need an explicit override
# here (one console_id, two distinct LaunchBox platforms). Everything else falls back
# to the RA-console map already vetted for the LBDB scraper. Verified against
# gamesdb.launchbox-app.com; entries marked UNVERIFIED should be confirmed against a
# real local LaunchBox.Metadata.db (see `lbdb-platforms --like` CLI command) before
# relying on them for scrape/media matching.
PLATFORM_OVERRIDES: dict[str, str] = {
    "ngp": "SNK Neo Geo Pocket",                        # mono; distinct from ngpc's "...Color"
    "ngpc": "SNK Neo Geo Pocket Color",
    "satellaview": "Nintendo Satellaview",              # shares SNES console (3)
    "n64dd": "Nintendo 64DD",                           # shares N64 console (2)
    "ndsi": "Nintendo DSi",                             # DSi (console 78): own LB platform — LBDB has none, so it fell back to "Nintendo DS" and clobbered nds' 601-game XML on push

    "sufami": "Sufami Turbo",                           # shares SNES console (3). Previously pointed AT
    # "Super Nintendo Entertainment System" for want of an LBDB platform — which guaranteed a
    # collision: both folders rendered the same XML and the later push overwrote the earlier, so
    # a 66-system deploy silently reduced SNES from 1121 games to sufami's 1. Same failure ndsi
    # caused for nds. A custom LaunchBox platform with no LBDB entry is the lesser evil; it just
    # needs a platform icon placed by hand (see the Platform Icons media pack) and a LaunchBox
    # restart to show in the sidebar.
    "wonderswan": "WonderSwan",
    "wonderswancolor": "WonderSwan Color",              # shares WonderSwan console (53)
    "supergrafx": "PC Engine SuperGrafx",               # shares PC Engine console (8)
    "neogeo": "SNK Neo Geo AES",                        # shares Arcade console (27)
    "neogeomvs": "SNK Neo Geo MVS",                     # shares Arcade console (27); MVS-exclusive titles
    "naomi": "Sega Naomi",                              # shares Arcade console (27)
    "naomi2": "Sega Naomi 2",                           # shares Arcade console (27)
    "atomiswave": "Sammy Atomiswave",                   # shares Arcade console (27)
    "arcade": "Arcade",                                 # base FBNeo arcade (console 27) -> FBNeo core
    "appleii": "Apple II",                              # console 38 -> RAppleWin standalone (local C:\Apple2Disks staging)
    "pc8800": "NEC PC-8801",                            # console 47 -> QUASI88 core
    "msx2": "Microsoft MSX2",                           # split from msx (both console 29); matches LBDB_PLATFORM folder-override
}


def platform_collisions(folders: list[str]) -> dict[str, list[str]]:
    """Folders that resolve to the SAME LaunchBox platform, keyed by platform.

    Rendering is per-folder and each push writes <Platform>.xml wholesale, so two folders
    sharing a platform means the later push silently discards the earlier one's games. This has
    now happened twice (ndsi over nds, sufami over snes) and both times the deploy reported
    complete success -- the loss is only visible by counting games on the target. Callers should
    surface this rather than proceed quietly.
    """
    seen: dict[str, list[str]] = {}
    for f in folders:
        p = platform_for(f)
        if p:
            seen.setdefault(p, []).append(f)
    return {p: fs for p, fs in seen.items() if len(fs) > 1}


def platform_for(folder: str) -> str | None:
    """Resolve a Batocera system folder to its LaunchBox Platform name."""
    if folder in PLATFORM_OVERRIDES:
        return PLATFORM_OVERRIDES[folder]
    sysc = get_system_by_folder(folder)
    if not sysc:
        return None
    from app.services.scraper_lbdb import LBDB_PLATFORM
    return LBDB_PLATFORM.get(sysc.ra_console_id)


# ------------------------------------------------------------------------------------
# RomFleet MediaAsset.media_type -> LaunchBox media folder
# ------------------------------------------------------------------------------------
# Inverse of scraper_lbdb._IMG_TYPE_MAP (image types); video/manual added. `root` picks
# which top-level tree the folder lives under — Videos/ and Manuals/ are siblings of
# Images/, NOT nested inside it (a correction vs. the naive "everything under Images/"
# template — see the render report).
@dataclass(frozen=True)
class MediaTarget:
    root: str     # "Images" | "Videos" | "Manuals"
    folder: str   # LaunchBox media-type folder name under <root>/<Platform>/


MEDIA_TARGET: dict[str, MediaTarget] = {
    "boxfront":    MediaTarget("Images", "Box - Front"),
    "boxback":     MediaTarget("Images", "Box - Back"),
    "titlescreen": MediaTarget("Images", "Screenshot - Game Title"),
    "screenshot":  MediaTarget("Images", "Screenshot - Gameplay"),
    "logo":        MediaTarget("Images", "Clear Logo"),
    "video":       MediaTarget("Videos", ""),
    "manual":      MediaTarget("Manuals", ""),
}

# Deterministic-GUID namespace — any fixed UUID works; keeps <ID>/<GameID> stable
# across re-renders so a fresh bundle copy isn't a "new game" to LaunchBox.
_GUID_NS = uuid.uuid5(uuid.NAMESPACE_URL, "https://romfleet.local/launchbox-render")

_DISC_RE = re.compile(r"\(Disc\s*(\d+)\)", re.IGNORECASE)
# Windows-invalid filename chars, plus apostrophe (LaunchBox's own convention strips
# it too, e.g. "Nemesis '94" -> "Nemesis _94") -> underscore.
_INVALID_FS = re.compile(r"[\\/:*?\"<>|']")


def _guid(*parts: str) -> str:
    return str(uuid.uuid5(_GUID_NS, "|".join(parts)))


def _esc(s: str | None) -> str:
    return sax.escape(str(s)) if s else ""


def sanitize_media_title(title: str) -> str:
    """LaunchBox media-matching convention: Windows-invalid filename chars (plus
    apostrophe) become underscores. Collapses whitespace LaunchBox would also collapse."""
    return _INVALID_FS.sub("_", title or "").strip()


def _app_path(rom_base: str, system_folder: str, filename: str) -> str:
    """Windows-style ApplicationPath join (backslashes), per-instance rom_base."""
    base = rom_base.rstrip("\\/")
    return f"{base}\\{system_folder}\\{filename}"


def _rom_subpath(rom) -> str:
    """A rom file's path RELATIVE to its system-folder root, with Windows separators —
    so per-game subfolders survive into the ApplicationPath. Nearly every system stores
    games flat (one .zip/.chd per game) where this == filename; but a raw multi-file disc
    system (jaguarcd: <disc>/<disc>.cue + Track-N .bin sidecars) keeps each disc in its own
    subfolder, and the .cue must be launched from inside it. Falls back to the bare filename
    if the folder marker isn't found in the stored path (identity for flat systems)."""
    marker = f"/{rom.system_folder}/"
    idx = rom.path.find(marker)
    rel = rom.path[idx + len(marker):] if idx != -1 else rom.filename
    return rel.replace("/", "\\")


# ------------------------------------------------------------------------------------
# Apple II special-case (folder 'appleii' ONLY)
# ------------------------------------------------------------------------------------
# RAppleWin (the RA AppleWin fork) can't read disk images off the J: SMB share, and
# LaunchBox's AutoExtract mishandles the .m3u inside multi-file Apple II zips (races its
# temp cleanup). So for appleii the ApplicationPath points at a loose LOCAL boot disk on
# the endpoint (C:\Apple2Disks\<zipbase>.<ext>), staged by push_launchbox's appleii
# post-step. The "primary" boot disk is the first name-sorted inner zip entry with a real
# disk-image ext (Side A), EXCLUDING the .m3u playlist. This mirror-matches the staging
# step so render output == staged filenames. Gated strictly to 'appleii' — no other
# system's render is affected.
APPLEII_STAGE_DIR = "C:\\Apple2Disks"
APPLEII_DISK_EXTS = ("woz", "dsk", "nib", "do", "po")   # bootable images; .m3u excluded


def appleii_primary_entry(zip_path) -> tuple[str, str] | None:
    """(inner_entry_name, ext) of an Apple II zip's primary/boot disk: the first
    name-sorted inner entry whose ext is a real disk image (excludes .m3u). None if the
    zip is unreadable or holds no disk image."""
    import zipfile
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = sorted(n for n in zf.namelist() if not n.endswith("/"))
    except Exception:
        return None
    for n in names:
        ext = n.rsplit(".", 1)[-1].lower() if "." in n else ""
        if ext in APPLEII_DISK_EXTS:
            return n, ext
    return None


def appleii_app_path(zip_filename: str, roms_root=None) -> str | None:
    """Local-disk ApplicationPath for an appleii game: C:\\Apple2Disks\\<zipbase>.<ext>
    where <zipbase> is the zip name minus '.zip' and <ext> is the primary boot disk's
    extension (inspected server-side). None if not a zip / no disk found (caller falls
    back to the normal J: path)."""
    from pathlib import Path
    from app.core.config import settings
    if not zip_filename.lower().endswith(".zip"):
        return None
    root = Path(roms_root) if roms_root else Path(settings.src_roms_path) / "appleii"
    prim = appleii_primary_entry(root / zip_filename)
    if not prim:
        return None
    _, ext = prim
    zipbase = zip_filename[:-4]   # strip '.zip'
    return f"{APPLEII_STAGE_DIR}\\{zipbase}.{ext}"


_PART_RE = re.compile(r"\((Part|Vol\.?|Side|Episode|Week|Chapter|Set)\s*([0-9A-Za-z]+)\)", re.I)
_SERIES_PREFIX = re.compile(r"^[^-]{1,12}\s-\s")            # 'SM64 - ', 'LoZMM - '
_TAG_SUFFIX = re.compile(r"\s*\((?:Hack|Unl|Unlicensed|Aftermarket|Demo)\)\s*$", re.I)

# Satellaview broadcasts were episodic. Romanised episode markers vary — 第N話 shows up as
# "Dai-N-wa" AND "Dai-N-Hanashi"; 第N回 as "Dai-N-kai" — normalise them all to "Episode N".
_EPISODE_RE = re.compile(r"Dai[-\s]?(\d+)[-\s]?(?:wa|kai|hanashi)\b"
                         r"|(?<![A-Za-z])(?:Week|Episode|Ep)[-\s]?(\d+)\b", re.I)
_BCAST_DATE_RE = re.compile(r"\((\d{1,2})-(\d{1,2})\)")     # air-date tag (M-D): (9-29), (7-14)
_ENG_TAG_RE = re.compile(r"\(En(?:[,)]|\s)", re.I)          # English (fan-translated) marker
_TRAIL_CRUFT = re.compile(r"(?:\s*\([^)]*\)|\s*\[[^\]]*\])+\s*$")  # trailing (...)/[...] groups


def _sat_qualifiers(fn: str) -> str:
    """' (M/D)' / ' (M/D, EN)' / ' (EN)' suffix from a Satellaview file's air-date + English tag."""
    q = []
    bd = _BCAST_DATE_RE.search(fn)
    if bd and 1 <= int(bd.group(1)) <= 12:
        q.append(f"{int(bd.group(1))}/{int(bd.group(2))}")
    if _ENG_TAG_RE.search(fn):
        q.append("EN")
    return f" ({', '.join(q)})" if q else ""


def _extra_name(primary_fn: str, extra_fn: str, folder: str | None = None) -> str:
    """Label for a multi-file additional app. Real discs -> 'Disc N'; explicit part/volume
    markers -> 'Part N' / 'Vol N'; Satellaview broadcasts -> 'Episode N (M/D, EN)' or the air
    date; otherwise (competition entries) -> the title minus the prefix shared with the primary."""
    from pathlib import PurePath
    m = _DISC_RE.search(extra_fn)
    if m:
        return f"Disc {m.group(1)}"
    pm = _PART_RE.search(extra_fn)
    if pm:
        return f"{pm.group(1).replace('.', '').strip().title()} {pm.group(2)}"
    if folder == "satellaview":
        em = _EPISODE_RE.search(extra_fn)
        if em:
            return f"Episode {int(em.group(1) or em.group(2))}" + _sat_qualifiers(extra_fn)
        bd = _BCAST_DATE_RE.search(extra_fn)
        if bd and 1 <= int(bd.group(1)) <= 12:
            return f"{int(bd.group(1))}/{int(bd.group(2))}" + (" (EN)" if _ENG_TAG_RE.search(extra_fn) else "")
    ep, pp = PurePath(extra_fn).stem, PurePath(primary_fn).stem
    i = 0
    while i < min(len(ep), len(pp)) and ep[i] == pp[i]:
        i += 1
    while i > 0 and ep[i - 1] not in " -_/([.":            # back up to a word boundary
        i -= 1
    name = ep[i:].lstrip(" -_([.").strip()
    if not name:
        name = ep
    if i == 0:                                            # no shared prefix -> drop a series prefix
        name = _SERIES_PREFIX.sub("", name)
    if folder == "satellaview":                           # collection sub-game (Kirby): keep the
        if " - " in name:                                 # distinctive tail, drop region/format cruft
            name = name.rsplit(" - ", 1)[1]
        name = _TRAIL_CRUFT.sub("", name).strip(" -_()[].") or name
    return _TAG_SUFFIX.sub("", name).strip() or ep


_PAREN_RE = re.compile(r"\(([^)]*)\)")


def _distinguishing_tag(fn: str, sibling_fns: list[str]) -> str | None:
    """A parenthetical tag in `fn` but in none of `sibling_fns` — breaks a label collision
    (e.g. an "[b]"/"(Uncensored)" variant sharing a date). Skips Disc/Part markers."""
    other = set()
    for o in sibling_fns:
        other.update(_PAREN_RE.findall(o))
    for tag in _PAREN_RE.findall(fn):
        if tag in other or _DISC_RE.search(f"({tag})") or _PART_RE.search(f"({tag})"):
            continue
        return tag
    return None


def _dedupe_labels(labels: list[str], filenames: list[str]) -> list[str]:
    """Disambiguate identical labels within one game's extras (e.g. two Satellaview '8/13')."""
    from collections import Counter
    counts = Counter(labels)
    out, seen = [], {}
    for lab, fn in zip(labels, filenames):
        if counts[lab] > 1:
            sibs = [f for f, l in zip(filenames, labels) if l == lab and f != fn]
            tag = _distinguishing_tag(fn, sibs)
            seen[lab] = seen.get(lab, 0) + 1
            out.append(f"{lab} ({tag})" if tag else f"{lab} ({seen[lab]})")
        else:
            out.append(lab)
    return out


def _disc_num(filename: str) -> int:
    m = _DISC_RE.search(filename)
    return int(m.group(1)) if m else 1


def _max_players(players: str | None) -> int | None:
    """Best-effort int from a free-text players field ("1-2", "4", "1" -> 2, 4, 1)."""
    if not players:
        return None
    nums = re.findall(r"\d+", players)
    return max(int(n) for n in nums) if nums else None


def _play_mode(players: str | None) -> str | None:
    n = _max_players(players)
    if n is None:
        return None
    return "Single Player" if n <= 1 else "Multiplayer"


def _release_dt(release_date: str | None) -> str | None:
    """LaunchBox wants an ISO datetime; RomFleet stores a plain "YYYY-MM-DD" (or
    partial) string. Pad to midnight, no offset — LaunchBox parses this fine."""
    if not release_date:
        return None
    d = release_date.strip()
    if len(d) == 4 and d.isdigit():   # year-only
        d = f"{d}-01-01"
    return f"{d}T00:00:00"


# ------------------------------------------------------------------------------------
# Game XML
# ------------------------------------------------------------------------------------

def _is_hidden(game: Game, endpoint: str | None) -> bool:
    """A game is hidden on an endpoint if hidden_on holds '*' (all agents) or that endpoint."""
    h = getattr(game, "hidden_on", None) or []
    return bool(endpoint) and ("*" in h or endpoint in h)


# LaunchBox platform -> per-game machine-mode override map (data/*.json). Games listed in the
# map's "overrides" (keyed by RA game id, with a rom-basename fallback) get a per-game
# <CommandLine> that REPLACES the EmulatorPlatform default; everything else emits no CommandLine
# and inherits the default. Lets pre-SR PC-8801 titles run in V1H etc. — mode is runtime-only, so
# it never affects the RA hash. Extend this dict to add more platforms (e.g. Apple II model).
_PLATFORM_MODE_FILES = {
    "NEC PC-8801": "pc8801_modes.json",
}


@lru_cache(maxsize=None)
def _platform_modes(filename: str) -> dict:
    p = Path(__file__).resolve().parents[1] / "data" / filename
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _mode_cmdline(game: Game, platform: str, app_path: str) -> str | None:
    """Per-game emulator CommandLine override for platforms with a machine-mode map (else None)."""
    fname = _PLATFORM_MODE_FILES.get(platform)
    if not fname:
        return None
    ov = _platform_modes(fname).get("overrides", {})
    ent = ov.get(str(game.ra_game_id)) if getattr(game, "ra_game_id", None) else None
    if not ent:   # fallback: match by rom-zip basename
        base = app_path.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        ent = next((e for e in ov.values() if e.get("rom") == base), None)
    return ent.get("args") if ent else None


def _game_xml(game: Game, platform: str, app_path: str, hidden: bool = False) -> str:
    gid = _guid("game", str(game.id))
    lines = ["  <Game>"]

    def tag(name: str, value):
        if value is None or value == "":
            return
        lines.append(f"    <{name}>{_esc(value)}</{name}>")

    tag("ID", gid)
    tag("Title", game.title)
    tag("SortTitle", game.sort_title)
    tag("Platform", platform)
    tag("Region", game.region)
    tag("Developer", game.developer)
    tag("Publisher", game.publisher)
    tag("Genre", game.genre)
    tag("Notes", game.description)
    tag("Rating", game.rating)
    tag("MaxPlayers", _max_players(game.players))
    tag("PlayMode", _play_mode(game.players))
    tag("ReleaseDate", _release_dt(game.release_date))
    if game.star_rating is not None:                       # personal grade
        tag("StarRatingFloat", f"{game.star_rating:.2f}")
        tag("StarRating", str(int(round(game.star_rating))))
    if game.community_rating is not None:                   # scraped crowd score (reference)
        tag("CommunityStarRating", f"{game.community_rating:.2f}")
    tag("ApplicationPath", app_path)
    tag("CommandLine", _mode_cmdline(game, platform, app_path))   # per-game machine mode (PC-8801 V1H etc.)
    dbid = (game.source_ids or {}).get("lbdb")
    if dbid:
        tag("DatabaseID", dbid)
    tag("DateAdded", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"))
    # play state (canonical) — merged in fleet-wide by favorites_sync before each push. PlayCount
    # is the accumulated total; LaunchBox's <LastPlayedDate> is ISO-8601; <PlayTime> is SECONDS and
    # is written from RA's authoritative playtime (agents' own PlayTime is ignored on read-back).
    if getattr(game, "play_count", 0):
        tag("PlayCount", game.play_count)
    _lp = getattr(game, "last_played", None)
    if _lp is not None:
        tag("LastPlayedDate", _lp.strftime("%Y-%m-%dT%H:%M:%S"))
    _ptm = getattr(game, "playtime_minutes", None)
    if _ptm:
        tag("PlayTime", int(_ptm) * 60)
    tag("Completed", "false")
    tag("Favorite", "true" if getattr(game, "is_favorite", False) else "false")
    tag("Hide", "true" if hidden else "false")
    tag("Broken", "false")
    lines.append("  </Game>")
    return "\n".join(lines)


def _additional_app_xml(game: Game, parent_guid: str, app_path: str, name: str,
                        priority: int) -> str:
    aid = _guid("aa", str(game.id), str(priority), name)
    # UseEmulator=true is REQUIRED: it makes LaunchBox launch this ApplicationPath through the
    # game's platform emulator (RetroArch) as a selectable "version to play". Without it LB
    # treats the entry as a direct file-launch and silently PRUNES it on the next save, so
    # multi-disc / competition versions vanish. AutoRun* are booleans (false), not 0; LaunchBox
    # has no <Priority> on AdditionalApplication — the XML order sets the list order.
    lines = [
        "  <AdditionalApplication>",
        f"    <Id>{_esc(aid)}</Id>",
        f"    <GameID>{_esc(parent_guid)}</GameID>",
        f"    <ApplicationPath>{_esc(app_path)}</ApplicationPath>",
        f"    <Name>{_esc(name)}</Name>",
        "    <UseEmulator>true</UseEmulator>",
        "    <AutoRunAfter>false</AutoRunAfter>",
        "    <AutoRunBefore>false</AutoRunBefore>",
        "    <WaitForExit>false</WaitForExit>",
        "  </AdditionalApplication>",
    ]
    return "\n".join(lines)


@dataclass
class RenderStats:
    system: str
    platform: str
    games: int = 0
    hidden: int = 0
    multi_disc: int = 0
    additional_apps: int = 0
    skipped_no_rom: int = 0
    media_copied: int = 0
    media_missing_primary: int = 0
    xml_result: dict = field(default_factory=dict)
    media_results: list = field(default_factory=list)


def _owned_games(db, folder: str) -> list[Game]:
    gids = [r[0] for r in db.query(RomFile.game_id).filter(
        RomFile.system_folder == folder, RomFile.match_status == "matched",
        RomFile.game_id.isnot(None)).distinct().all()]
    if not gids:
        return []
    return db.scalars(select(Game).where(Game.id.in_(gids)).options(
        selectinload(Game.roms), selectinload(Game.media))).all()


def _write_with_backup(path: Path, content: str, dry_run: bool, backup: bool = True) -> dict:
    """Dry-run + diff, backup-before-write. Never silently clobbers an existing file.

    backup=False skips the timestamped `.bak-` copy — used for the Batocera gamelist,
    which is written into the LIVE canonical share (Batocera mounts it directly) and is
    deterministically re-renderable, so accumulating backups there is pure pollution.
    """
    existed = path.exists()
    old = path.read_text(encoding="utf-8") if existed else ""
    changed = old != content
    diff = "\n".join(difflib.unified_diff(
        old.splitlines(), content.splitlines(),
        fromfile=f"{path} (existing)", tofile=f"{path} (new)", lineterm="")) if changed else ""
    result = {"path": str(path), "existed": existed, "changed": changed, "dry_run": dry_run}
    if dry_run:
        result["diff"] = diff
        return result
    if backup and existed and changed:
        bak = path.with_name(path.name + f".bak-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
        shutil.copy2(path, bak)
        result["backup"] = str(bak)
    if changed or not existed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        result["written"] = True
    else:
        result["written"] = False
    return result


# Arcade platforms carry a video per game (ArcadeDB shortplays). Deploying dozens/hundreds of
# these makes LaunchBox/BigBox autoplay them, which can hang a marginal GPU (LiveKernelEvent 141
# TDR) and crash the frontend while browsing. Skip video deploy for these — box art + snaps stay.
# (was {arcade,neogeo,neogeomvs,naomi,naomi2,atomiswave} — video was blanket-disabled after a GPU
# TDR crash on the LaunchBox endpoint. Root-caused 2026-07-20: the crash was BigBox rendering the HD arcade snaps on
# the weak Ryzen iGPU, not the videos themselves (all standard H.264). Fixed by pinning BigBox to the
# RTX 3060 via DirectX UserGpuPreferences. Verified smooth (Genesis 370 snaps + multi-platform
# screensaver), so video is re-enabled fleet-wide, arcade included.)
NO_VIDEO_SYSTEMS: set[str] = set()


def _copy_media(game: Game, platform: str, output_dir: Path, dry_run: bool,
                skip_video: bool = False) -> list[dict]:
    """Copy each media_type's primary asset to its LaunchBox-convention destination.
    Falls back to the first asset of that type if none is flagged is_primary."""
    results = []
    sanitized = sanitize_media_title(game.title)
    if not sanitized:
        return results
    by_type: dict[str, MediaAsset] = {}
    for m in game.media:
        if not m.path:
            continue
        if skip_video and m.media_type == "video":
            continue
        if m.media_type not in by_type or m.is_primary:
            by_type.setdefault(m.media_type, m)
            if m.is_primary:
                by_type[m.media_type] = m
    from app.core.config import settings
    media_store = Path(settings.canonical_path) / "media"
    for mtype, asset in by_type.items():
        target = MEDIA_TARGET.get(mtype)
        if not target:
            continue
        src = media_store / asset.path
        ext = Path(asset.path).suffix or ".jpg"
        sub = f"{target.root}/{platform}"
        if target.folder:
            sub += f"/{target.folder}"
        dest = output_dir / sub / f"{sanitized}-01{ext}"
        r = {"game": game.title, "media_type": mtype, "src": str(src), "dest": str(dest)}
        if not src.exists():
            r["error"] = "source missing"
            results.append(r)
            continue
        if dry_run:
            r["would_copy"] = not (dest.exists() and dest.stat().st_size == src.stat().st_size)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            r["copied"] = True
        results.append(r)
    return results


def render_platform(folder: str, rom_base: str, output_dir: str | Path,
                     dry_run: bool = False, db=None, media: bool = True,
                     endpoint: str | None = None) -> RenderStats:
    """Render one system's curated library to Data/Platforms/<Platform>.xml + media.

    rom_base: Windows-side path to this instance's roms root (e.g. "Z:\\roms" or
    "\\\\NAS\\share\\roms"). ApplicationPath = rom_base + "\\" + folder +
    "\\" + rom filename.
    output_dir: the LaunchBox export bundle root (a Data/, Images/, Videos/, Manuals/
    tree gets created under it).
    """
    sysc = get_system_by_folder(folder)
    if not sysc:
        raise ValueError(f"unknown system folder: {folder}")
    platform = platform_for(folder)
    if not platform:
        raise ValueError(f"no LaunchBox platform mapping for folder '{folder}' "
                          f"(console {sysc.ra_console_id}); add it to PLATFORM_OVERRIDES "
                          f"or scraper_lbdb.LBDB_PLATFORM")

    out = Path(output_dir)
    # clear this platform's bundle media so the render is a clean rebuild — otherwise stale files
    # (old box art with a different extension, removed videos) linger in the bundle and re-deploy.
    import shutil as _sh
    if not dry_run:
        for _t in ("Images", "Videos", "Manuals"):
            _sh.rmtree(out / _t / platform, ignore_errors=True)
    stats = RenderStats(system=folder, platform=platform)
    owns_db = db is None
    db = db or SessionLocal()
    try:
        games = _owned_games(db, folder)
        game_blocks: list[str] = []
        aa_blocks: list[str] = []

        for game in games:
            roms = [r for r in game.roms if r.system_folder == folder and r.match_status == "matched"]
            if not roms:
                stats.skipped_no_rom += 1
                continue
            roms.sort(key=lambda r: (_disc_num(r.filename), r.filename))
            primary, extras = roms[0], roms[1:]

            # appleii ONLY: point the Game at its loose LOCAL boot disk (C:\Apple2Disks\
            # <zipbase>.<ext>) — RAppleWin can't read the J: share (see appleii_app_path).
            # Falls back to the normal J: path if the zip can't be inspected. Every other
            # system keeps the standard rom_base\folder\filename path unchanged.
            if folder == "appleii":
                app_path = (appleii_app_path(primary.filename)
                            or _app_path(rom_base, folder, primary.filename))
            else:
                app_path = _app_path(rom_base, folder, _rom_subpath(primary))
            hidden = _is_hidden(game, endpoint)
            game_blocks.append(_game_xml(game, platform, app_path, hidden=hidden))
            stats.games += 1
            if hidden:
                stats.hidden += 1

            if extras:
                stats.multi_disc += 1
                gid = _guid("game", str(game.id))
                names = _dedupe_labels(
                    [_extra_name(primary.filename, r.filename, folder) for r in extras],
                    [r.filename for r in extras])
                for i, (r, name) in enumerate(zip(extras, names), start=2):
                    prio = _disc_num(r.filename) if _DISC_RE.search(r.filename) else i
                    aa_path = _app_path(rom_base, folder, _rom_subpath(r))
                    aa_blocks.append(_additional_app_xml(game, gid, aa_path, name, prio))
                    stats.additional_apps += 1

            if media:
                media_results = _copy_media(game, platform, out, dry_run,
                                            skip_video=folder in NO_VIDEO_SYSTEMS)
                stats.media_results.extend(media_results)
                for r in media_results:
                    if r.get("error"):
                        continue
                    stats.media_copied += 1
            if not any(m.media_type == "boxfront" for m in game.media):
                stats.media_missing_primary += 1

        xml = ['<?xml version="1.0" encoding="utf-8"?>', "<LaunchBox>"]
        xml.extend(game_blocks)
        xml.extend(aa_blocks)
        xml.append("</LaunchBox>")
        content = "\n".join(xml) + "\n"

        xml_path = out / "Data" / "Platforms" / f"{platform}.xml"
        stats.xml_result = _write_with_backup(xml_path, content, dry_run)
    finally:
        if owns_db:
            db.close()

    log.info("render_platform", folder=folder, platform=platform, games=stats.games,
              multi_disc=stats.multi_disc, media_copied=stats.media_copied,
              missing_boxfront=stats.media_missing_primary, dry_run=dry_run)
    return stats


def render_all(rom_base: str, output_dir: str | Path, dry_run: bool = False) -> list[RenderStats]:
    """Render every system folder that has a resolvable LaunchBox platform + owned games."""
    results = []
    db = SessionLocal()
    try:
        for sysc in load_systems():
            if not platform_for(sysc.folder):
                log.warning("render_all: no platform mapping, skipping", folder=sysc.folder)
                continue
            if not _owned_games(db, sysc.folder):
                continue
            results.append(render_platform(sysc.folder, rom_base, output_dir, dry_run, db))
    finally:
        db.close()
    return results
