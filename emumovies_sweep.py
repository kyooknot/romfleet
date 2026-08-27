"""EmuMovies (+ ArcadeDB for arcade-family) video-snap sweep.
Scrape+store ONLY. Per owned game missing a video asset, pull a video and store it as a
video MediaAsset in the canonical media store. Gentle, sequential, paced.
"""
import sys, time, json
# Resolve the backend package relative to THIS file, so the tool works under any
# install prefix rather than only /opt/romfleet.
sys.path.insert(0, str(Path(__file__).resolve().parents[0] / "backend"))
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.db.session import SessionLocal
from app.db.models import Game, MediaAsset, RomFile
from app.core.config import settings
from app.services.scraper_emumovies import EmuMovies, scrape_video, emu_system_for, _arcade_search_names
from app.services.scraper_arcadedb import ArcadeDB
from app.services.scraper_util import save_media, set_source

MEDIA_STORE = Path(settings.canonical_path) / "media"
ARCADE_FOLDERS = {"arcade", "neogeo", "neogeomvs", "naomi", "naomi2", "atomiswave"}
EMU_SLEEP = 0.25
ADB_SLEEP = 0.20

import re
_CAT = re.compile(r"\s*\((?:Homebrew|Hack|Unlicensed|Prototype|Demo|Test Kit|Subset|Bonus|Multi)\)\s*$", re.I)

def name_variants(title):
    """Conservative EXACT-match name variants for EmuMovies (still exact on their side).
    Primary (raw stripped title) is tried by scrape_video; these are the fallbacks."""
    base = _CAT.sub("", title).strip()
    out = []
    def add(s):
        s = re.sub(r"\s+", " ", s).strip()
        if s and s != base and s not in out:
            out.append(s)
    if ":" in base:
        add(base.replace(":", " -"))   # "A: B" -> "A - B"
        add(base.replace(":", ""))      # "A: B" -> "A B"
    if "&" in base:
        add(base.replace("&", "and"))
    if re.search(r"\band\b", base):
        add(re.sub(r"\band\b", "&", base))
    if base.lower().startswith("the "):
        add(base[4:])
    # combined colon+ampersand
    if ":" in base and "&" in base:
        add(base.replace(":", " -").replace("&", "and"))
    return out

def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)

def owned_missing_video(db):
    """folder -> [game_id,...] owned games with NO video asset (representative folder = first sorted)."""
    rows = db.query(RomFile.game_id, RomFile.system_folder).filter(
        RomFile.game_id.isnot(None)).distinct().all()
    gf = defaultdict(set)
    for gid, folder in rows:
        gf[gid].add(folder)
    vids = set(r[0] for r in db.query(MediaAsset.game_id).filter(
        MediaAsset.media_type == "video").distinct().all())
    by_folder = defaultdict(list)
    for gid, folders in gf.items():
        if gid in vids:
            continue
        by_folder[sorted(folders)[0]].append(gid)
    return by_folder

def arcadedb_video(db, game, adb):
    """Targeted ArcadeDB VIDEO-only fetch (keyed by MAME romname). Returns True if stored."""
    r = game.roms[0] if game.roms else None
    rom = r.filename.rsplit(".", 1)[0] if (r and r.filename) else None
    if not rom:
        return False
    j = adb.lookup(rom)
    if not j:
        return False
    url = j.get("url_video_shortplay_hd") or j.get("url_video_shortplay")
    if not url:
        return False
    if save_media(db, game, "video", "arcadedb", url, MEDIA_STORE, adb.http, ext=".mp4"):
        if j.get("game_name"):
            set_source(game, "arcadedb", j["game_name"], j.get("title"))
        return True
    return False

