"""Size missing-video: per system_folder, owned games with NO video MediaAsset."""
import sys
from pathlib import Path
# Resolve the backend package relative to THIS file, so the tool works under any
# install prefix rather than only /opt/romfleet.
sys.path.insert(0, str(Path(__file__).resolve().parents[0] / "backend"))
from collections import defaultdict
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.db.session import SessionLocal
from app.db.models import Game, MediaAsset, RomFile
from app.services.scraper_emumovies import emu_system_for

with SessionLocal() as db:
    # owned game_id -> set of folders (owned = has a rom_file mapped to it)
    rows = db.query(RomFile.game_id, RomFile.system_folder).filter(
        RomFile.game_id.isnot(None)).distinct().all()
    game_folders = defaultdict(set)
    for gid, folder in rows:
        game_folders[gid].add(folder)
    owned_ids = set(game_folders)

    # game_ids that HAVE a video asset
    vids = set(r[0] for r in db.query(MediaAsset.game_id).filter(
        MediaAsset.media_type == "video").distinct().all())

    # pick a representative folder per game (first, sorted) for grouping
    games = db.scalars(select(Game).where(Game.id.in_(owned_ids))).all()
    gmap = {g.id: g for g in games}

    per = defaultdict(lambda: {"owned": 0, "missing": 0, "console": None, "emu": None})
    total_owned = total_missing = 0
    for gid in owned_ids:
        folder = sorted(game_folders[gid])[0]
        g = gmap.get(gid)
        cid = g.console_id if g else None
        d = per[folder]
        d["owned"] += 1
        d["console"] = cid
        d["emu"] = emu_system_for(folder, cid)
        total_owned += 1
        if gid not in vids:
            d["missing"] += 1
            total_missing += 1

    print(f"{'FOLDER':<22}{'CONSOLE':>8}{'OWNED':>8}{'MISSING':>9}  EMU_SYSTEM")
    print("-" * 80)
    for folder in sorted(per, key=lambda f: -per[f]["missing"]):
        d = per[folder]
        if d["missing"] == 0:
            continue
        print(f"{folder:<22}{str(d['console']):>8}{d['owned']:>8}{d['missing']:>9}  {d['emu'] or '(NO EMU MAP)'}")
    print("-" * 80)
    # also list folders fully covered
    covered = [f for f in per if per[f]["missing"] == 0]
    print(f"Folders with 0 missing: {len(covered)}")
    print(f"TOTAL owned={total_owned}  TOTAL missing-video={total_missing}")
    # folders with no emu map (external gap possibility)
    noemu = sorted(set(f for f in per if per[f]["emu"] is None and per[f]["missing"] > 0))
    if noemu:
        print("Folders MISSING an EmuMovies system map:", noemu)
