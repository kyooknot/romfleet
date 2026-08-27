"""Drop-off processor: identify manually-acquired ROMs by hash and file them into the
right canonical system folder. For the long tail RomFleet can't auto-source (homebrew,
translations whose base isn't in No-Intro, oddball dumps) — drop the ROM (bare, zipped,
or in a .rar/.7z) into <canonical>/dropoff and this hashes it across every RA header-
handling method, matches it against the gate, files the match as a clean canonical zip,
and removes it from the drop-off. Unmatched files are left in place for review.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

import structlog

from app.core.config import settings
from app.core.systems import get_system_by_console_id, get_system_by_folder
from app.core.elasticsearch import get_es

log = structlog.get_logger()

_JUNK_EXT = (".txt", ".nfo", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".xml", ".md",
             ".sqlite", ".torrent", ".ds_store", ".sav", ".cht")
_ARCHIVE_EXT = (".rar", ".7z")


def _md5(b: bytes) -> str:
    return hashlib.md5(b, usedforsecurity=False).hexdigest()


def _rom_payloads(path: Path):
    """Yield (member_name, bytes) ROM payloads from a bare file, a zip, or a .rar/.7z
    (extracted via 7z, nested zips recursed), skipping docs/art and macOS sidecar cruft."""
    ext = path.suffix.lower()
    if ext == ".zip":
        try:
            with zipfile.ZipFile(path) as z:
                for n in z.namelist():
                    base = n.split("/")[-1]
                    if n.endswith("/") or base.startswith(("._", "__")) \
                            or Path(base).suffix.lower() in _JUNK_EXT:
                        continue
                    yield n, z.read(n)
        except zipfile.BadZipFile:
            return
    elif ext in _ARCHIVE_EXT:
        tmpd = tempfile.mkdtemp(prefix="dropoff_")
        r = subprocess.run(["7z", "x", "-y", "-o" + tmpd, str(path)], capture_output=True)
        if r.returncode != 0:
            log.warning("dropoff.unpack_failed", file=path.name, err=r.stderr.decode()[:150])
            return
        for p in sorted(Path(tmpd).rglob("*")):
            if not p.is_file() or p.name.startswith(("._", "__")) \
                    or p.suffix.lower() in _JUNK_EXT:
                continue
            if p.suffix.lower() == ".zip":
                yield from _rom_payloads(p)          # nested zip inside the archive
            else:
                yield p.name, p.read_bytes()
    elif ext not in _JUNK_EXT:
        yield path.name, path.read_bytes()


def _byte_candidates(member: str, data: bytes) -> dict[str, tuple[str, bytes]]:
    """method -> (md5, payload-to-store-if-the-target-system-is-raw-hashed). Covers every RA
    header-handling method computable from bytes alone: raw cart, NES iNES strip, SNES copier
    strip, Atari Lynx LNX strip, Atari 7800 .a78 header strip. (NDS is added async elsewhere.)"""
    ext = Path(member).suffix.lower()
    out: dict[str, tuple[str, bytes]] = {"raw": (_md5(data), data)}
    if data[:4] == b"NES\x1a":                                   # iNES 16-byte header
        out["nes"] = (_md5(data[16:]), data[16:])
    if len(data) % 1024 == 512:                                  # SNES 512-byte copier header
        out["snes"] = (_md5(data[512:]), data[512:])
    if data[:4] == b"LYNX":                                      # Lynx 64-byte LNX header
        out["lynx"] = (_md5(data[64:]), data[64:])
    if data[1:10] == b"ATARI7800" or (ext == ".a78" and len(data) % 1024 == 128):
        out["a78"] = (_md5(data[128:]), data[128:])              # Atari 7800 128-byte header
    return out


async def _nds_hash(data: bytes) -> str | None:
    """RA Nintendo DS hash (rahash) for a raw .nds payload; None if it can't be hashed."""
    from app.hashers import get_hasher
    fd, tmp = tempfile.mkstemp(suffix=".nds")
    try:
        os.write(fd, data)
        os.close(fd)
        return await get_hasher("nds").hash_file(tmp)
    except Exception as e:  # noqa  (homebrew .nds without a proper header can't be hashed)
        log.debug("dropoff.nds_hash_failed", err=str(e)[:120])
        return None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