def main():
    t0 = time.time()
    emu = EmuMovies()
    if not emu.configured:
        log("FATAL: EmuMovies not configured"); return
    if not emu.login():
        log("FATAL: EmuMovies login failed"); return
    log(f"EmuMovies login OK (session set). Media store: {MEDIA_STORE}")
    adb = ArcadeDB()

    results = {}
    with SessionLocal() as db:
        by_folder = owned_missing_video(db)
        folders = sorted(by_folder, key=lambda f: -len(by_folder[f]))
        grand_before = sum(len(v) for v in by_folder.values())
        log(f"Folders to sweep: {len(folders)}  total missing-video: {grand_before}")

        for folder in folders:
            gids = by_folder[folder]
            games = db.scalars(select(Game).where(Game.id.in_(gids)).options(
                selectinload(Game.media), selectinload(Game.roms))).all()
            emu_ok = 0
            emu_var_ok = 0
            adb_ok = 0
            no_map = 0
            attempted = 0
            is_arcade = folder in ARCADE_FOLDERS
            # if no emu system AND not arcade -> skip entirely (external gap)
            sample_cid = games[0].console_id if games else None
            has_emu = any(emu_system_for(folder, g.console_id) for g in games[:1]) or emu_system_for(folder, sample_cid)
            log(f">>> {folder} ({len(games)} missing) arcade={is_arcade} emu_map={bool(has_emu)}")
            for g in games:
                attempted += 1
                got = False
                lookup = emu_system_for(folder, g.console_id)
                if lookup:
                    try:
                        res = scrape_video(db, g, emu, MEDIA_STORE, folder=folder)
                        if res.get("video") == "downloaded":
                            emu_ok += 1; got = True
                        elif res.get("error"):
                            no_map += 1
                    except Exception as e:
                        log(f"    emu error gid={g.id}: {str(e)[:120]}")
                    time.sleep(EMU_SLEEP)
                    # variant fallback (title-keyed systems only; MAME uses romset names already)
                    if not got and lookup != "MAME" and not (g.source_ids or {}).get("emumovies"):
                        for vname in name_variants(g.title):
                            try:
                                url = emu.search_video(vname, lookup)
                            except Exception:
                                url = None
                            time.sleep(EMU_SLEEP)
                            if url and save_media(db, g, "video", "emumovies", url, MEDIA_STORE, emu.http, ext=".mp4"):
                                set_source(g, "emumovies", vname, vname)
                                emu_var_ok += 1; got = True
                                break
                else:
                    no_map += 1
                if not got and is_arcade:
                    try:
                        if arcadedb_video(db, g, adb):
                            adb_ok += 1; got = True
                    except Exception as e:
                        log(f"    adb error gid={g.id}: {str(e)[:120]}")
                    time.sleep(ADB_SLEEP)
                if attempted % 50 == 0:
                    db.commit()
                    log(f"    {folder}: {attempted}/{len(games)} emu={emu_ok} var={emu_var_ok} adb={adb_ok}")
            db.commit()
            filled = emu_ok + emu_var_ok + adb_ok
            still = len(games) - filled
            results[folder] = {"before": len(games), "emu": emu_ok, "emu_variant": emu_var_ok,
                               "adb": adb_ok, "filled": filled, "still": still, "no_map": no_map,
                               "console": sample_cid, "emu_system": emu_system_for(folder, sample_cid)}
            log(f"<<< {folder}: emu={emu_ok} var={emu_var_ok} adb={adb_ok} filled={filled} still_missing={still}")
            # persist running results
            Path(str(Path(__file__).resolve().parents[0] / "logs" / "emumovies_sweep_results.json")).write_text(json.dumps(results, indent=2))

    emu.close(); adb.close()
    dt = time.time() - t0
    tot_emu = sum(r["emu"] for r in results.values())
    tot_var = sum(r["emu_variant"] for r in results.values())
    tot_adb = sum(r["adb"] for r in results.values())
    tot_before = sum(r["before"] for r in results.values())
    tot_filled = tot_emu + tot_var + tot_adb
    log("=" * 70)
    log(f"DONE in {dt/60:.1f} min. filled emu={tot_emu} variant={tot_var} adb={tot_adb} "
        f"total_filled={tot_filled} / before={tot_before} still={tot_before-tot_filled}")
    Path(str(Path(__file__).resolve().parents[0] / "logs" / "emumovies_sweep_results.json")).write_text(json.dumps(results, indent=2))

if __name__ == "__main__":
    main()
