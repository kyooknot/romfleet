"""Patch drop-off: turn a manually-acquired ROM patch into an owned, RA-verified hack.

For "no-patch" games (hacks/translations RA supports but that aren't on RAPatches), drop the
patch — bare or zipped, in any common format (IPS / BPS / UPS / xdelta) — into the drop-off.
This identifies the target hack from the RA gate (by the patch's name), finds the base ROM it
applies to (owned, else sourced from No-Intro), applies the patch, verifies the output against
the hack's registered RA hash, and files it. Unmatched patches are left in place for review.

Disc-based xdelta (GameCube/Wii/3DS) needs the disc toolchain and is out of scope here — this
covers cart/handheld systems (raw / header-strip / NDS hashing) that we can build in-memory.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import os
import zipfile
from pathlib import Path

import structlog

from app.core.config import settings
from app.core.systems import get_system_by_console_id
from app.core.elasticsearch import get_es
from app.services.patching import apply_ips, apply_bps, _source_base_roms, _base_candidates

log = structlog.get_logger()

_PATCH_EXTS = (".ips", ".bps", ".ups", ".xdelta", ".xd", ".vcdiff")
_JUNK_EXT = (".txt", ".nfo", ".pdf", ".png", ".jpg", ".md", ".readme", ".diz")
# rom-name extension noise to strip from a hack's canonical file name
_EXT_STRIP = ("nes", "sfc", "smc", "fig", "z64", "n64", "v64", "nds", "gb", "gbc", "gba",
              "sms", "gg", "md", "gen", "32x", "bin", "pce", "ws", "wsc", "ngp", "vb", "a78")


def _fmt(data: bytes) -> str | None:
    if data[:5] == b"PATCH":
        return "ips"
    if data[:4] == b"BPS1":
        return "bps"
    if data[:4] == b"UPS1":
        return "ups"
    if data[:3] == b"\xd6\xc3\xc4":          # VCDIFF / xdelta3 magic
        return "xdelta"
    return None


def _extract_patches(path: Path):
    """Yield (label, patch_bytes, fmt) for every patch in a bare file or zip."""
    if path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(path) as z:
                for n in z.namelist():
                    base = n.split("/")[-1]
                    if n.endswith("/") or base.startswith(("._", "__")):
                        continue
                    if Path(base).suffix.lower() in _PATCH_EXTS:
                        data = z.read(n)
                        fmt = _fmt(data) or Path(base).suffix.lower().lstrip(".")
                        yield base, data, fmt
        except zipfile.BadZipFile:
            return
    elif path.suffix.lower() in _PATCH_EXTS:
        data = path.read_bytes()
        yield path.name, data, (_fmt(data) or path.suffix.lower().lstrip("."))


def apply_ups(source: bytes, patch: bytes) -> bytes:
    """Apply a UPS patch (XOR-diff format). Source CRC not enforced — the RA hash-verify is."""
    if patch[:4] != b"UPS1":
        raise ValueError("not a UPS patch")
    pos = 4

    def rd(pos):
        val, shift = 0, 1
        while True:
            b = patch[pos]; pos += 1
            val += (b & 0x7f) * shift
            if b & 0x80:
                break
            shift <<= 7
            val += shift
        return val, pos

    _src_size, pos = rd(pos)
    dst_size, pos = rd(pos)
    out = bytearray(source)
    if len(out) < dst_size:
        out.extend(b"\x00" * (dst_size - len(out)))
    out = out[:dst_size]
    opos = 0
    end = len(patch) - 12                     # 3 trailing CRC32 footers
    while pos < end:
        rel, pos = rd(pos)
        opos += rel
        while True:
            b = patch[pos]; pos += 1
            if b == 0:
                break
            if opos < len(out):
                out[opos] ^= b
            opos += 1
        opos += 1
    return bytes(out)


def _apply(fmt: str, base: bytes, patch: bytes) -> bytes:
    if fmt == "ips":
        return apply_ips(base, patch)
    if fmt == "bps":
        return apply_bps(base, patch)
    if fmt == "ups":
        return apply_ups(base, patch)
    if fmt == "xdelta":
        td = tempfile.mkdtemp(prefix="patchdrop_")
        bp, pp, op = Path(td) / "base", Path(td) / "p.xdelta", Path(td) / "out"
        bp.write_bytes(base); pp.write_bytes(patch)
        r = subprocess.run(["xdelta3", "-d", "-f", "-s", str(bp), str(pp), str(op)],
                           capture_output=True)
        if r.returncode != 0 or not op.exists():
            raise RuntimeError(f"xdelta3: {r.stderr.decode()[:120]}")
        return op.read_bytes()
    raise ValueError(f"unsupported patch format: {fmt}")


def _rom_bytes(path: Path, exts: tuple) -> bytes | None:
    """Inner ROM bytes from a base file (unzip if needed)."""
    if path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(path) as z:
                names = [n for n in z.namelist() if not n.endswith("/")
                         and Path(n).suffix.lower() not in _JUNK_EXT]
                pick = next((n for n in names if Path(n).suffix.lower() in exts), None) \
                    or (max(names, key=lambda n: z.getinfo(n).file_size) if names else None)
                return z.read(pick) if pick else None
        except zipfile.BadZipFile:
            return None
    return path.read_bytes()


def _clean_title(name: str) -> str:
    """Patch file name -> a searchable hack title (strip ext, version/format/credit tags)."""
    s = Path(name).stem
    s = re.sub(r"\.(ips|bps|ups|xdelta|xd|vcdiff)$", "", s, flags=re.I)
    s = re.sub(r"\b(v?\d+[\d.]*|rev\s*\w+|optimized|final|patched?)\b", " ", s, flags=re.I)
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", s)      # (Cutsman) / [v2]
    return re.sub(r"\s+", " ", s).strip()


async def _find_hack(title: str) -> dict | None:
    """Best-matching RA gate entry for a patch name, preferring hacks/translations with hashes."""
    if not title:
        return None
    r = await get_es().search(
        index=settings.es_index_games, size=8,
        query={"match": {"title": {"query": title, "operator": "or"}}},
        source=["title", "console_id", "hashes.md5", "hashes.name",
                "num_achievements", "points", "game_id"])
    hits = [h["_source"] for h in r["hits"]["hits"]]
    # prefer decorated hack/translation titles that actually carry supported hashes
    hits.sort(key=lambda s: (("~Hack~" in s["title"] or "~Translation~" in s["title"]),
                             bool(s.get("hashes"))), reverse=True)
    for s in hits:
        if s.get("hashes"):
            return s
    return None


def _safe_name(hash_name: str | None, title: str, gid) -> str:
    s = re.sub(r'[<>:"/\\|?*]', "", hash_name or title or f"hack_{gid}").strip()
    if "." in s and s.rsplit(".", 1)[-1].lower() in _EXT_STRIP:
        s = s.rsplit(".", 1)[0]
    return s or f"hack_{gid}"


async def _build(sysc, folder: str, patches: list, md5set: set, base_names: list) -> dict | None:
    """Apply each patch to owned then sourced bases; keep the output that reproduces an RA hash."""
    from app.hashers import get_hasher
    hasher = get_hasher(sysc.hash_method)
    exts = tuple(e.lower() for e in (sysc.extensions or [])) or (".bin",)
    canon = Path(settings.canonical_path) / "roms" / folder
    rom_ext = next((e for e in exts if e != ".zip"), ".bin")

    async def _try(base_path: Path) -> dict | None:
        base = _rom_bytes(base_path, exts)
        if not base:
            return None
        for label, patch, fmt in patches:
            # try the base as-is and a header-stripped variant (covers ips/bps authored either way)
            variants = [base]
            if base[:4] == b"NES\x1a":
                variants.append(base[16:])
            elif len(base) % 1024 == 512:
                variants.append(base[512:])
            for v in variants:
                try:
                    out = _apply(fmt, v, patch)
                except Exception:  # noqa  (wrong base / format — reject)
                    continue
                fd, tmp = tempfile.mkstemp(suffix=rom_ext)
                try:
                    os.write(fd, out); os.close(fd)
                    got = await hasher.hash_file(tmp)
                except Exception:  # noqa
                    got = None
                finally:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                if got and got in md5set:
                    return {"data": out, "md5": got, "fmt": fmt}
        return None

    base_exts = exts + (".zip",)          # ROMs are stored zipped in the canonical library
    owned = [p for p in canon.glob("*") if p.suffix.lower() in base_exts] if canon.is_dir() else []
    # RA often names a hack's hash after the HACK, not its base (e.g. N64 SM64 hacks), so
    # name-similarity alone can't find the base. Rank by base-name, then fall back to trying
    # every owned ROM — safe because xdelta fast-fails on the wrong source and byte-hashers
    # (raw/nes/snes/n64/…) are cheap. Slow hashers (nds/disc) only get the ranked shortlist
    # unless the patch is xdelta (which still fast-fails before we ever hash).
    _SLOW = {"nds", "psx_chd", "saturn_chd", "dreamcast_chd", "psp", "ps2", "gamecube", "wii",
             "pcenginecd_chd", "segacd_chd", "jaguarcd_raw", "neogeocd_chd", "3do", "pcfx"}
    ranked, seen = [], set()
    for bn in base_names:
        for p in _base_candidates(bn, owned, limit=12):
            if p not in seen:
                ranked.append(p); seen.add(p)
    rest = [p for p in owned if p not in seen]
    all_xdelta = {p[2] for p in patches} <= {"xdelta"}
    order = ranked + (rest if (all_xdelta or sysc.hash_method not in _SLOW) else [])

    # 1) owned bases
    for base in order:
        r = await _try(base)
        if r:
            r["sourced"] = False
            return r
    # 2) source the retail base from No-Intro (delete after)
    for bn in base_names:
        for tmp in _source_base_roms(folder, bn, base_exts, limit=4):
            try:
                r = await _try(tmp)
                if r:
                    r["sourced"] = True
                    return r
            finally:
                tmp.unlink(missing_ok=True)
    return None


async def process_patch_dropoff(dropoff: str | None = None) -> dict:
    """Identify + build every patch in the drop-off. Returns per-file results + affected systems."""
    dd = Path(dropoff) if dropoff else Path(settings.canonical_path) / "dropoff"
    if not dd.is_dir():
        return {"error": f"no dropoff dir at {dd}", "results": [], "systems": []}

    results, systems = [], set()
    for f in sorted(dd.iterdir()):
        if f.is_dir() or f.name.startswith("."):
            continue
        patches = list(_extract_patches(f))
        if not patches:
            continue                                  # not a patch — leave for the ROM drop-off
        hack = await _find_hack(_clean_title(patches[0][0]) or _clean_title(f.name))
        if not hack:
            results.append({"file": f.name, "status": "unidentified (no gate match)"})
            continue
        syscs = get_system_by_console_id(hack["console_id"])
        if not syscs:
            results.append({"file": f.name, "status": f"no system for console {hack['console_id']}",
                            "hack": hack["title"]})
            continue
        sysc = syscs[0]
        folder = sysc.folder
        md5set = {(h.get("md5") or "").lower() for h in hack.get("hashes", []) if h.get("md5")}
        base_names = [h["name"] for h in hack.get("hashes", []) if h.get("name")]
        built = await _build(sysc, folder, patches, md5set, base_names)
        if not built:
            results.append({"file": f.name, "status": "no base reproduced the RA hash",
                            "hack": hack["title"], "system": folder})
            continue
        name = _safe_name(next((h.get("name") for h in hack["hashes"]
                                if (h.get("md5") or "").lower() == built["md5"]), None),
                          hack["title"], hack.get("game_id"))
        rom_ext = next((e for e in (sysc.extensions or []) if e.lower() != ".zip"), ".bin")
        dest = Path(settings.canonical_path) / "roms" / folder / f"{name}.zip"
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"{name}{rom_ext}", built["data"])
        f.unlink()
        (f.with_name("._" + f.name)).unlink(missing_ok=True)
        systems.add(folder)
        results.append({"file": f.name, "status": "built", "system": folder,
                        "hack": hack["title"], "via": f"{built['fmt']}"
                        + (" + sourced base" if built["sourced"] else ""), "dest": dest.name})
        log.info("patch_dropoff.built", file=f.name, system=folder, hack=hack["title"],
                 fmt=built["fmt"], sourced=built["sourced"])
    return {"dropoff": str(dd), "results": results, "systems": sorted(systems)}
