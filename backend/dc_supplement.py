"""
Dreamcast supplement sourcer: for games the built-in auto-sourcer couldn't hash
(gdi-in-zip disc source, DreamcastHasher can't hash .zip directly).

Per remaining RA game id:
  1. Pull RA game (md5 hash set) from ES.
  2. Find candidate .zip members in the sega_dreamcast archive.org item by title-key.
  3. Download zip -> staging, extract, locate .gdi/.cue/.iso track sheet.
  4. rahash 40 <sheet> ; verify md5 is in RA hash set.
  5. On match: chdman createcd -> .chd ; re-hash via DreamcastHasher to confirm ;
     move to /mnt/romfleet/roms/dreamcast/ ; onboard (_get_or_create_game + _upsert_rom).

Usage: python dc_supplement.py <id1> <id2> ...
"""
import asyncio, os, sys, subprocess, zipfile, shutil, glob, json
from pathlib import Path

# Resolve the backend package relative to THIS file, so the tool works under any
# install prefix rather than only /opt/romfleet.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.core.systems import get_system_by_folder
from app.hashers import get_hasher
from app.services.sourcing import _cookies, _item_index, _download, _title_key, ARCHIVE
from app.services.ingest import _get_or_create_game, _upsert_rom
from app.db.session import SessionLocal
from app.core.config import settings
from app.core.elasticsearch import init_es, close_es, get_es
from app.services.sourcing import _ra_game
from urllib.parse import quote

STAGING = Path("/mnt/romfleet/_staging/dc_supp")
FOLDER = "dreamcast"
REGION_PRIO = ["(USA)", "(World)", "(Europe)", "(UK)", "(Japan)"]  # preference order


def region_rank(name: str) -> int:
    for i, r in enumerate(REGION_PRIO):
        if r in name:
            return i
    return len(REGION_PRIO)


async def main(ids):
    sysc = get_system_by_folder(FOLDER)
    hasher = get_hasher(sysc.hash_method)
    cookies = _cookies()
    canon = Path(settings.canonical_path) / "roms" / FOLDER
    STAGING.mkdir(parents=True, exist_ok=True)
    idx = _item_index("sega_dreamcast", cookies)  # title_key -> [filenames]
    await init_es()
    db = SessionLocal()
    results = {"sourced": [], "no_candidate": [], "codec_or_hash_fail": []}
    try:
        for gid in ids:
            ra = await _ra_game(get_es(), gid)
            if not ra:
                results["no_candidate"].append((gid, "no RA game"))
                continue
            title = ra.get("title", str(gid))
            md5set = {(h.get("md5") or "").lower() for h in ra.get("hashes", []) if h.get("md5")}
            # gather candidate zip members: title-key of game + of each hash name
            keys = {_title_key(title)}
            for h in ra.get("hashes", []):
                keys.add(_title_key(h.get("name", "")))
            cands = []
            for k in keys:
                for fn in idx.get(k, []):
                    if fn.lower().endswith(".zip"):
                        cands.append(fn)
            cands = sorted(set(cands), key=region_rank)
            print(f"\n=== {gid} {title} :: {len(cands)} candidates ===", flush=True)
            matched = False
            for fn in cands:
                work = STAGING / f"g{gid}"
                if work.exists():
                    shutil.rmtree(work, ignore_errors=True)
                work.mkdir(parents=True)
                zpath = work / Path(fn).name
                url = f"{ARCHIVE}/download/sega_dreamcast/{quote(fn)}"
                print(f"  dl {fn}", flush=True)
                if not _download(url, cookies, zpath, timeout=1800):
                    print("    download failed", flush=True)
                    continue
                try:
                    with zipfile.ZipFile(zpath) as z:
                        z.extractall(work)
                except Exception as e:
                    print(f"    unzip failed {e}", flush=True)
                    continue
                zpath.unlink(missing_ok=True)
                # find sheet: prefer .gdi, then .cue, then .iso
                sheet = None
                for ext in (".gdi", ".cue", ".iso"):
                    hits = glob.glob(str(work / "**" / f"*{ext}"), recursive=True)
                    if hits:
                        sheet = hits[0]
                        break
                if not sheet:
                    print("    no gdi/cue/iso in zip", flush=True)
                    continue
                r = subprocess.run(["rahash", "40", sheet], capture_output=True, text=True, timeout=180)
                got = (r.stdout or "").strip()
                print(f"    rahash={got} match={got in md5set}", flush=True)
                if got not in md5set:
                    continue
                # build CHD for canonical storage
                base = Path(fn).name[:-4]  # strip .zip
                chd_tmp = work / f"{base}.chd"
                cc = subprocess.run(["chdman", "createcd", "-i", sheet, "-o", str(chd_tmp)],
                                    capture_output=True, text=True, timeout=3600)
                if cc.returncode != 0 or not chd_tmp.exists():
                    print(f"    createcd failed: {cc.stderr.strip()[:200]}", flush=True)
                    continue
                # confirm CHD hashes to same RA md5
                chd_hash = await hasher.hash_file(str(chd_tmp))
                if chd_hash != got:
                    print(f"    CHD re-hash mismatch {chd_hash} != {got}", flush=True)
                    continue
                dest = canon / f"{base}.chd"
                shutil.move(str(chd_tmp), str(dest))
                hit = {"ra_game_id": gid, "title": title,
                       "hash_name": base, "num_achievements": ra.get("num_achievements", 0),
                       "points": ra.get("points", 0)}
                game = _get_or_create_game(db, hit, sysc.ra_console_id)
                _upsert_rom(db, dest, FOLDER, sysc.hash_method, chd_hash, "matched",
                            base, game.id, {"ra": base}, None)
                db.commit()
                results["sourced"].append((gid, title, base))
                print(f"    ONBOARDED {dest.name}", flush=True)
                matched = True
                shutil.rmtree(work, ignore_errors=True)
                break
            if not matched:
                (results["no_candidate"] if not cands else results["codec_or_hash_fail"]).append((gid, title))
    finally:
        db.close()
        await close_es()
    print("\n==== SUMMARY ====", flush=True)
    print(json.dumps(results, indent=2), flush=True)
    return results


if __name__ == "__main__":
    ids = [int(x) for x in sys.argv[1:]]
    asyncio.get_event_loop().run_until_complete(main(ids))
