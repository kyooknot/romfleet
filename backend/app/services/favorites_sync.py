"""Bidirectional user-state sync (Favorites + personal Star ratings): pull a frontend's
per-game state back into RomFleet's central store so it survives re-pushes and propagates
to every endpoint on the next render.

GUID mapping: the renderer stamps each <Game><ID> as uuid5(NS, "game|<game.id>") — a
deterministic id LaunchBox preserves across its on-exit rewrites — so endpoint state
reverses cleanly back to a RomFleet game id.

DIFF-BASED (default): apply only what the user changed ON THE ENDPOINT since our last
deploy — the endpoint's current XML vs the bundle's last-rendered XML (the baseline).
This captures frontend toggles/grades WITHOUT clobbering values authored in the web UI
(which live centrally and match the last-deployed baseline). Works both directions.

mirror=True makes the endpoint fully authoritative (central := endpoint) — a manual full
reconcile from one endpoint.
"""
from __future__ import annotations

import base64
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import structlog

from app.db.session import SessionLocal
from app.db.models import Game, RomFile
from app.core.config import settings
from app.core.systems import load_systems
from app.services.render_launchbox import _guid, platform_for
from app.services.push_launchbox import get_endpoint, _ssh, _ps_encoded, Endpoint

log = structlog.get_logger()


# ------------------------------------------------------------------------------------
# Shared play-state merge rules (applied identically by every sync-IN reader)
# ------------------------------------------------------------------------------------
# RomFleet's DB is the merge hub. A surface (an endpoint name from endpoints.yaml, or "batocera")
# reports its CURRENT per-game play_count; we accumulate only the NEW plays since we last
# observed that surface, tracked in Game.play_sync[surface]. Because sync-OUT re-baselines
# play_sync[surface] to the canonical total right after writing it back to the surface, the
# value the surface echoes on the NEXT read yields a zero delta — no double-count.