async def _match(md5: str):
    r = await get_es().search(
        index=settings.es_index_games, size=1,
        query={"nested": {"path": "hashes", "query": {"term": {"hashes.md5": md5}}}},
        source=["game_id", "title", "console_id", "hashes.md5", "hashes.name"])
    hits = r["hits"]["hits"]
    if not hits:
        return None
    s = hits[0]["_source"]
    hname = next((h.get("name") for h in s.get("hashes", [])
                  if (h.get("md5") or "").lower() == md5), None)
    return {"console": s["console_id"], "game": s["game_id"], "title": s["title"],
            "hash_name": hname}


async def _identify(member: str, data: bytes):
    """Try every content-derived hash candidate against the gate. Returns the match plus the
    method and the exact bytes that produced it (so raw-hashed systems store the right variant)."""
    cands = _byte_candidates(member, data)
    if Path(member).suffix.lower() == ".nds":
        h = await _nds_hash(data)
        if h:
            cands["nds"] = (h, data)
    for method, (md5, variant) in cands.items():
        m = await _match(md5)
        if m:
            return {**m, "method": method, "variant": variant}
    return None


async def process_dropoff(dropoff: str | None = None) -> dict:
    """Identify + file every ROM in the drop-off dir. Returns per-file results and the set
    of systems that gained files (the caller re-ingests those)."""
    dd = Path(dropoff) if dropoff else Path(settings.canonical_path) / "dropoff"
    if not dd.is_dir():
        return {"error": f"no dropoff dir at {dd}", "results": [], "systems": []}

    results, systems = [], set()
    for f in sorted(dd.iterdir()):
        if f.is_dir() or f.name.startswith(".") or f.suffix.lower() in _JUNK_EXT:
            continue
        hit = orig = None
        for member, data in _rom_payloads(f):
            m = await _identify(member, data)
            if m:
                hit, orig = {**m, "member": member}, data
                break
        if not hit:
            results.append({"file": f.name, "status": "unmatched"})
            continue

        syscs = get_system_by_console_id(hit["console"])
        if not syscs:
            results.append({"file": f.name, "status": f"no system for console {hit['console']}",
                            "game": hit["title"]})
            continue
        # shared-console (e.g. 8=pcengine/supergrafx, 3=snes/satellaview): first is the
        # base folder; a hash-library split can refine later.
        folder = syscs[0].folder
        sysc = get_system_by_folder(folder)
        # A raw-hashed system re-hashes the stored file as-is, so store the exact variant that
        # matched (e.g. a header-stripped .a78). A strip/special system (nes/snes/lynx/nds)
        # re-strips at scan time, so store the ORIGINAL member untouched.
        store = hit["variant"] if (sysc and sysc.hash_method == "raw") else orig

        rom_ext = Path(hit["member"]).suffix or ".bin"
        name = re.sub(r'[<>:"/\\|?*]', "", hit["hash_name"] or hit["title"]).strip()
        if name.lower().endswith(rom_ext.lower()):
            name = name[: -len(rom_ext)]
        dest = Path(settings.canonical_path) / "roms" / folder / f"{name}.zip"
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"{name}{rom_ext}", store)
        f.unlink()
        (f.with_name("._" + f.name)).unlink(missing_ok=True)   # macOS sidecar
        systems.add(folder)
        results.append({"file": f.name, "status": "filed", "system": folder,
                        "game": hit["title"], "via": f"{hit['method']} hash", "dest": dest.name})
        log.info("dropoff.filed", file=f.name, system=folder, game=hit["title"], via=hit["method"])
    return {"dropoff": str(dd), "results": results, "systems": sorted(systems)}
