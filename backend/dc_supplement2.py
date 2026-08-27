"""
Dreamcast supplement sourcer v2 — multi-disc + region-lock aware.

Per RA game id:
  - Find candidate .zip members in sega_dreamcast item by title-key.
  - Order: region preference, then "clean" (penalize demo/beta/proto/collector/etc), then disc.
  - Download best candidate per disc-slot; extract; rahash 40; verify md5 in RA set.
  - Region-lock after first successful onboard (1G1R). Fill each disc-slot once.
  - On verify: chdman createcd -> .chd (canonical fmt); confirm re-hash; move + onboard.

Usage: python dc_supplement2.py <id1> <id2> ...
"""
import asyncio, os, re, sys, subprocess, zipfile, shutil, glob, json
from pathlib import Path

# Resolve the backend package relative to THIS file, so the tool works under any
# install prefix rather than only /opt/romfleet.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.core.systems import get_system_by_folder
from app.hashers import get_hasher
from app.services.sourcing import _cookies, _item_index, _download, _title_key, ARCHIVE, _ra_game
from app.services.ingest import _get_or_create_game, _upsert_rom
from app.db.session import SessionLocal
from app.core.config import settings
from app.core.elasticsearch import init_es, close_es, get_es
from urllib.parse import quote

STAGING = Path("/mnt/romfleet/_staging/dc_supp")
FOLDER = "dreamcast"
REGION_PRIO = ["(USA", "(World", "(Europe", "(UK", "(Japan"]
# penalize non-primary editions/builds in ordering (still eligible if nothing else verifies)
BAD_TOKENS = ["demo", "beta", "proto", "trial", "sample", "taikenban", "tentou",
              "movie", "kiosk", "collector", "genteiban", "press", "review", "(alt"]


def region_rank(name: str) -> int:
    for i, r in enumerate(REGION_PRIO):
        if r in name:
            return i
    return len(REGION_PRIO)


def clean_penalty(name: str) -> int:
    low = name.lower()
    return sum(1 for t in BAD_TOKENS if t in low)


def disc_num(name: str) -> int:
    m = re.search(r"\(Disc\s*(\d+)\)", name, re.I)
    return int(m.group(1)) if m else 1


def region_tag(name: str) -> str:
    for r in REGION_PRIO:
        if r in name:
            return r
    return "?"


async def main(ids):
    sysc = get_system_by_folder(FOLDER)
    hasher = get_hasher(sysc.hash_method)
    cookies = _cookies()
    canon = Path(settings.canonical_path) / "roms" / FOLDER
    STAGING.mkdir(parents=True, exist_ok=True)
    idx = _item_index("sega_dreamcast", cookies)
    await init_es()
    db = SessionLocal()
    results = {"sourced": [], "no_candidate": [], "hash_fail": []}
    try:
        for gid in ids:
            ra = await _ra_game(get_es(), gid)
            if not ra:
                results["no_candidate"].append([gid, "no RA game"]); continue
            title = ra.get("title", str(gid))
            md5set = {(h.get("md5") or "").lower() for h in ra.get("hashes", []) if h.get("md5")}
            keys = {_title_key(title)}
            for h in ra.get("hashes", []):
                keys.add(_title_key(h.get("name", "")))
            cands = set()
            for k in keys:
                for fn in idx.get(k, []):
                    if fn.lower().endswith(".zip"):
                        cands.add(fn)
            cands = sorted(cands, key=lambda fn: (region_rank(fn), clean_penalty(fn), disc_num(fn), fn))
            print("\n=== %d %s :: %d candidates ===" % (gid, title, len(cands)), flush=True)
            for c in cands:
                print("     cand:", c, flush=True)
            locked_region = None
            filled_discs = set()
            consumed_md5 = set()
            onboarded = []
            for fn in cands:
                dn = disc_num(fn)
                rt = region_tag(fn)
                if locked_region and rt != locked_region:
                    continue
                if dn in filled_discs:
                    continue
                work = STAGING / ("g%d" % gid)
                shutil.rmtree(work, ignore_errors=True); work.mkdir(parents=True)
                zpath = work / Path(fn).name
                url = "%s/download/sega_dreamcast/%s" % (ARCHIVE, quote(fn))
                print("  dl %s" % fn, flush=True)
                if not _download(url, cookies, zpath, timeout=2400):
                    print("    download failed", flush=True); continue
                try:
                    with zipfile.ZipFile(zpath) as z: z.extractall(work)
                except Exception as e:
                    print("    unzip failed %s" % e, flush=True); continue
                zpath.unlink(missing_ok=True)
                sheet = None
                for ext in (".gdi", ".cue", ".iso"):
                    hits = sorted(glob.glob(str(work / "**" / ("*" + ext)), recursive=True))
                    if hits: sheet = hits[0]; break
                if not sheet:
                    print("    no gdi/cue/iso", flush=True); continue
                r = subprocess.run(["rahash", "40", sheet], capture_output=True, text=True, timeout=300)
                got = (r.stdout or "").strip()
                ok = got in md5set and got not in consumed_md5
                print("    rahash=%s inRA=%s" % (got, got in md5set), flush=True)
                if not ok:
                    shutil.rmtree(work, ignore_errors=True); continue
                base = Path(fn).name[:-4]
                chd_tmp = work / (base + ".chd")
                cc = subprocess.run(["chdman", "createcd", "-i", sheet, "-o", str(chd_tmp)],
                                    capture_output=True, text=True, timeout=5400)
                if cc.returncode != 0 or not chd_tmp.exists():
                    print("    createcd FAIL: %s" % cc.stderr.strip()[:200], flush=True)
                    shutil.rmtree(work, ignore_errors=True); continue
                chd_hash = await hasher.hash_file(str(chd_tmp))
                if chd_hash != got:
                    print("    CHD rehash mismatch %s!=%s" % (chd_hash, got), flush=True)
                    shutil.rmtree(work, ignore_errors=True); continue
                dest = canon / (base + ".chd")
                shutil.move(str(chd_tmp), str(dest))
                hit = {"ra_game_id": gid, "title": title, "hash_name": base,
                       "num_achievements": ra.get("num_achievements", 0), "points": ra.get("points", 0)}
                game = _get_or_create_game(db, hit, sysc.ra_console_id)
                _upsert_rom(db, dest, FOLDER, sysc.hash_method, chd_hash, "matched",
                            base, game.id, {"ra": base}, None)
                db.commit()
                onboarded.append(base)
                consumed_md5.add(got); filled_discs.add(dn); locked_region = rt
                print("    ONBOARDED %s.chd" % base, flush=True)
                shutil.rmtree(work, ignore_errors=True)
            if onboarded:
                results["sourced"].append([gid, title, onboarded])
            elif not cands:
                results["no_candidate"].append([gid, title])
            else:
                results["hash_fail"].append([gid, title])
    finally:
        db.close(); await close_es()
        shutil.rmtree(STAGING, ignore_errors=True)
    print("\n==== SUMMARY ====", flush=True)
    print(json.dumps(results, indent=2), flush=True)


if __name__ == "__main__":
    ids = [int(x) for x in sys.argv[1:]]
    asyncio.get_event_loop().run_until_complete(main(ids))
