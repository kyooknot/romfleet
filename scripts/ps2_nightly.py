"""PS2 nightly re-hash runner: resumable, gentle, stops at 06:00 America/New_York.
Based on the proven local_match_pilot.py onboarder; adds a wall-clock stop + PROGRESS
lines for the chat heartbeat. Normally only runs inside the 23:00->06:00 window (the
initial guard), but PS2_FORCE=1 lets it start any time (still stops at the next 06:00).
Resumability is inherent: missing_for_system() only returns still-missing games."""
import asyncio, shutil, sys, os
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from app.services.missing import missing_for_system
from app.services.sourcing import _ra_game, _norm
from app.services.ingest import _get_or_create_game, _upsert_rom
from app.core.systems import get_system_by_folder
from app.core.elasticsearch import get_es, init_es, close_es
from app.hashers import get_hasher
from app.db.session import SessionLocal

FOLDER = sys.argv[1] if len(sys.argv) > 1 else "ps2"
FORCE = bool(os.environ.get("PS2_FORCE"))
EXTS = (".chd", ".cue", ".iso", ".cso", ".rvz", ".gdi", ".bin")
SRC = Path(f"/mnt/src/roms/{FOLDER}"); CANON = Path(f"/mnt/romfleet/roms/{FOLDER}")
EDT = ZoneInfo("America/New_York")
def log(*a): print(*a, flush=True)
_HC = {}

def daytime() -> bool:
    return 6 <= datetime.now(EDT).hour < 23

def next_6am(now):
    t = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if now >= t:
        t += timedelta(days=1)
    return t

async def h1(hasher, f):
    if f in _HC: return _HC[f]
    try:
        r = (await asyncio.wait_for(hasher.hash_file(str(f)), timeout=300)).lower()
    except Exception as e:
        log("   hashfail", f.name, str(e)[:60]); r = None
    _HC[f] = r; return r

async def main():
    now0 = datetime.now(EDT)
    if not FORCE and daytime():
        log(f"=== [{FOLDER}] not in window (now {now0:%H:%M} EDT); exiting ==="); return
    stop_dt = next_6am(now0)
    sysc = get_system_by_folder(FOLDER); hasher = get_hasher(sysc.hash_method)
    CANON.mkdir(parents=True, exist_ok=True)
    locals_ = sorted(f for f in SRC.iterdir() if f.suffix.lower() in EXTS) if SRC.exists() else []
    lnorm = [(f, _norm(f.stem)) for f in locals_]
    await init_es(); db = SessionLocal()
    m = await missing_for_system(FOLDER); wanted = [g for g in m["missing"] if g.get("id")]
    canon_now = len(list(CANON.glob("*.chd")))
    log(f"=== [{FOLDER}] NIGHT START {now0:%Y-%m-%d %H:%M} EDT (force={FORCE}) stop={stop_dt:%H:%M} "
        f"missing={len(wanted)} canonical={canon_now} local_files={len(locals_)} ===")
    if not locals_: log("NO LOCAL FILES"); db.close(); await close_es(); return
    added = []; nohit = []; processed = 0; closed = False
    for g in wanted:
        if datetime.now(EDT) >= stop_dt:
            log("=== reached 06:00 EDT — stopping cleanly ==="); closed = True; break
        processed += 1
        ra = await _ra_game(get_es(), g["id"])
        if not ra: nohit.append(g["title"] + " [no-ra]"); continue
        gn = _norm(ra.get("title", g["title"])); gw = [w for w in gn.split() if len(w) > 2]
        longest = max(gw, key=len) if gw else ""
        cands = [f for f, fn in lnorm if fn == gn or (len(gn) > 4 and (gn in fn or fn in gn)) or
                 (gw and sum(w in fn.split() for w in gw) >= max(2, len(gw) - 1)) or
                 (len(longest) >= 5 and longest in fn.split())]
        if not cands: nohit.append(g["title"] + " [no-file]"); continue
        md5map = {(x.get("md5") or "").lower(): (x.get("name") or "") for x in ra.get("hashes", []) if x.get("md5")}
        discs = []
        for f in cands:
            h = await h1(hasher, f)
            if h and h in md5map: discs.append((h, f, md5map[h]))
        if not discs: nohit.append(g["title"] + " [no-hash-match]"); continue
        for h, f, disp in discs:
            dest = CANON / f.name
            if not dest.exists(): shutil.copy2(f, dest)
            hit = {"ra_game_id": g["id"], "title": ra.get("title", g["title"]), "hash_name": disp or f.name,
                   "num_achievements": ra.get("num_achievements", 0), "points": ra.get("points", 0)}
            game = _get_or_create_game(db, hit, sysc.ra_console_id)
            _upsert_rom(db, dest, FOLDER, sysc.hash_method, h, "matched", disp or f.name, game.id, {"ra": disp}, None)
        db.commit()
        added.append(g["title"])
        log(f"   ✓ [{len(added)}] {g['title']} <- {[d[1].name for d in discs]}")
        if processed % 20 == 0:
            log(f"PROGRESS added={len(added)} processed={processed}/{len(wanted)} "
                f"canonical={len(list(CANON.glob('*.chd')))}")
    canon_end = len(list(CANON.glob("*.chd")))
    log(f"=== [{FOLDER}] NIGHT DONE {datetime.now(EDT):%H:%M} EDT added={len(added)} "
        f"processed={processed}/{len(wanted)} canonical={canon_end} "
        f"still_missing_targets={len(wanted) - len(added)} window_closed={closed} ===")
    db.close(); await close_es()

asyncio.run(main())
