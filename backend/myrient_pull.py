#!/usr/bin/env python3
"""Match every missing game for a system against the Myrient RA sets, extract and onboard."""
import asyncio, json, os, re, sys, urllib.request
from pathlib import Path
from urllib.parse import quote
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend")); os.chdir(Path(__file__).resolve().parents[1] / "backend")
import app.services.sourcing as S
from app.services.remote_zip import RemoteZip
from app.hashers import get_hasher
from app.core.systems import get_system_by_folder
from app.core.config import settings
from app.core.elasticsearch import get_es, init_es, close_es
from app.services.ingest import gate_lookup, _get_or_create_game, _upsert_rom
from app.db.session import SessionLocal

SETS = {
    "ps2": ["myrient-retroachievements-ps-2-aka-playstation-2-roms-from-a-l-december-1-2025",
            "myrient-retroachievements-ps-2-aka-playstation-2-roms-from-m-to-z-december-1-2025"],
    "interton": ["myrient-retroachievements-interton-vc-4000-roms-october-25-2025"],
    "gamecube": ["myrient-retroachievements-gamecube-roms-october-28-2025"],
    "mastersystem": ["myrient-retroachievements-sega-master-system-roms-december-1-2025"],
    "saturn": ["myrient-retroachievements-sega-saturn-roms-october-30-2025"],
    "neogeocd": ["myrient-retroachievements-snk-neo-geo-cd-roms-october-24-2025"],
}
APPLY = "--apply" in sys.argv
FOLDER = sys.argv[1]

def key(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

async def main():
    miss = json.load(urllib.request.urlopen("http://127.0.0.1:8000/api/missing", timeout=90))
    rows = miss if isinstance(miss, list) else (miss.get("missing") or [])
    want = [r for r in rows if r.get("system") == FOLDER]
    print("%s: %d missing" % (FOLDER, len(want)))
    sysc = get_system_by_folder(FOLDER)
    c = S._cookies()
    canon = Path(settings.canonical_path) / "roms" / FOLDER
    stage = Path("/mnt/romfleet/_staging/rzip"); stage.mkdir(parents=True, exist_ok=True)
    await init_es()
    db = SessionLocal()
    got = 0
    try:
        for item in SETS.get(FOLDER, []):
            zs = [f.get("name") for f in S._item_files(item, c)
                  if (f.get("name") or "").lower().endswith(".zip")]
            if not zs:
                print("  %s: no zip" % item[-40:]); continue
            z = RemoteZip(f"{S.ARCHIVE}/download/{quote(item)}/{quote(zs[0])}", cookies=c)
            try:
                members = z.members()
            except Exception as e:
                print("  %s: listing failed %s" % (item[-40:], str(e)[:70])); continue
            print("  %s -> %d members" % (item[-44:], len(members)))
            for r in list(want):
                t = key(r.get("title"))
                cand = [m for m in members if key(m.name.split("/")[-1])[:len(t)] == t or t[:24] in key(m.name)]
                for m in cand[:1]:
                    fn = m.name.split("/")[-1]
                    print("     MATCH %-58s %.2f GB" % (fn[:58], m.size / 1073741824))
                    if not APPLY:
                        continue
                    dest = stage / fn
                    if not z.extract(m, dest):
                        continue
                    try:
                        md5 = await asyncio.wait_for(get_hasher(sysc.hash_method).hash_file(str(dest)), timeout=3600)
                    except Exception as e:
                        print("       hash failed: %s" % str(e)[:70]); dest.unlink(missing_ok=True); continue
                    hit = await gate_lookup(get_es(), md5)
                    if not hit:
                        print("       no RA match (%s) — discarded" % md5); dest.unlink(missing_ok=True); continue
                    final = canon / fn
                    dest.replace(final)
                    game = _get_or_create_game(db, {"ra_game_id": hit.get("ra_game_id") or r.get("id"),
                                                    "title": hit.get("title"), "hash_name": hit.get("hash_name"),
                                                    "num_achievements": 0, "points": 0}, sysc.ra_console_id)
                    _upsert_rom(db, final, FOLDER, sysc.hash_method, md5, "matched",
                                hit.get("hash_name"), game.id, {"ra": hit.get("hash_name")}, None)
                    db.commit()
                    got += 1
                    print("       ONBOARDED -> %s" % hit.get("title"))
                    want.remove(r)
    finally:
        db.close(); await close_es()
    print("\nonboarded: %d  %s" % (got, "" if APPLY else "(DRY RUN — pass --apply)"))
asyncio.run(main())
