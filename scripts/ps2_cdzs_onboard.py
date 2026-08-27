"""One-off onboarder for PS2 CD-frame CHDs (cdzs/cdlz codecs) now that
_rahash_chd_libchdr has a chd_extract_cd branch for them. Iterates local
/mnt/src/roms/ps2/*.chd files whose codec tag starts with 'cd', hashes each
with the normal PS2Hasher (console 21), and looks the resulting md5 up
directly in ra_games via a nested query (no title matching needed — the hash
is the ground truth). Matches are copied to canonical + upserted into
rom_files/games, same as the proven ps2_nightly.py onboarder.
"""
import asyncio, shutil, sys, os, glob, json
from pathlib import Path
from app.services.ingest import _get_or_create_game, _upsert_rom
from app.core.systems import get_system_by_folder
from app.core.elasticsearch import get_es, init_es, close_es
from app.hashers import get_hasher
from app.db.session import SessionLocal

FOLDER = "ps2"
SRC = Path(f"/mnt/src/roms/{FOLDER}")
CANON = Path(f"/mnt/romfleet/roms/{FOLDER}")

def log(*a):
    print(*a, flush=True)

def codec_tag(f: Path) -> str:
    try:
        with open(f, "rb") as fh:
            fh.seek(16)
            return fh.read(4).decode("ascii", "replace")
    except OSError:
        return ""

async def ra_lookup_by_hash(es, console_id: int, md5: str) -> dict | None:
    query = {
        "bool": {
            "must": [
                {"term": {"console_id": console_id}},
                {"nested": {"path": "hashes", "query": {"term": {"hashes.md5": md5}}}},
            ]
        }
    }
    r = await es.search(index="ra_games", size=1, query=query)
    hits = r["hits"]["hits"]
    if not hits:
        return None
    return hits[0]["_source"]

async def main():
    sysc = get_system_by_folder(FOLDER)
    hasher = get_hasher(sysc.hash_method)
    CANON.mkdir(parents=True, exist_ok=True)

    all_chd = sorted(SRC.glob("*.chd"))
    cd_frame = [f for f in all_chd if codec_tag(f).startswith("cd")]
    log(f"=== [{FOLDER}] cd-frame CHD candidates: {len(cd_frame)} (of {len(all_chd)} total local .chd) ===")

    canon_names = {f.name for f in CANON.glob("*.chd")}
    todo = [f for f in cd_frame if f.name not in canon_names]
    log(f"already canonical: {len(cd_frame) - len(todo)}; to process: {len(todo)}")

    await init_es()
    es = get_es()
    db = SessionLocal()

    added, nohash, nomatch, errors = [], [], [], []
    for i, f in enumerate(todo, 1):
        codec = codec_tag(f)
        try:
            h = (await asyncio.wait_for(hasher.hash_file(str(f)), timeout=600)).lower()
        except Exception as e:
            log(f"  [{i}/{len(todo)}] HASHFAIL {codec} {f.name}: {e}")
            errors.append((f.name, codec, str(e)))
            continue
        if not h or len(h) != 32:
            log(f"  [{i}/{len(todo)}] BADHASH {codec} {f.name}: {h!r}")
            nohash.append((f.name, codec, h))
            continue

        ra = await ra_lookup_by_hash(es, sysc.ra_console_id, h)
        if not ra:
            log(f"  [{i}/{len(todo)}] NOMATCH {codec} {f.name} hash={h}")
            nomatch.append((f.name, codec, h))
            continue

        # find the display name for this exact md5 within the RA hash list
        disp = ""
        for x in ra.get("hashes", []):
            if (x.get("md5") or "").lower() == h:
                disp = x.get("name") or ""
                break

        dest = CANON / f.name
        if not dest.exists():
            shutil.copy2(f, dest)

        hit = {
            "ra_game_id": ra["game_id"],
            "title": ra.get("title", f.stem),
            "num_achievements": ra.get("num_achievements", 0),
            "points": ra.get("points", 0),
        }
        game = _get_or_create_game(db, hit, sysc.ra_console_id)
        _upsert_rom(db, dest, FOLDER, sysc.hash_method, h, "matched", disp or f.name, game.id, {"ra": disp}, None)
        db.commit()
        added.append((f.name, codec, h, ra.get("title")))
        log(f"  [{i}/{len(todo)}] MATCH {codec} {f.name} -> {ra.get('title')} ({h})")

        if i % 15 == 0:
            log(f"PROGRESS {i}/{len(todo)} added={len(added)} nomatch={len(nomatch)} errors={len(errors)}")

    log("=== DONE ===")
    log(f"added={len(added)} nomatch={len(nomatch)} nohash={len(nohash)} errors={len(errors)}")
    log("--- added ---")
    for n, c, h, t in added:
        log(f"  {c:5s} {h} {t}  <- {n}")
    log("--- nomatch (hashed OK, no RA hash match) ---")
    for n, c, h in nomatch:
        log(f"  {c:5s} {h} {n}")
    log("--- errors ---")
    for n, c, e in errors:
        log(f"  {c:5s} {n}: {e}")

    db.close()
    await close_es()

asyncio.run(main())