def _parse_int(text: str | None) -> int | None:
    if text is None:
        return None
    try:
        return int(float(str(text).strip()))
    except (ValueError, TypeError):
        return None


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_lb_datetime(text: str | None) -> datetime | None:
    """LaunchBox <LastPlayedDate> is a .NET ISO-8601 string, e.g.
    '2024-01-15T20:30:00.0000000-08:00'. A never-played game carries DateTime.MinValue
    ('0001-01-01T00:00:00') or omits the tag — both -> None."""
    if not text:
        return None
    s = text.strip()
    if s.startswith("0001-01-01"):
        return None
    # trim .NET 7-digit fractional seconds (Python handles at most 6) before fromisoformat
    import re as _re
    s = _re.sub(r"(\.\d{6})\d+", r"\1", s)
    try:
        return _aware(datetime.fromisoformat(s))
    except ValueError:
        try:
            return _aware(datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            return None


def _parse_es_datetime(text: str | None) -> datetime | None:
    """Batocera/EmulationStation <lastplayed> is '%Y%m%dT%H%M%S'. Zero/blank -> None."""
    if not text:
        return None
    s = text.strip()
    if not s or s.startswith("00000000") or s.startswith("19700101T0000"):
        return None
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def apply_playcount(game: Game, surface: str, surface_current: int | None) -> int:
    """APPEND-merge a surface's current play_count into the canonical total. Adds only the
    plays new since we last observed `surface`; always re-baselines play_sync[surface] to the
    surface's current value. Returns the delta added."""
    if surface_current is None:
        return 0
    ps = dict(game.play_sync or {})
    prev = ps.get(surface, 0) or 0
    delta = max(0, surface_current - prev)
    if delta:
        game.play_count = (game.play_count or 0) + delta
    ps[surface] = surface_current
    game.play_sync = ps
    return delta


def apply_last_played(game: Game, dt: datetime | None) -> bool:
    """last_played := max(existing, dt)."""
    if dt is None:
        return False
    cur = _aware(game.last_played)
    dt = _aware(dt)
    if cur is None or dt > cur:
        game.last_played = dt
        return True
    return False


def writeback_playcount(surface: str, systems: list[str] | None = None) -> int:
    """Re-baseline play_sync[surface] = play_count for the games in `systems` (or all) AFTER
    a render/push OUT has written the canonical play_count onto that surface. This is the
    anti-double-count invariant: the value the surface now shows == the value it will echo on
    the next read, so that read contributes a zero delta. Call ONLY after a successful write to
    the LIVE surface."""
    db = SessionLocal()
    try:
        q = db.query(Game)
        if systems:
            gids = [r[0] for r in db.query(RomFile.game_id).filter(
                RomFile.system_folder.in_(systems), RomFile.game_id.isnot(None)).distinct()]
            if not gids:
                return 0
            q = q.filter(Game.id.in_(gids))
        n = 0
        for g in q.all():
            ps = dict(g.play_sync or {})
            if ps.get(surface) != (g.play_count or 0):
                ps[surface] = g.play_count or 0
                g.play_sync = ps
                n += 1
        db.commit()
        return n
    finally:
        db.close()


def _fetch_platform_xml(ep: Endpoint, platform: str) -> str | None:
    """Read the endpoint's Data\\Platforms\\<platform>.xml as text (base64 over ssh to
    preserve exact bytes/encoding). None if absent."""
    remote = f"{ep.lb_path.rstrip(chr(92))}\\Data\\Platforms\\{platform}.xml"
    script = (f"if(Test-Path -LiteralPath '{remote}')"
              f"{{[Convert]::ToBase64String([IO.File]::ReadAllBytes('{remote}'))}}")
    r = _ssh(ep, _ps_encoded(script))
    b64 = r.stdout.strip()
    if r.returncode != 0 or not b64:
        return None
    try:
        return base64.b64decode(b64).decode("utf-8", errors="replace")
    except Exception:
        return None


def _star(text: str | None) -> float | None:
    """Parse a LaunchBox star value; 0/blank means unrated -> None."""
    if not text:
        return None
    try:
        v = float(text)
    except ValueError:
        return None
    return v if v > 0 else None


def _parse_game_state(xml_text: str) -> dict[str, dict]:
    """{<ID> GUID: {"fav", "star", "playcount", "lastplayed"}} for each <Game>.
    <PlayTime> (LaunchBox's own seconds counter) is intentionally NOT read — playtime is
    RA-authoritative and the agents' value is ignored/overwritten."""
    out: dict[str, dict] = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for g in root.findall("Game"):
        gid = (g.findtext("ID") or "").strip()
        if not gid:
            continue
        fav = (g.findtext("Favorite") or "false").strip().lower() == "true"
        star = _star(g.findtext("StarRatingFloat") or g.findtext("StarRating"))
        out[gid] = {"fav": fav, "star": star,
                    "playcount": _parse_int(g.findtext("PlayCount")),
                    "lastplayed": _parse_lb_datetime(g.findtext("LastPlayedDate"))}
    return out


def _bundle_state(ep: Endpoint, platform: str) -> dict[str, dict]:
    """State we LAST rendered/deployed for this platform (baseline to diff against)."""
    p = Path(ep.bundle_dir) / "Data" / "Platforms" / f"{platform}.xml"
    if not p.exists():
        return {}
    try:
        return _parse_game_state(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def sync_user_state(endpoint: str = "", systems: list[str] | None = None,
                    mirror: bool = False) -> dict:
    """Pull Favorite + personal Star rating from `endpoint` into Game (diff-based)."""
    endpoint = endpoint or settings.default_endpoint
    ep = get_endpoint(endpoint)
    if systems:
        platforms = {platform_for(s) for s in systems}
    else:
        platforms = {platform_for(s.folder) for s in load_systems()}
    platforms = {p for p in platforms if p}

    db = SessionLocal()
    try:
        games = db.query(Game).all()
        guid_to_game = {_guid("game", str(g.id)): g for g in games}
        seen = fav_changed = star_changed = play_added = last_changed = 0
        for platform in sorted(platforms):
            xml_text = _fetch_platform_xml(ep, platform)
            if not xml_text:
                continue
            deployed = _bundle_state(ep, platform)
            for guid, st in _parse_game_state(xml_text).items():
                g = guid_to_game.get(guid)
                if g is None:
                    continue                      # AA-promoted / foreign game — skip
                seen += 1
                base = deployed.get(guid, {})
                # favorite (diff-based OR-merge; existing behaviour)
                if (mirror or st["fav"] != base.get("fav", False)) and g.is_favorite != st["fav"]:
                    g.is_favorite = st["fav"]
                    fav_changed += 1
                # personal star rating (existing behaviour)
                if (mirror or st["star"] != base.get("star")) and g.star_rating != st["star"]:
                    g.star_rating = st["star"]
                    star_changed += 1
                # play_count: APPEND (delta via play_sync — no double-count through write-back)
                if apply_playcount(g, endpoint, st["playcount"]):
                    play_added += 1
                # last_played: max(existing, endpoint)
                if apply_last_played(g, st["lastplayed"]):
                    last_changed += 1
        db.commit()
    finally:
        db.close()
    result = {"endpoint": endpoint, "platforms": len(platforms), "mirror": mirror,
              "games_matched": seen, "fav_changed": fav_changed, "star_changed": star_changed,
              "play_added": play_added, "last_played_changed": last_changed}
    log.info("sync_user_state", **result)
    return result


# Back-compat alias (callers/CLI historically said "favorites").
def sync_favorites(endpoint: str = "", systems: list[str] | None = None,
                   mirror: bool = False) -> dict:
    endpoint = endpoint or settings.default_endpoint
    return sync_user_state(endpoint, systems, mirror)


# ------------------------------------------------------------------------------------
# Batocera sync-IN (surface = "batocera")
# ------------------------------------------------------------------------------------
# Batocera mounts the canonical NAS share directly and writes gamelist.xml IN PLACE, so the
# device's edits are readable SERVER-SIDE at <canonical>/roms/<folder>/gamelist.xml — no
# push/pull round-trip. We match each <game> back to a DB Game by the rom filename in <path>
# (the same name render_gamelist emitted) and apply the shared merge rules (source="batocera").
#
# Multi-file (competition/disc) sets emit ONE <game> per file, all representing the same DB
# game. Batocera tracks play state per FILE, so we AGGREGATE a group with MAX playcount (not
# SUM — sync-OUT writes the same canonical total onto every entry, so SUM would N-times
# overcount on the read-back; MAX yields a clean zero delta when nothing new was played and
# still catches new plays on any single entry) and MAX lastplayed, OR-ing favorite.
#
# metadata/media read-back (user-edited <name>/<desc>/scraped art on the device) is NOT done
# here — this task is play-STATE only; see the report's follow-on note.

def _parse_bato_state(xml_text: str) -> dict[str, dict]:
    """{rom filename: {"fav", "playcount", "lastplayed"}} for each <game>, keyed by the
    basename of <path> (== RomFile.filename)."""
    out: dict[str, dict] = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for g in root.findall("game"):
        path = (g.findtext("path") or "").strip()
        if not path:
            continue
        fn = path.replace("\\", "/").rsplit("/", 1)[-1]
        out[fn] = {"fav": (g.findtext("favorite") or "false").strip().lower() == "true",
                   "playcount": _parse_int(g.findtext("playcount")),
                   "lastplayed": _parse_es_datetime(g.findtext("lastplayed"))}
    return out


def sync_batocera_state(folder: str, db=None) -> dict:
    """Pull favorite + play_count + last_played from the server-side Batocera gamelist.xml for
    one system into the DB (source='batocera'). No-op if the gamelist is absent."""
    surface = "batocera"
    gamelist = Path(settings.canonical_path) / "roms" / folder / "gamelist.xml"
    if not gamelist.exists():
        return {"folder": folder, "gamelist": False}
    try:
        state = _parse_bato_state(gamelist.read_text(encoding="utf-8"))
    except Exception as e:  # noqa
        log.warning("batocera sync read failed", folder=folder, err=str(e))
        return {"folder": folder, "error": str(e)[:150]}

    owns_db = db is None
    db = db or SessionLocal()
    try:
        roms = db.query(RomFile).filter(RomFile.system_folder == folder,
                                        RomFile.game_id.isnot(None)).all()
        # aggregate the device's per-file state up to the owning game
        by_game: dict[int, dict] = {}
        for r in roms:
            st = state.get(r.filename)
            if not st:
                continue
            agg = by_game.setdefault(r.game_id, {"fav": False, "playcount": None, "lastplayed": None})
            agg["fav"] = agg["fav"] or st["fav"]
            if st["playcount"] is not None:
                agg["playcount"] = max(agg["playcount"] or 0, st["playcount"])
            if st["lastplayed"] is not None and (agg["lastplayed"] is None
                                                 or st["lastplayed"] > agg["lastplayed"]):
                agg["lastplayed"] = st["lastplayed"]

        seen = fav_changed = play_added = last_changed = 0
        for gid, agg in by_game.items():
            g = db.get(Game, gid)
            if g is None:
                continue
            seen += 1
            # favorite: OR-merge only (no per-device baseline to safely turn one OFF)
            if agg["fav"] and not g.is_favorite:
                g.is_favorite = True
                fav_changed += 1
            if apply_playcount(g, surface, agg["playcount"]):
                play_added += 1
            if apply_last_played(g, agg["lastplayed"]):
                last_changed += 1
        db.commit()
    finally:
        if owns_db:
            db.close()
    result = {"folder": folder, "gamelist": True, "games_matched": seen,
              "fav_changed": fav_changed, "play_added": play_added,
              "last_played_changed": last_changed}
    log.info("sync_batocera_state", **result)
    return result
