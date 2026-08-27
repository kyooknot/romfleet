"""Auto-patch missing RA hacks: owned base disc + RAPatches .xdelta -> hacked ROM, verified.

RAPatches stores GameCube hacks at GameCube/Hacks/<BaseGame>/<RA_game_id>-<name>.zip (an
.xdelta + readme). We locate the patch by RA game id, decompress the OWNED base disc to ISO,
apply the xdelta, and keep the result only if rahash reproduces the RA hash — same trust gate
as sourcing. Verified output is recompressed to RVZ into the canonical library.
"""
from __future__ import annotations
import difflib
import json
import os
import re
import shutil
import subprocess
import hashlib
import io
import struct
import time
import zipfile
import zlib
from pathlib import Path
from urllib.parse import quote, unquote
from app.core.config import settings
from app.hashers import get_hasher
from app.services.ingest import _get_or_create_game, _upsert_rom
import structlog

log = structlog.get_logger()


def _bin(name: str, *extra: str) -> str:
    """Resolve an external tool to an ABSOLUTE path.

    romfleet-api runs under systemd with PATH=/usr/local/sbin:/usr/local/bin:... so bare names
    resolve there — but anything invoked outside systemd (cron, a manual run, another service)
    gets the leaner /sbin:/bin:/usr/sbin:/usr/bin and every locally-built tool in
    /usr/local/bin (rahash, chd_extract, xdelta3n) silently vanishes with ENOENT. DOLPHIN was
    already hardcoded absolute for this reason; do it for the rest too.
    """
    found = shutil.which(name)
    if found:
        return found
    for cand in (*extra, f"/usr/local/bin/{name}", f"/usr/bin/{name}", f"/usr/games/{name}"):
        if Path(cand).is_file():
            return cand
    return name          # let it fail loudly at call time rather than guess


DOLPHIN = _bin("dolphin-tool", "/usr/games/dolphin-tool")
RAHASH = _bin("rahash")
# Prefer xdelta3n (3.1.0) over the distro xdelta3 (3.0.11). Disc patches are routinely built
# with a source window larger than 3.0.11 will allocate: it caps -B at 2GiB and then still
# fails a 4.5GB source with "hard window size exceeded: XD3_INVALID_INPUT", whose own hint
# ("normally this indicates that the source file is incorrect") is actively misleading — the
# source was byte-verified against the patch readme's MD5 and was correct. 3.1.0 decodes it.
XDELTA3 = _bin("xdelta3n", "/usr/local/bin/xdelta3n") if Path("/usr/local/bin/xdelta3n").is_file() \
    else _bin("xdelta3")
SEVENZ = _bin("7z")
CHDMAN = _bin("chdman")
# xdelta3 -B ceiling; 3.1.0 rejects anything above this outright.
_XD3_MAX_SRCWIN = 2147483648


def _xd3_decode(src: Path, patch: Path, out: Path, timeout: int = 5400):
    """xdelta3 decode with the source window sized to the source file.

    Without -B the decoder uses a 64MiB default and rejects any patch built with a bigger
    window, which is most disc-sized patches. -B is clamped to the binary's 2GiB ceiling —
    xdelta3n still decodes >2GiB sources with that, which 3.0.11 does not.
    """
    try:
        winsz = min(src.stat().st_size, _XD3_MAX_SRCWIN)
    except OSError:
        winsz = _XD3_MAX_SRCWIN
    return subprocess.run([XDELTA3, "-d", "-f", "-B", str(winsz), "-s", str(src),
                           str(patch), str(out)], capture_output=True, text=True, timeout=timeout)


# repo root — the per-system patch path (System/<Hacks|Translation>/.../id-name.zip) is appended.
RAP_RAW = "https://raw.githubusercontent.com/RetroAchievements/RAPatches/main"
RAP_API = "https://api.github.com/repos/RetroAchievements/RAPatches"
_TMP = Path("/rahash_tmp")
# Disc patch work is staged on the POOL, never the small /tmp/_TMP (disc images are GB-scale).
_STAGE = Path("/mnt/romfleet/_staging")
# RAPatches index disk cache — a rebuild is ~24 unauthenticated GitHub calls
# against a 60/hour limit, so it must survive process restarts.
_INDEX_CACHE = _STAGE / "rapatches_index.json"
_INDEX_TTL = 12 * 3600
_INDEX: dict | None = None
# ra_game_id -> why the last patch attempt stopped. patch_outlook consults this so a failure
# reports what actually happened instead of assuming "a patch exists and we failed, therefore
# the patch didn't reproduce the hash" — which is wrong whenever we never got a base disc to
# apply it to, and that is the common case.
_LAST_PATCH_FAIL: dict[str, str] = {}

# RA console id -> RAPatches top-level system-folder name. Each holds Hacks/ and/or
# Translation/ subtrees keyed by RA game id (structure verified against the live repo).
RAPATCHES_SYS = {
    43: "3DO", 40: "Dreamcast", 16: "GameCube", 19: "Wii", 39: "Saturn",
    21: "PS2", 12: "PlayStation", 41: "PlayStation Portable", 49: "PC-FX",
    9: "Sega CD", 76: "PC Engine CD", 56: "Neo Geo CD",
}
# GameCube/Wii patch to an ISO via dolphin-tool and store RVZ.
_DOLPHIN_CONSOLES = {16, 19}
# CD-based systems: extract via chdman extractcd, patch the data image, store CHD.
_CHDCD_CONSOLES = {43, 40, 39, 12, 9, 76, 56, 49}
# DVD-based systems: extract via chd_extract (chdman 0.251 can't read our zstd CHDs), patch,
# store ISO. PS2 + PSP were in RAPATCHES_SYS all along but matched NEITHER set above, so
# patch_game returned None for them and their patches were indexed but unusable.
_DVD_CONSOLES = {21, 41}

# Directory tokens that are RAPatches CATEGORIES or LANGUAGES, never a base-game name — so the
# base-game folder (used to match an owned base disc) is the nearest parent dir that is neither.
_RAP_CATEGORY = {"hacks", "translation", "translations", "fix", "improvement", "subset", "undub",
                 "widescreen", "cosmetic", "lfs", "other", "unsorted", "removed", "misc",
                 "saves", "utilities", "dlc"}
_RAP_LANG = {"english", "french", "german", "spanish", "italian", "portuguese", "russian",
             "hungarian", "indonesian", "korean", "multi", "swedish", "turkish", "ukrainian",
             "vietnamese", "japanese", "chinese", "dutch", "polish", "czech", "greek", "finnish",
             "norwegian", "danish", "arabic", "hebrew", "catalan", "brazilian", "latin", "thai"}

# Category dirs under a system that _index() must NOT descend into. Everything else is fair
# game: RA gives Hack/Improvement/Fix/Undub/Widescreen/Cosmetic/Other/LFS/Multipart releases
# their own game entries, and each patch zip is named "<ra_game_id>-<slug>", so they index
# exactly like Hacks/ and Translation/ do. Restricting to hacks|translation(s) missed ~170
# disc game ids — including ALL of Wii's, whose category dir is the SINGULAR "Hack".
# Indexing more categories is safe because the post-patch RA hash gate, not the directory
# name, decides whether a patched image is accepted.
# "subset" is excluded deliberately: a subset has no dump of its own (see ra_monitor).
_RAP_SKIP_DIRS = {"subset", "subsets", "removed", "saves", "utilities", "dlc"}
# RAPatches ships both .zip and .7z payloads; a .zip-only test dropped 74 reachable patches.
_RAP_ARCHIVE_EXT = (".zip", ".7z")


def _index() -> dict:
    """RA game_id -> {system, console, path, base, kind} for EVERY RAPatches disc patch, across
    all mapped systems and every patch category. Per-subtree recursive tree fetches dodge
    GitHub's truncation of the full-repo tree. `base` is the nearest parent dir that is not a
    category/language token (a real base-game folder, as GameCube/Hacks uses) or None
    (translations are foldered by language only, so the base is taken from the RA title).

    Backed by a disk cache, because a rebuild costs ~24 UNAUTHENTICATED GitHub API calls and
    the in-process cache dies with the process. Repeated runs exhaust the 60/hour limit, after
    which every call returns nothing and this used to hand back an EMPTY index — which
    patch_game cannot distinguish from "no patch exists", so all patching silently no-ops and
    every hack reports as "no hash-matching source found". Observed exactly that today.

    So: serve a fresh cache without touching the network; on a failed/empty rebuild fall back
    to a STALE cache rather than {}; and if there is no cache either, say so loudly instead of
    pretending the repo is empty.
    """
    global _INDEX
    if _INDEX is not None:
        return _INDEX

    now = time.time()
    cached, cached_age = None, None
    try:
        if _INDEX_CACHE.is_file():
            blob = json.loads(_INDEX_CACHE.read_text())
            cached, cached_age = blob.get("index"), now - blob.get("fetched_at", 0)
            if cached and cached_age < _INDEX_TTL:
                _INDEX = cached
                log.info("RAPatches index from cache", entries=len(cached),
                         age_hours=round(cached_age / 3600, 1))
                return _INDEX
    except Exception as e:  # noqa — a corrupt cache must never block a rebuild
        log.warning("RAPatches index cache unreadable", error=str(e)[:120])

    _INDEX = {}
    from app.services.sourcing import _get_json
    for cid, sysname in RAPATCHES_SYS.items():
        try:
            top = _get_json(f"{RAP_API}/contents/{quote(sysname)}")
            if not isinstance(top, list):
                continue
            for sub in top:
                if sub.get("type") != "dir" or sub.get("name", "").lower() in _RAP_SKIP_DIRS:
                    continue
                subname = sub["name"]
                tree = _get_json(f"{RAP_API}/git/trees/{sub['sha']}?recursive=1")
                for t in (tree or {}).get("tree", []):
                    p = t.get("path", "")               # relative to the subtree root
                    if not p.lower().endswith(_RAP_ARCHIVE_EXT):
                        continue
                    gid = p.rsplit("/", 1)[-1].split("-", 1)[0]
                    if not gid.isdigit():
                        continue
                    base = None
                    for d in reversed(p.split("/")[:-1]):
                        if d.lower() in _RAP_LANG or d.lower() in _RAP_CATEGORY:
                            continue
                        base = d
                        break
                    _INDEX.setdefault(gid, {"system": sysname, "console": cid,
                                            "path": f"{sysname}/{subname}/{p}",
                                            "base": base, "kind": subname})
        except Exception as e:  # noqa
            log.warning("RAPatches index failed", system=sysname, error=str(e)[:150])

    if _INDEX:
        try:
            _INDEX_CACHE.parent.mkdir(parents=True, exist_ok=True)
            _INDEX_CACHE.write_text(json.dumps({"fetched_at": now, "index": _INDEX}))
        except Exception as e:  # noqa
            log.warning("could not write RAPatches index cache", error=str(e)[:120])
        return _INDEX

    # Rebuild produced nothing — almost always the GitHub 60/hour limit. Prefer stale over
    # empty: an empty index makes every patchable game look unpatchable.
    if cached:
        log.warning("RAPatches index rebuild EMPTY (GitHub rate limit?) — using STALE cache",
                    entries=len(cached), age_hours=round((cached_age or 0) / 3600, 1))
        _INDEX = cached
    else:
        log.error("RAPatches index EMPTY and no cache — patching is disabled this run, "
                  "every hack will report as unsourceable (check GitHub rate limit)")
    return _INDEX


def _owned_discs(folder: str) -> list[Path]:
    d = Path(settings.canonical_path) / "roms" / folder
    return [p for p in d.glob("*") if p.suffix.lower() in (".rvz", ".iso", ".gcm")] if d.is_dir() else []


# A hack/patch applies to the CLEAN RETAIL dump — penalise files that are themselves hacks,
# MSU-1 audio packs, translations, or versioned dumps so retail ranks first.
_BASE_NOISE = re.compile(r"MSU-?1|\bHack\b|\bAftermarket\b|\(v\d|\bEn\b|\bFr\b|\bEs\b|"
                         r"\bDe\b|\bIt\b|translat|\bUnl\b|\(v[\d]", re.I)


def _base_candidates(base_name: str, discs: list[Path], limit: int = 3) -> list[Path]:
    """Match owned ROMs to a hack's base game by franchise prefix, ranked so the CLEAN RETAIL
    dump wins over MSU-1/hack/translation variants. Wrong bases are rejected downstream by the
    hash check, so returning a few extra candidates is cheap for cart patches."""
    from app.services.sourcing import _title_key
    bk = _title_key(base_name)
    fr = " ".join(bk.split()[:2])                       # e.g. "SPONGEBOB SQUAREPANTS"
    pool = [p for p in discs if fr and fr in _title_key(p.name)] or list(discs)

    def score(p: Path) -> float:
        ratio = difflib.SequenceMatcher(None, bk, _title_key(p.name)).ratio()
        return ratio + (0.0 if _BASE_NOISE.search(p.name) else 0.25)   # clean-retail bonus

    scored = sorted(((score(p), p) for p in pool), key=lambda x: x[0], reverse=True)
    return [p for _, p in scored[:limit]]


def apply_ips(base: bytes, patch: bytes) -> bytes:
    """Apply an IPS patch to `base`. IPS = 'PATCH' + records (3-byte offset, 2-byte size; size 0
    means an RLE run of 2-byte length + 1 byte) + 'EOF'. Patches may extend the file."""
    if patch[:5] != b"PATCH":
        raise ValueError("not an IPS patch")
    out = bytearray(base)
    i = 5
    while patch[i:i + 3] != b"EOF":
        off = int.from_bytes(patch[i:i + 3], "big"); i += 3
        size = int.from_bytes(patch[i:i + 2], "big"); i += 2
        if size == 0:                                  # RLE run
            run = int.from_bytes(patch[i:i + 2], "big"); i += 2
            chunk = patch[i:i + 1] * run; i += 1
        else:
            chunk = patch[i:i + size]; i += size
        end = off + len(chunk)
        if end > len(out):
            out.extend(b"\x00" * (end - len(out)))
        out[off:end] = chunk
    return bytes(out)


def _nes_bytes(path: Path) -> bytes:
    """Raw .nes bytes from a .nes or .zip file."""
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            n = next((x for x in z.namelist() if x.lower().endswith(".nes")), None)
            if not n:
                raise ValueError("no .nes in zip")
            return z.read(n)
    return path.read_bytes()


def _nes_hash(data: bytes) -> str:
    """RA NES hash: strip the 16-byte iNES header, MD5 the PRG+CHR body."""
    if data[:4] == b"NES\x1a":
        data = data[16:]
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _owned_nes(folder: str) -> list[Path]:
    d = Path(settings.canonical_path) / "roms" / folder
    return [p for p in d.glob("*") if p.suffix.lower() in (".nes", ".zip")] if d.is_dir() else []


def _patch_nes(db, sysc, folder: str, g: dict, ra: dict) -> str | None:
    return _patch_cart(db, sysc, folder, g, ra, base_exts=(".nes", ".zip"),
                       bytes_fn=_nes_bytes, hash_fn=_nes_hash, out_ext=".nes")


def _bps_varint(data: bytes, pos: int):
    result, shift = 0, 1
    while True:
        b = data[pos]; pos += 1
        result += (b & 0x7f) * shift
        if b & 0x80:
            break
        shift <<= 7
        result += shift
    return result, pos


def apply_bps(source: bytes, patch: bytes) -> bytes:
    """Apply a BPS patch. Source CRC is NOT enforced — the final RA hash-verify is the real gate,
    so header variants of the base don't break us."""
    if patch[:4] != b"BPS1":
        raise ValueError("not a BPS patch")
    pos = 4
    _src_size, pos = _bps_varint(patch, pos)
    tgt_size, pos = _bps_varint(patch, pos)
    meta_size, pos = _bps_varint(patch, pos)
    pos += meta_size
    out = bytearray(tgt_size)
    out_pos = src_rel = tgt_rel = 0
    end = len(patch) - 12                               # 3 trailing CRC32 footers
    while pos < end:
        data, pos = _bps_varint(patch, pos)
        action, length = data & 3, (data >> 2) + 1
        if action == 0:                                 # SourceRead
            out[out_pos:out_pos + length] = source[out_pos:out_pos + length]
            out_pos += length
        elif action == 1:                               # TargetRead
            out[out_pos:out_pos + length] = patch[pos:pos + length]
            pos += length; out_pos += length
        elif action == 2:                               # SourceCopy
            off, pos = _bps_varint(patch, pos)
            src_rel += -(off >> 1) if off & 1 else (off >> 1)
            out[out_pos:out_pos + length] = source[src_rel:src_rel + length]
            out_pos += length; src_rel += length
        else:                                           # TargetCopy (may overlap — byte loop)
            off, pos = _bps_varint(patch, pos)
            tgt_rel += -(off >> 1) if off & 1 else (off >> 1)
            for _ in range(length):
                out[out_pos] = out[tgt_rel]; out_pos += 1; tgt_rel += 1
    return bytes(out)


def _apply_patch(base: bytes, patch: bytes) -> bytes:
    if patch[:5] == b"PATCH":
        return apply_ips(base, patch)
    if patch[:4] == b"BPS1":
        return apply_bps(base, patch)
    raise ValueError("unsupported patch format (not IPS/BPS)")


def _snes_bytes(path: Path) -> bytes:
    """Headerless .sfc bytes from a .sfc/.smc/.zip (strip the 512-byte copier header if present)."""
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            n = next((x for x in z.namelist()
                      if x.lower().endswith((".sfc", ".smc", ".fig"))), None)
            if not n:
                raise ValueError("no SNES rom in zip")
            data = z.read(n)
    else:
        data = path.read_bytes()
    if len(data) % 1024 == 512:
        data = data[512:]
    return data


def _snes_hash(data: bytes) -> str:
    if len(data) % 1024 == 512:
        data = data[512:]
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _owned_snes(folder: str) -> list[Path]:
    d = Path(settings.canonical_path) / "roms" / folder
    exts = (".sfc", ".smc", ".fig", ".zip")
    return [p for p in d.glob("*") if p.suffix.lower() in exts] if d.is_dir() else []


def _patch_snes(db, sysc, folder: str, g: dict, ra: dict) -> str | None:
    return _patch_cart(db, sysc, folder, g, ra, base_exts=(".sfc", ".smc", ".fig", ".zip"),
                       bytes_fn=_snes_bytes, hash_fn=_snes_hash, out_ext=".sfc")


def _to_z64(data: bytes) -> bytes:
    """Normalise an N64 ROM to big-endian .z64 (what RAPatches patches target)."""
    m = data[:4]
    if m == b"\x37\x80\x40\x12":                        # .v64 byteswapped → swap byte pairs
        a = bytearray(data)
        a[0::2], a[1::2] = data[1::2], data[0::2]
        return bytes(a)
    if m == b"\x40\x12\x37\x80":                        # .n64 little-endian → reverse each word
        a = bytearray(len(data))
        for i in range(0, len(data) - 3, 4):
            a[i:i + 4] = data[i:i + 4][::-1]
        return bytes(a)
    return data                                         # already .z64 (or unknown — try as-is)


def _n64_bytes(path: Path) -> bytes:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            ndd = next((x for x in names if x.lower().endswith(".ndd")), None)
            if ndd:
                return z.read(ndd)                # 64DD disk image — hashed as-is, no z64 normalize
            n = next((x for x in names if x.lower().endswith((".z64", ".n64", ".v64"))), None)
            if not n:
                raise ValueError("no n64 rom in zip")
            data = z.read(n)
    else:
        data = path.read_bytes()
        if path.suffix.lower() == ".ndd":
            return data
    return _to_z64(data)


def _n64_hash(data: bytes) -> str:
    """rahash console 2 — rcheevos normalises N64 (incl. padding expanded hacks) before MD5."""
    import subprocess as _sp, os as _os, tempfile as _t
    _TMP.mkdir(parents=True, exist_ok=True)
    fd, tf = _t.mkstemp(suffix=".z64", dir=str(_TMP))
    try:
        with _os.fdopen(fd, "wb") as f:
            f.write(data)
        r = _sp.run([RAHASH, "2", tf], capture_output=True, text=True, timeout=120)
        return r.stdout.strip() if r.returncode == 0 else ""
    finally:
        _os.unlink(tf)


def _owned_n64(folder: str) -> list[Path]:
    d = Path(settings.canonical_path) / "roms" / folder
    exts = (".z64", ".n64", ".v64", ".zip")
    return [p for p in d.glob("*") if p.suffix.lower() in exts] if d.is_dir() else []


def _patch_n64(db, sysc, folder: str, g: dict, ra: dict) -> str | None:
    # System has no .extensions column (it lives in systems.yaml), so this raised
    # AttributeError on EVERY n64 hack patch — the exception surfaced as a bare
    # not attempted yet with no reason recorded, hiding the breakage entirely.
    dd = folder == "n64dd" or ".ndd" in (getattr(sysc, "extensions", None) or [])
    return _patch_cart(db, sysc, folder, g, ra,
                       base_exts=(".ndd", ".zip") if dd else (".z64", ".n64", ".v64", ".zip"),
                       bytes_fn=_n64_bytes, hash_fn=_n64_hash, out_ext=".ndd" if dd else ".z64")


def _raw_hash(data: bytes) -> str:
    """Whole-file MD5 — the RA hash for every raw-cart system (Genesis, SMS, GG, 32X,
    SG-1000, WonderSwan, NGP/NGPC, Atari, GB/GBC/GBA, Lynx, ColecoVision, …)."""
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _raw_bytes_for(rom_exts: tuple):
    """bytes_fn for a raw-cart system: ROM bytes from a bare file or a .zip (member by
    extension, else the largest member). No header handling — raw systems hash whole-file."""
    def _fn(path: Path) -> bytes:
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as z:
                names = [n for n in z.namelist() if not n.endswith("/")]
                if not names:
                    raise ValueError("empty zip")
                n = next((x for x in names if x.lower().endswith(rom_exts)),
                         max(names, key=lambda x: z.getinfo(x).file_size))
                return z.read(n)
        return path.read_bytes()
    return _fn


def _nds_hash(data: bytes) -> str:
    """RA's NDS hash is NOT a whole-file MD5 — rcheevos normalises the ROM (header/secure-area
    handling), so it must come from rahash itself. rahash wants a path, so stage the bytes."""
    import tempfile as _tf
    fd, tmp = _tf.mkstemp(suffix=".nds")
    os.close(fd)
    try:
        Path(tmp).write_bytes(data)
        r = subprocess.run(["rahash", "18", tmp], capture_output=True, text=True, timeout=180)
        return (r.stdout or "").strip().lower() if r.returncode == 0 else ""
    finally:
        Path(tmp).unlink(missing_ok=True)


def _patch_nds(db, sysc, folder: str, g: dict, ra: dict) -> str | None:
    """NDS hacks via RA's own patch_url (IPS/BPS/xdelta in one zip).

    NDS reached no patcher at all before this: hash_method is "nds" so the raw-cart path did
    not claim it, and console 18 is not in RAPATCHES_SYS so the disc path did not either — the
    game fell through and reported "no patcher for nds". That was wrong. RA publishes a
    Compatibility Patch for these sets (Pokemon Blaze Black / Volt White ships .bps AND .xdelta
    in the same zip), so the cart path applies once it is told how to hash the result.
    """
    return _patch_cart(db, sysc, folder, g, ra, base_exts=(".nds", ".zip"),
                       bytes_fn=_raw_bytes_for((".nds",)), hash_fn=_nds_hash, out_ext=".nds")


def _patch_raw(db, sysc, folder: str, g: dict, ra: dict) -> str | None:
    """Generic raw-cart patch path — any system that RA hashes as whole-file MD5."""
    rom_exts = tuple(e.lower() for e in sysc.extensions if e.lower() != ".zip") or (".bin",)
    base_exts = tuple(e.lower() for e in sysc.extensions) or (".bin", ".zip")
    return _patch_cart(db, sysc, folder, g, ra, base_exts=base_exts,
                       bytes_fn=_raw_bytes_for(rom_exts), hash_fn=_raw_hash, out_ext=rom_exts[0])


def _source_base_roms(folder: str, base_name: str, exts: tuple, limit: int = 4):
    """Download clean-retail base ROMs matching base_name from the No-Intro `ni-roms` cart source
    to patch against when the base isn't owned locally. Yields temp Paths; the caller deletes them.
    Ranks by name similarity, a clean-retail bonus, and a region-tag bonus."""
    from app.services.sourcing import _sources_for, _member_index, _cookies, _norm, NIROMS, _download
    cookies = _cookies()
    if not cookies:
        return
    cart = next((s for s in _sources_for(folder) if s.get("kind") == "cart"), None)
    if not cart:
        return
    idx = _member_index(cart["zip"], cookies)
    bk = _norm(base_name)
    fr = " ".join(bk.split()[:2])
    members = set()
    for norm, names in idx.items():
        if (bk and bk in norm) or (fr and fr in norm):
            members.update(names)

    def score(n: str) -> float:
        nn = _norm(re.sub(r"\.\w{1,4}$", "", n))
        if bk and nn == bk:
            # An exact name match is the answer, not a contender. The bonuses below are worth
            # up to +0.55, which was enough for "Alex Kidd in Miracle World (USA, Europe)" to
            # outrank the exact "(USA, Europe, Brazil) (En) (Rev 1)" the patch readme asked
            # for -- so the caller passing the precise filename still got four wrong dumps.
            return 99.0
        return (difflib.SequenceMatcher(None, bk, nn).ratio()
                + (0.0 if _BASE_NOISE.search(n) else 0.25)
                + (0.3 if re.search(r"\((USA|World|Europe)\)", n) else 0.0))

    _TMP.mkdir(parents=True, exist_ok=True)
    for n in sorted(members, key=score, reverse=True)[:limit]:
        dest = _TMP / f"base_{abs(hash(n)) % 10 ** 9}.zip"
        if _download(f"{NIROMS}/{quote(cart['zip'])}/{quote(n)}.zip", cookies, dest):
            yield dest
    yield from _source_base_from_disc_sources(folder, base_name, limit)


def _source_base_from_disc_sources(folder: str, base_name: str, limit: int = 4):
    """Same job, but over the registry's DISC-kind sources.

    _source_base_roms only ever looked at the cart source (the No-Intro zip). That is wrong for
    any system whose base ROMs are not in a No-Intro cart set: MSX disk games reported
    "base ROM not owned and not sourceable" while `Aleste 2 (Japan).zip` — holding
    `Aleste 2 (Japan) (Disk 1).rom`, md5 883e46527da292c148769425b30ad685, the exact base the
    patch asked for — sat in the registered [RA Sets] MSX collection the whole time
    (2026-08-15).
    """
    from app.services.sourcing import _sources_for, _item_files, _cookies, _norm, _download
    cookies = _cookies()
    bk = _norm(base_name)
    fr = " ".join(bk.split()[:2])
    if not bk:
        return
    for src in _sources_for(folder):
        if src.get("kind") == "cart" or not src.get("item"):
            continue
        try:
            files = _item_files(src["item"], cookies)
        except Exception:  # noqa
            continue
        pref = src.get("prefix") or ""
        cands = []
        for f in files:
            name = f.get("name") or ""
            if pref and not name.startswith(pref):
                continue
            stem = re.sub(r"\.\w{1,4}$", "", name.rsplit("/", 1)[-1])
            nn = _norm(stem)
            if not nn:
                continue
            if nn == bk:
                score = 99.0
            elif bk in nn or (fr and fr in nn):
                score = difflib.SequenceMatcher(None, bk, nn).ratio() + (
                    0.0 if _BASE_NOISE.search(stem) else 0.25)
            else:
                continue
            cands.append((score, name))
        _TMP.mkdir(parents=True, exist_ok=True)
        for _score, name in sorted(cands, reverse=True)[:limit]:
            dest = _TMP / f"dbase_{abs(hash(name)) % 10 ** 9}.zip"
            url = f"https://archive.org/download/{src['item']}/{quote(name)}"
            if _download(url, cookies, dest):
                yield dest


# ── patch provenance ─────────────────────────────────────────────────────────
# A patched hack is MANUFACTURED here, not downloaded, so its rebuild instruction is a recipe.
# Two of the three ingredients rot at very different rates:
#   - RAPatches lives in a GitHub repo and is reasonably durable.
#   - Everything else is a forum post pointing at Google Drive. Newer Super Mario Bros. Wii's
#     patch reached us as a hand-delivered file; a URL for it would be worth nothing.
# So keep the patch BYTES whenever they are small enough to be free, and treat the URL as a
# convenience rather than the source of truth. Content-addressed by md5, so the same patch
# shared by several hacks is stored once.
_PATCH_STORE = Path(settings.canonical_path) / "patches"
_PATCH_BLOB_MAX = 50 * 1024 * 1024          # 50 MB — most IPS/BPS are KB


def _store_patch_blob(data: bytes, name_hint: str = "") -> dict:
    """Persist patch bytes next to the library; return the provenance fragment."""
    if not data:
        return {"stored": False, "reason": "empty"}
    if len(data) > _PATCH_BLOB_MAX:
        return {"stored": False, "bytes": len(data),
                "reason": "larger than %d MB — kept by URL only" % (_PATCH_BLOB_MAX // 1048576)}
    md5 = hashlib.md5(data).hexdigest()
    try:
        _PATCH_STORE.mkdir(parents=True, exist_ok=True)
        ext = Path(name_hint or "").suffix.lower()
        if ext not in (".ips", ".bps", ".xdelta", ".vcdiff", ".patch"):
            ext = ".bin"
        dest = _PATCH_STORE / f"{md5}{ext}"
        if not dest.exists():
            dest.write_bytes(data)
        return {"stored": True, "md5": md5, "bytes": len(data),
                "path": str(dest.relative_to(Path(settings.canonical_path)))}
    except Exception as e:  # noqa
        return {"stored": False, "bytes": len(data), "reason": str(e)[:120]}


def _patch_cart(db, sysc, folder: str, g: dict, ra: dict, *, base_exts, bytes_fn, hash_fn,
                out_ext) -> str | None:
    """Generic cart hack patcher (NES/SNES/N64): apply a RAPatches .ips/.bps to a base ROM and
    keep the result only if it reproduces the RA hash. Tries owned bases first, then sources the
    clean retail base from No-Intro and deletes it afterward. Base is discovered from the patch
    path .../Hacks/<Base>/ ; only the correct base yields the RA hash so wrong ones are rejected."""
    from app.services.sourcing import _download
    purl = next((h.get("patch_url") for h in ra.get("hashes", []) if h.get("patch_url")), None)
    if not purl:
        return None
    md5set = {(h.get("md5") or "").lower() for h in ra.get("hashes", []) if h.get("md5")}
    # Do not count a disk we ALREADY HAVE as a successful patch. Aleste 2 wants Disks 2 and 3;
    # the patcher reproduced Disk 1 (which was already in the library), matched it against the
    # full md5set, declared success and returned — so the run reported `sourced: 1` for a game
    # that stayed incomplete, and every later sweep re-did the same work (825 s a time).
    # Narrowing the target to hashes we lack makes the existing "try every patch x every base"
    # loop keep going until it produces one we actually need (2026-08-15).
    try:
        from app.db.models import RomFile as _RF
        _owned = {(r.computed_hash or "").lower()
                  for r in db.query(_RF).filter(_RF.system_folder == folder).all()
                  if r.computed_hash}
        if md5set - _owned:                 # never empty the target set entirely
            md5set = md5set - _owned
    except Exception:  # noqa
        pass
    m = re.search(r"/Hacks/([^/]+)/", purl)
    base_name = unquote(m.group(1)) if m else g["title"]
    gid = str(g["id"])
    _TMP.mkdir(parents=True, exist_ok=True)
    pz = _TMP / f"patch_{g['id']}.zip"
    if not _download(purl, None, pz):
        _LAST_PATCH_FAIL[gid] = "patch download failed (RAPatches unreachable?)"
        return None
    base_hint_name = base_hint_md5 = None
    # RAPatches serves .7z for some sets — the N64 SM64 hacks are all
    # "<id>-SM64-<Name>.7z" — and zipfile.ZipFile() raises BadZipFile on those. The exception
    # escaped patch_game entirely, so no reason was ever recorded and the Missing page fell
    # back to "not attempted yet" for a game that had in fact been attempted four times
    # (2026-08-16). Repack any non-zip archive into a zip the block below can read.
    if not zipfile.is_zipfile(pz):
        exdir = _TMP / f"px_{gid}"
        shutil.rmtree(exdir, ignore_errors=True)
        exdir.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["7z", "x", "-y", f"-o{exdir}", str(pz)],
                           capture_output=True, timeout=1800)
        members = [f for f in sorted(exdir.rglob("*")) if f.is_file()]
        if not members:
            _LAST_PATCH_FAIL[gid] = ("patch archive could not be read (not a zip, and 7z "
                                     "extraction produced nothing: %s)"
                                     % (r.stderr or b"")[:80].decode("utf-8", "replace"))
            return None
        repack = _TMP / f"patch_{g['id']}_repack.zip"
        with zipfile.ZipFile(repack, "w") as zf:
            for f in members:
                zf.write(f, str(f.relative_to(exdir)))
        shutil.rmtree(exdir, ignore_errors=True)
        pz = repack
    try:
        with zipfile.ZipFile(pz) as z:
            # a patch zip may hold SEVERAL versions (e.g. v1.0 + v1.12) — RA registers one
            # specific version's hash, so try them all and keep whichever reproduces it.
            patches = [z.read(n) for n in z.namelist() if n.lower().endswith((".ips", ".bps"))]
            for n in z.namelist():
                if n.lower().endswith(".txt"):
                    base_hint_name, base_hint_md5 = _readme_base(z.read(n))
                    if base_hint_name or base_hint_md5:
                        break
            if not patches:
                kinds = sorted({Path(n).suffix.lower() for n in z.namelist()} - {".txt", ""})
                _LAST_PATCH_FAIL[gid] = (
                    "patch is %s, but this system's patcher only applies IPS/BPS"
                    % (", ".join(k.lstrip(".") for k in kinds) or "an unknown format"))
                return None
    finally:
        pz.unlink(missing_ok=True)
    want_crc = next((c for c in (_bps_source_crc(p) for p in patches) if c), None)
    if base_hint_name or base_hint_md5 or want_crc:
        log.info("cart patch base requirement", game=g.get("title"), base=base_hint_name,
                 base_md5=base_hint_md5,
                 base_crc32=("%08x" % want_crc) if want_crc else None)
    _EXT_STRIP = ("nes", "sfc", "smc", "fig", "z64", "n64", "v64", "ndd", "ips", "bps",
                  "sg", "sms", "gg", "32x", "bin", "md", "gen", "ws", "wsc", "ngp", "ngc",
                  "gb", "gbc", "gba", "a26", "col", "int", "vec", "lnx", "pce", "vb", "min")

    def _safe_for(md5: str) -> str:
        """Name the output after the hash it actually reproduces (the hack/translation),
        NOT the first-listed hash (which may be the untranslated base)."""
        hn = next((h.get("name") for h in ra.get("hashes", [])
                   if (h.get("md5") or "").lower() == md5 and h.get("name")), None) or g["title"]
        s = re.sub(r'[<>:"/\\|?*]', "", hn).strip() or f"hack_{g['id']}"
        if "." in s and s.rsplit(".", 1)[-1].lower() in _EXT_STRIP:
            s = s.rsplit(".", 1)[0]
        return s

    canon = Path(settings.canonical_path) / "roms" / folder

    def _try(base_path: Path, patch: bytes):
        try:
            patched = _apply_patch(bytes_fn(base_path), patch)
        except Exception:  # noqa
            return None
        got = hash_fn(patched)
        if got not in md5set:                          # wrong base or bad patch — reject
            return None
        safe = _safe_for(got)
        dest = canon / f"{safe}.zip"
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"{safe}{out_ext}", patched)
        hit = {"ra_game_id": g["id"], "title": g["title"], "hash_name": safe,
               "num_achievements": ra.get("num_achievements", 0), "points": ra.get("points", 0)}
        game = _get_or_create_game(db, hit, sysc.ra_console_id)
        # A patched hack was never downloaded, so there is no URL to record. What a rebuild
        # needs is the RECIPE: the patch, and enough to re-acquire the exact base. The base is
        # identified by md5 rather than filename because the same base ships under many names
        # across sources — the md5 is what actually has to match for the patch to reproduce
        # this hash.
        _upsert_rom(db, dest, folder, sysc.hash_method, got, "matched", safe, game.id,
                    {"ra": safe}, None,
                    {"kind": "patch", "verified": True,
                     "at": __import__("datetime").datetime.utcnow().isoformat(timespec="seconds"),
                     "patch": {"url": purl,
                               "blob": _store_patch_blob(patch, purl),
                               "base_md5": hashlib.md5(bytes_fn(base_path)).hexdigest(),
                               "base_file": base_path.name,
                               "base_hint_name": base_hint_name,
                               "base_hint_md5": base_hint_md5,
                               "format": "ips/bps", "out_ext": out_ext},
                     # Cart patches are applied in-process, so there is no shell command to
                     # replay — name the routine instead of inventing a fake argv.
                     "steps": [{"op": "apply_patch", "impl": "patching._apply_patch",
                                "in": "base_md5", "out": "rom"}]})
        db.commit()
        return "patch"

    def _is_wanted_base(p: Path) -> bool:
        """Does this file match the base the patch actually names (md5, else BPS CRC32)?"""
        if not (base_hint_md5 or want_crc):
            return False
        try:
            b = bytes_fn(p)
        except Exception:  # noqa
            return False
        if base_hint_md5 and hashlib.md5(b).hexdigest() == base_hint_md5:
            return True
        return bool(want_crc and (zlib.crc32(b) & 0xFFFFFFFF) == want_crc)

    # Cart ROMs live in canonical as .zip (the No-Intro shape RA hashes through), so filtering
    # on base_exts alone -- ('.sms',), ('.nes',) -- matched almost nothing and the owned-base
    # pass was silently dead for every raw-cart system. It returned "Ring Dash SMS" as the best
    # base for Alex Kidd purely because that one file happened to be stored unzipped.
    _accept = tuple(base_exts) + (".zip",)
    owned = [p for p in canon.glob("*") if p.suffix.lower() in _accept] if canon.is_dir() else []
    # Exact-base-first: a title match returns every regional dump AND any hack/translation of
    # the same game, and only one specific dump can reproduce the RA hash.
    ordered = sorted(_base_candidates(base_name, owned, limit=12),
                     key=lambda p: 0 if _is_wanted_base(p) else 1)
    for base in ordered:
        for patch in patches:
            if _try(base, patch) == "patch":
                return "patch"
    # fallback: source the retail base from No-Intro, patch, then delete it (user-approved).
    # Ask for the readme's exact filename before the fuzzy title — for Alex Kidd the fuzzy
    # search returned four wrong dumps and never the "(Rev 1)" the patch requires.
    tried_names = []
    for nm in ([base_hint_name] if base_hint_name else []) + [base_name]:
        stem = re.sub(r"\.\w{1,4}$", "", nm)
        if stem in tried_names:
            continue
        tried_names.append(stem)
        for tmp in _source_base_roms(folder, stem, base_exts, limit=4):
            try:
                for patch in patches:
                    if _try(tmp, patch) == "patch":
                        return "patch"
            finally:
                tmp.unlink(missing_ok=True)
    if base_hint_name or base_hint_md5 or want_crc:
        need = base_hint_name or base_name
        _LAST_PATCH_FAIL[gid] = (
            "base ROM not owned and not sourceable — patch needs %s%s" %
            (need, " (md5 %s)" % base_hint_md5 if base_hint_md5 else
             (" (crc32 %08x)" % want_crc if want_crc else "")))
    else:
        _LAST_PATCH_FAIL[gid] = ("no base ROM reproduced the RA hash (tried %d owned + sourced "
                                 "candidates); patch readme names no base" % len(ordered))
    return None


def patch_game(db, sysc, folder: str, g: dict, ra: dict) -> str | None:
    """Try to produce game g by patching a base ROM (owned, else sourced). Returns 'patch'.

    Every failing path must leave a reason in _LAST_PATCH_FAIL. Only the PS2/PSP path ever did,
    so a failed GameCube/Wii/CD/cart patch fell through to patch_outlook's last resort —
    "patch published; not yet produced" — which claims we never TRIED. We had tried, and the
    reason was simply thrown away. The wrapper below guarantees an attempt is always recorded
    as an attempt, even when the path itself has nothing specific to say.
    """
    gid = str(g["id"])
    had = gid in _LAST_PATCH_FAIL
    out = _patch_game_inner(db, sysc, folder, g, ra)
    if out is None and not had and gid not in _LAST_PATCH_FAIL:
        # Last resort only. Every reachable failure path now records something specific, so the
        # remaining silent cases are structural: either the console has no patcher at all, or
        # it has one but RAPatches publishes nothing for this game.
        cid = sysc.ra_console_id
        if cid in RAPATCHES_SYS:
            try:
                indexed = bool(_index().get(gid))
            except Exception:  # noqa
                indexed = False
            _LAST_PATCH_FAIL[gid] = (
                "patch attempted, but this system's patcher recorded no reason — check the log"
                if indexed else "no RAPatches entry published for this game")
        elif sysc.hash_method != "raw":
            _LAST_PATCH_FAIL[gid] = (
                "no patcher for %s — RA hashes the patched dump, and this console has neither a "
                "RAPatches xdelta path nor an IPS/BPS cart path" % folder)
        else:
            _LAST_PATCH_FAIL[gid] = ("patch attempted, but this system's patcher recorded no "
                                     "reason — check the log for this game")
    elif out == "patch":
        _LAST_PATCH_FAIL.pop(gid, None)
    return out


def _patch_game_inner(db, sysc, folder: str, g: dict, ra: dict) -> str | None:
    if sysc.ra_console_id == 7:                         # NES → IPS/BPS patch path
        return _patch_nes(db, sysc, folder, g, ra)
    if sysc.ra_console_id == 3:                         # SNES → IPS/BPS patch path
        return _patch_snes(db, sysc, folder, g, ra)
    if sysc.ra_console_id == 2:                         # N64 → IPS/BPS patch path
        return _patch_n64(db, sysc, folder, g, ra)
    if sysc.ra_console_id == 18:                        # NDS → IPS/BPS/xdelta via RA patch_url
        return _patch_nds(db, sysc, folder, g, ra)
    if sysc.hash_method == "raw":                       # all raw-cart systems → IPS/BPS patch path
        return _patch_raw(db, sysc, folder, g, ra)
    info = _index().get(str(g["id"]))                   # disc systems → RAPatches xdelta path
    if not info:
        # A cart system with no RAPatches entry is genuinely "no patch published"; saying
        # nothing here lets patch_outlook fall back to RA's own patch_url check.
        return None
    cid = sysc.ra_console_id
    if cid in _DOLPHIN_CONSOLES:                        # GameCube/Wii → dolphin ISO, store RVZ
        return _patch_disc_dolphin(db, sysc, folder, g, ra, info)
    if cid in _CHDCD_CONSOLES:                          # CD systems → chdman, store CHD
        return _patch_disc_cd(db, sysc, folder, g, ra, info)
    if cid in _DVD_CONSOLES:                            # PS2/PSP → chd_extract, store ISO
        return _patch_disc_dvd(db, sysc, folder, g, ra, info)
    log.warning("no patch path for console — patch indexed but unusable",
                console=cid, system=folder, game=g.get("title"))
    return None


def patch_outlook(sysc, g: dict, ra: dict) -> str | None:
    """Why a wanted game could not be produced, as a short phrase for the alert footer —
    or None when it is an ordinary dump that simply wasn't found at any source.

    "No hash-matching source found" is actively misleading for a hack or translation: RA
    hashes the PATCHED output, so no source anywhere will ever carry it and no amount of
    searching can succeed. The only route is a patch, so the useful question is whether one
    has been published. Answered the same two ways patch_game dispatches — RA's per-hash
    patch_url for cart systems, our RAPatches index for disc systems."""
    from app.services.ingest import clean_ra_title
    cat = (clean_ra_title(g.get("title") or "")[1] or "").lower()
    label = "Translation" if "translation" in cat else "Hack" if "hack" in cat else None
    has_patch = any(h.get("patch_url") for h in ra.get("hashes", []))
    if not has_patch and sysc.ra_console_id in RAPATCHES_SYS:
        try:
            has_patch = bool(_index().get(str(g["id"])))
        except Exception:  # noqa — a GitHub hiccup must not break the reason string
            return None
    if has_patch:
        # Prefer what the patcher actually hit this run. Asserting "the patch didn't reproduce
        # the hash" is wrong whenever we never obtained a base disc to apply it to — which is
        # the common case, and is how the PS2 fairy-tale titles came to report a patch failure
        # for a patch that was never run.
        actual = _LAST_PATCH_FAIL.get(str(g["id"]))
        if actual:
            return f"{label or 'Patch'} — {actual}"
        return f"{label or 'Patch'} — patch published; not yet produced"
    return f"{label} — no patch published" if label else None


# ── Shared disc-patch helpers ─────────────────────────────────────────────────

_HINT_FILE = "base_hint.json"
# Trailing annotations are common: "Peter Pan (Europe) (En,Fr,De).bin (Redump)". Capturing
# them into the filename broke BOTH the exact-name check and the .bin/CD detection, so that
# title was rejected even though the readme's md5 is Redump's own and therefore correct.
_HINT_IMG_RE = re.compile(r"([^\s:][^:]*?\.(?:iso|bin|cue|chd))(?:\s*\([^)]*\))*\s*$", re.I)
_HINT_MD5_RE = re.compile(r"\bmd5\b\s*[:=]\s*([0-9a-f]{32})\b", re.I)
# Same idea for cart hacks, whose readmes name a .sms/.nes/.sfc/... instead of a disc image.
_HINT_ROM_RE = re.compile(
    r"([^\s:][^:]*?\.(?:sms|gg|sg|nes|fds|sfc|smc|fig|md|gen|bin|32x|n64|z64|v64|gb|gbc|gba"
    r"|a26|a78|col|int|vec|lnx|pce|vb|ws|wsc|ngp|ngc|min))\s*$", re.I)


def _readme_base(txt: bytes) -> tuple[str | None, str | None]:
    """(base filename, base md5) named by a cart hack's readme.

    The disc patcher has gated on this since the PS2 work; the cart patcher never did, and it
    matters just as much. Alex Kidd in Mushroom World needs "Alex Kidd in Miracle World (USA,
    Europe, Brazil) (En) (Rev 1).sms" specifically — we owned only a Portuguese TRANSLATION of
    that game, and the fuzzy title match happily fed it (plus four wrong No-Intro dumps) to the
    patcher, all of which produced a non-matching hash and no explanation.
    """
    try:
        s = txt.decode("utf-8", "replace")
    except Exception:  # noqa
        return None, None
    name = md5 = None
    for line in (l.strip() for l in s.splitlines()):
        if not line:
            continue
        if name is None:
            body = line.split(":", 1)[-1].strip() if line.lower().startswith("file:") else line
            m = _HINT_ROM_RE.search(body)
            if m:
                name = m.group(1).strip()
        if md5 is None:
            m = _HINT_MD5_RE.search(line)
            if m:
                md5 = m.group(1).lower()
    return name, md5


def _bps_source_crc(patch: bytes) -> int | None:
    """A BPS footer states the CRC32 of the exact base it was built against — a free, decisive
    check that beats applying the patch to a pile of guesses and hoping one hashes right."""
    if len(patch) < 12 or patch[:4] != b"BPS1":
        return None
    try:
        return struct.unpack("<III", patch[-12:])[0]
    except Exception:  # noqa
        return None


def _stash_base_hint(txt: bytes, stage: Path) -> None:
    """Record the base disc a RAPatches readme says to patch against.

    Most readmes state it outright, often with the base's MD5:

        Use with:
        (Redump)
        Grand Theft Auto - San Andreas (USA) (v1.03).iso
        MD5: c383c015065f8060343032480928d08d

    That is far stronger than guessing the base from the hack's title, which picked Guitar
    Hero II for a Guitar Hero III hack and "Cars Mater-National Championship" for a patch
    whose real base is "Disney-Pixar Cars (USA)". It also gives the sourcing fallback an exact
    filename to look for, and an MD5 to reject a wrong base BEFORE a multi-GB extract.
    """
    try:
        s = txt.decode("utf-8", "replace")
    except Exception:  # noqa
        return
    name = md5 = None
    for line in (l.strip() for l in s.splitlines()):
        if not line:
            continue
        if name is None:
            m = _HINT_IMG_RE.search(line.split(":", 1)[-1].strip() if line.lower().startswith("file:")
                                    else line)
            if m:
                name = m.group(1).strip()
        if md5 is None:
            m = _HINT_MD5_RE.search(line)
            if m:
                md5 = m.group(1).lower()
    if name or md5:
        try:
            (stage / _HINT_FILE).write_text(json.dumps({"base_name": name, "base_md5": md5}))
        except Exception:  # noqa
            pass


def _base_hint(stage: Path) -> dict:
    try:
        return json.loads((stage / _HINT_FILE).read_text())
    except Exception:  # noqa
        return {}


def _rahash_cli(cid: int, path: Path, timeout: int = 600) -> str:
    """rahash a plain disc image (iso/bin/cue) directly — auto-detects sector size."""
    r = subprocess.run([RAHASH, str(cid), str(path)], capture_output=True, text=True,
                       timeout=timeout)
    out = (r.stdout or "").strip()
    return out if r.returncode == 0 and len(out) == 32 else ""


def _safe_name(ra: dict, g: dict) -> str:
    """Canonical output basename: the enriched patched-hash name, else the RA title."""
    hname = next((h.get("name") for h in ra.get("hashes", []) if h.get("name")), None) or g["title"]
    return re.sub(r'[<>:"/\\|?*]', "", hname).strip() or f"hack_{g['id']}"


_LAST_PAYLOAD_KINDS: dict[str, list[str]] = {}
_PATCH_TOOL_HINT = {"exe": "a Windows self-applying .exe",
                    "patch": "a GeckoPatcher .patch",
                    "ppf": "a PPF patch", "rvz": "a prepatched RVZ"}


def _payload_note(path: str) -> str:
    kinds = _LAST_PAYLOAD_KINDS.get(path) or []
    if not kinds:
        return "no applicable payload"
    named = [_PATCH_TOOL_HINT.get(k, "." + k) for k in kinds]
    return "ships %s, which this patcher cannot apply" % " + ".join(named)


def _fetch_patch_xdeltas(path: str, stage: Path) -> list[Path]:
    """Download a RAPatches archive and extract its xdelta/xdelta3 payload(s) into `stage`.
    An archive may hold multiple versions — the hash gate downstream keeps whichever
    reproduces the RA hash. The bundled xdelta.exe (a Windows applier) is skipped.

    Handles BOTH payload formats RAPatches ships: .zip in-process, and .7z by shelling out
    to the `7z` binary (already a dependency of the sourcer's archive handling)."""
    from app.services.sourcing import _download
    pz = stage / ("patch.7z" if path.lower().endswith(".7z") else "patch.zip")
    if not _download(f"{RAP_RAW}/{quote(path)}", None, pz):
        return []
    out: list[Path] = []
    seen: set[str] = set()
    try:
        if pz.suffix.lower() == ".7z":
            exdir = stage / "_patch7z"
            exdir.mkdir(exist_ok=True)
            subprocess.run([SEVENZ, "x", "-y", f"-o{exdir}", str(pz)],
                           capture_output=True, timeout=300)
            for f in sorted(exdir.rglob("*")):
                if f.is_file():
                    seen.add(f.suffix.lower())
                if f.is_file() and f.suffix.lower() in (".xdelta", ".xdelta3"):
                    dest = stage / f"p{len(out)}.xdelta"
                    dest.write_bytes(f.read_bytes())
                    out.append(dest)
        else:
            with zipfile.ZipFile(pz) as z:
                for n in z.namelist():
                    seen.add(Path(n).suffix.lower())
                    if n.lower().endswith((".xdelta", ".xdelta3")):
                        dest = stage / f"p{len(out)}.xdelta"
                        dest.write_bytes(z.read(n))
                        out.append(dest)
                    elif n.lower().endswith(".txt"):
                        _stash_base_hint(z.read(n), stage)
    except Exception:  # noqa
        return []
    finally:
        pz.unlink(missing_ok=True)
    if not out:
        # Remember the formats it DID ship. "no xdelta payload" alone doesn't tell you whether
        # the patch is unobtainable or merely in a format we haven't taught this path yet.
        _LAST_PAYLOAD_KINDS[path] = sorted(e.lstrip(".") for e in seen if e and e != ".txt")
    return out


def _source_base_disc_urls(folder: str, ra: dict, title: str, limit: int = 5):
    """(download_url, cookies) candidates for a clean base disc from the folder's archive.org
    disc sources, matched by region-stripped title. The base isn't in the RA hash set (only the
    PATCHED hash is), so we can't hash-gate the DOWNLOAD — the post-patch hash is the real gate,
    so a wrong base simply fails to reproduce the RA hash and is discarded."""
    from app.services.sourcing import (_sources_for, _item_index, _cookies, _title_key,
                                       _search_candidates, ARCHIVE)
    cookies = _cookies()
    urls: list[str] = []
    tk = _title_key(title)
    for src in _sources_for(folder):
        if src.get("kind") == "cart" or not src.get("item"):
            continue
        idx = _item_index(src["item"], cookies)
        names = list(idx.get(tk, []))
        if not names:                                  # fuzzy fallback (typo / dropped 's')
            scored = sorted(((difflib.SequenceMatcher(None, tk, fk).ratio(), fn)
                             for fk, fns in idx.items() for fn in fns), reverse=True)
            names = [fn for r, fn in scored[:3] if r >= 0.8]
        # Rank EXACT base-name matches first. _title_key strips the region, so one key matches
        # every regional dump of a title: "Psychonauts" pulls (Europe), (Japan) and (USA) alike,
        # and plain listing order puts Europe first. But a RAPatches base is region-specific —
        # the wrong region simply fails the readme base_md5 gate, after a full multi-GB download.
        # With the 33 PS2 letter collections registered that misfire is the common case, so pay
        # the sort here rather than ~2.5GB per wrong guess across every patch title.
        want = (title or "").strip().lower()

        def _region(s: str) -> str:
            """First parenthesised group — the region, by No-Intro/Redump convention."""
            m = re.search(r"\(([^)]+)\)", s or "")
            return (m.group(1).strip().lower() if m else "")

        # The base name hint is region-STRIPPED ("Psychonauts"), so it cannot discriminate
        # between regional dumps at all. The region lives in the RA hash name instead —
        # "Psychonauts (USA) (Anti-Cheat) (v1.0) (Manako)" — so take it from there.
        want_regions = {_region(h.get("name", "")) for h in ra.get("hashes", []) if h.get("name")}
        want_regions.discard("")

        def _rank(fn: str) -> tuple:
            stem = Path(fn).name.rsplit(".", 1)[0].strip().lower()
            exact = 0 if stem == want else (1 if want and stem.startswith(want) else 2)
            # A region mismatch means a guaranteed base_md5 rejection after a full multi-GB
            # download, so it outranks name closeness.
            reg = 0 if (want_regions and _region(stem) in want_regions) else 1
            return (reg, exact)
        for fn in sorted(names, key=_rank):
            urls.append(f"{ARCHIVE}/download/{quote(src['item'])}/{quote(fn)}")
    try:                                               # last resort: archive.org title search
        for url, _n, _d in _search_candidates(ra, cookies) or []:
            urls.append(url)
    except Exception:  # noqa
        pass
    # de-dupe, preserve order
    seen, uniq = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq[:limit], cookies


def _new_stage(gid) -> Path:
    stage = _STAGE / f"rfpatch_{gid}"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    return stage


# ── GameCube / Wii (dolphin ISO → xdelta → RVZ) ───────────────────────────────

def _md5_file(p: Path) -> str:
    import hashlib
    h = hashlib.md5()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _disc_recipe(patch_src: str | None, patch_file: Path | None, base: Path,
                 steps: list) -> dict:
    """Rebuild recipe for a patched DISC title.

    Unlike a cart patch, this is several tool invocations whose FLAGS matter — dolphin-tool
    refuses to write RVZ without -b/-c, and the distro xdelta3 cannot decode the >2GiB sources
    these patches use. Recording the literal argv means a rebuild replays what worked instead
    of re-deriving it. (Newer Super Mario Bros. Wii took three attempts to get right: Rev 1 not
    Rev 2, .iso not .rvz, and the right xdelta binary.)
    """
    import datetime as _dt
    blob = {"stored": False}
    if patch_file is not None:
        try:
            blob = _store_patch_blob(patch_file.read_bytes(), patch_file.name)
        except Exception as e:  # noqa
            blob = {"stored": False, "reason": str(e)[:100]}
    base_md5 = None
    try:
        import hashlib as _h
        hh = _h.md5()
        with open(base, "rb") as fh:
            for chunk in iter(lambda: fh.read(8 << 20), b""):
                hh.update(chunk)
        base_md5 = hh.hexdigest()
    except Exception:  # noqa
        pass
    return {"kind": "patch", "verified": True,
            "at": _dt.datetime.utcnow().isoformat(timespec="seconds"),
            "patch": {"url": patch_src, "blob": blob,
                      "base_md5": base_md5, "base_file": base.name},
            "steps": steps}


def _hash_iso(cid: int, iso: Path) -> str:
    """rahash the extracted ISO directly (already a plain image — no async needed)."""
    return _rahash_cli(cid, iso, timeout=300)


def _patch_disc_dolphin(db, sysc, folder: str, g: dict, ra: dict, info: dict) -> str | None:
    cid, gid = sysc.ra_console_id, g["id"]
    md5set = {(h.get("md5") or "").lower() for h in ra.get("hashes", []) if h.get("md5")}
    safe = _safe_name(ra, g)
    canon = Path(settings.canonical_path) / "roms" / folder
    canon.mkdir(parents=True, exist_ok=True)
    stage = _new_stage(gid)
    try:
        xds = _fetch_patch_xdeltas(info["path"], stage)
        if not xds:
            # Silent until now: both disc paths bailed here with no reason, so the Missing page
            # said "this system's patcher recorded no reason" for every one of them.
            _LAST_PATCH_FAIL[str(gid)] = ("%s — %s" % (Path(info["path"]).name,
                                                       _payload_note(info["path"])))
            return None

        def _try_base(base: Path) -> str | None:
            base_iso, out_iso = stage / "base.iso", stage / "out.iso"
            r = subprocess.run([DOLPHIN, "convert", "-i", str(base), "-o", str(base_iso),
                                "-f", "iso"], capture_output=True, text=True, timeout=2400)
            if r.returncode != 0 or not base_iso.exists():
                return None
            try:
                for xd in xds:
                    out_iso.unlink(missing_ok=True)
                    r = _xd3_decode(base_iso, xd, out_iso)
                    if r.returncode != 0 or not out_iso.exists():
                        continue
                    if _hash_iso(cid, out_iso) not in md5set:
                        continue
                    dest = canon / f"{safe}.rvz"
                    r = subprocess.run([DOLPHIN, "convert", "-i", str(out_iso), "-o", str(dest),
                                        "-f", "rvz", "-c", "zstd", "-l", "5", "-b", "131072"],
                                       capture_output=True, text=True, timeout=2400)
                    if r.returncode != 0 or not dest.exists():
                        continue
                    got = _hash_iso(cid, out_iso)
                    _onboard(db, sysc, folder, g, ra, dest, safe, got,
                             _disc_recipe(info.get("path"), xd, base, [
                                 {"op": "convert", "argv": [DOLPHIN, "convert", "-i", "<base>",
                                                            "-o", "<base.iso>", "-f", "iso"]},
                                 {"op": "xdelta3", "argv": [XDELTA3, "-d", "-s", "<base.iso>",
                                                            "<patch>", "<out.iso>"],
                                  "note": "source window sized to the source file; the distro "
                                          "xdelta3 3.0.11 cannot decode >2GiB sources"},
                                 {"op": "convert", "argv": [DOLPHIN, "convert", "-i", "<out.iso>",
                                                            "-o", "<dest.rvz>", "-f", "rvz",
                                                            "-c", "zstd", "-l", "5",
                                                            "-b", "131072"],
                                  "note": "dolphin-tool errors without -b/-c"},
                             ]))
                    return "patch"
            finally:
                base_iso.unlink(missing_ok=True)
                out_iso.unlink(missing_ok=True)
            return None

        for base in _base_candidates(info["base"] or g["title"], _owned_discs(folder)):
            if _try_base(base) == "patch":
                return "patch"
        # no owned base matched — source a clean base disc, patch, then discard it
        urls, cookies = _source_base_disc_urls(folder, ra, info["base"] or g["title"])
        from app.services.sourcing import _download
        for url in urls:
            dl = stage / "base_src"
            dl.unlink(missing_ok=True)
            if not _download(url, cookies, dl):
                continue
            try:
                if _try_base(dl) == "patch":
                    return "patch"
            finally:
                dl.unlink(missing_ok=True)
        # NB: no `hint` in this path -- _base_hint/_stash_base_hint are the DVD path's machinery,
        # so the readme base name is not available here; info["base"] is what we have.
        need = info["base"] or g["title"]
        _LAST_PATCH_FAIL[str(gid)] = (
            "no base disc reproduced the RA hash — patch needs %s (tried %d source candidate%s)"
            % (need, len(urls), "" if len(urls) == 1 else "s")
            if urls else
            "base disc not available from any configured source (patch needs %s)" % need)
        return None
    finally:
        shutil.rmtree(stage, ignore_errors=True)


# ── DVD systems (PS2 / PSP — CHD or ISO base → xdelta → store ISO) ────────────
# PS2 (21) and PSP (41) were listed in RAPATCHES_SYS, so their patches were INDEXED, but
# patch_game dispatched only on _DOLPHIN_CONSOLES / _CHDCD_CONSOLES — so it fell through to
# `return None` and neither system could ever apply one. That silently cost PS2 16 of its 26
# missing games, most of them `Fix` entries (AntiCheat / RACompatibility) that RA REQUIRES to
# recognise otherwise-mainstream discs: Psychonauts, Cars, Spyro, Transformers, Haunted Mansion.
CHD_EXTRACT = "/usr/local/bin/chd_extract"
CHDMAN276 = "/usr/local/bin/chdman276"   # has extractcd; the packaged 0.251 does not


def _owned_discs_dvd(folder: str) -> list[Path]:
    d = Path(settings.canonical_path) / "roms" / folder
    return [p for p in d.glob("*") if p.suffix.lower() in (".chd", ".iso")] if d.is_dir() else []


# Minimum title similarity before a DVD disc is accepted as a patch base.
_DVD_BASE_MIN_RATIO = 0.62


def _dvd_base_candidates(base_name: str, discs: list[Path], limit: int = 2) -> list[Path]:
    """Like _base_candidates but REFUSES weak matches.

    _base_candidates deliberately returns a few extra guesses because "wrong bases are rejected
    downstream by the hash check, so returning a few extra candidates is cheap" — true for
    carts, false here. On PS2 every rejected guess costs a multi-GB chd_extract plus an xdelta,
    so a bad guess is minutes of disk churn, not milliseconds. Worse, its `or list(discs)`
    fallback means an UNOWNED base still yields nonsense: it proposed Sly 3 for Psychonauts,
    Shinobi for Pinocchio and Arctic Thunder for Cinderella.

    So require real similarity and return nothing when the base simply isn't owned — the caller
    then goes to the sourcing fallback, which is what should happen.
    """
    from app.services.sourcing import _title_key
    bk = _title_key(base_name)
    if not bk:
        return []
    scored = []
    for p in discs:
        pk = _title_key(p.name)
        ratio = difflib.SequenceMatcher(None, bk, pk).ratio()
        if bk in pk or pk in bk:                     # containment beats a middling ratio
            ratio = max(ratio, 0.95)
        if ratio >= _DVD_BASE_MIN_RATIO:
            scored.append((ratio + (0.0 if _BASE_NOISE.search(p.name) else 0.05), p))
    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored[:limit]]


def _dvd_base_iso(src: Path, stage: Path, want_bin: bool = False) -> Path | None:
    """Give xdelta a plain ISO to diff against.

    .chd goes through chd_extract, NOT chdman: the box has chdman 0.251, which predates the
    zstd CHD codec and returns rc=1 on essentially every disc in /roms/ps2. chd_extract is the
    libchdr-based tool built for exactly this (see tools/chd_extract).
    A plain .iso is used in place — xdelta only ever reads the source, never writes it, so
    there is no reason to burn 4.7GB of staging copying it.
    """
    ext = src.suffix.lower()
    if ext == ".iso":
        return src
    if ext in (".zip", ".7z", ".rar"):
        # Disc archives serve base discs wrapped as often as bare — unwrap, then recurse on
        # whatever image is inside (an .iso passes straight through, a .chd gets extracted).
        exdir = stage / "base_unpack"
        shutil.rmtree(exdir, ignore_errors=True)
        exdir.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([SEVENZ, "x", "-y", f"-o{exdir}", str(src)],
                           capture_output=True, text=True, timeout=5400)
        # PS2 dumps come as .bin/.img as often as .iso; restricting to .iso/.chd made every
        # sourced base report "no disc image inside archive" and threw away the download.
        prefer = (".iso", ".bin", ".img", ".chd")
        files = [p for p in exdir.rglob("*") if p.is_file()]
        inner = sorted((p for p in files if p.suffix.lower() in prefer),
                       key=lambda p: (prefer.index(p.suffix.lower()), -p.stat().st_size))
        if not inner:
            # Say what WAS in there — a bare "no disc image" is undiagnosable, and an
            # extractor that silently did nothing looks identical to a bad archive.
            log.warning("no disc image inside archive", archive=src.name,
                        rc=r.returncode, extracted=len(files),
                        contents=[p.name[:48] for p in files[:6]],
                        err=(r.stderr or "")[:160])
            return None
        return _dvd_base_iso(inner[0], stage, want_bin)
    if ext in (".bin", ".img"):
        return src                                   # a raw DVD image is already an ISO
    if ext != ".chd":
        log.warning("unusable base disc format", disc=src.name, ext=ext)
        return None
    if want_bin:
        # A CD-based PS2 title (the Phoenix Games fairy-tale discs are ~600MB CDs, not DVDs).
        # Its CHD stores 2352+96 CD frames with interleaved subcode, so chd_extract's raw hunk
        # concatenation does NOT yield Redump's .bin -- and the xdelta was authored against
        # that .bin, so the patch applied cleanly to the wrong bytes and produced a disc that
        # could never match the RA hash. chdman 0.276's extractcd de-interleaves properly.
        out = stage / "base.bin"
        cue = stage / "base.cue"
        for q in (out, cue):
            q.unlink(missing_ok=True)
        r = subprocess.run([CHDMAN276, "extractcd", "-i", str(src), "-o", str(cue),
                            "-ob", str(out), "-f"], capture_output=True, text=True, timeout=5400)
        if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            log.warning("chdman extractcd failed", disc=src.name, rc=r.returncode,
                        err=(r.stderr or "")[:160])
            out.unlink(missing_ok=True)
            return None
        return out
    out = stage / "base.iso"
    out.unlink(missing_ok=True)
    # chdman's extractdvd knows the logical disc size from the CHD metadata; chd_extract just
    # concatenates hunks, which does not reproduce Redump's .iso byte-for-byte. That silently
    # fed the xdelta a base that was RIGHT but not IDENTICAL, so the patch applied and the
    # output could never match the RA hash -- reported for months as "patch applied, but the
    # result did not match". Verified against the DAT: the RAPatches readme md5 for Psychonauts
    # and Cars IS Redump's, so the base was never the problem, our extraction was.
    r = subprocess.run([CHDMAN276, "extractdvd", "-i", str(src), "-o", str(out), "-f"],
                       capture_output=True, text=True, timeout=5400)
    if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        log.info("chdman extractdvd unavailable/failed — falling back to chd_extract",
                 disc=src.name, rc=r.returncode, err=(r.stderr or "")[:120])
        out.unlink(missing_ok=True)
        r = subprocess.run([CHD_EXTRACT, str(src), str(out)], capture_output=True, text=True,
                           timeout=5400)
    if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        log.warning("chd_extract failed", disc=src.name, rc=r.returncode,
                    size_mb=round(src.stat().st_size / 1e6) if src.exists() else 0,
                    err=(r.stderr or "")[:160])
        out.unlink(missing_ok=True)
        return None
    return out


def _patch_disc_dvd(db, sysc, folder: str, g: dict, ra: dict, info: dict) -> str | None:
    """Patch a DVD-based disc (PS2/PSP) and store the verified result as .iso.

    Stored as ISO rather than re-compressed to CHD on purpose: chdman 0.251 has no `createdvd`
    (only createraw/hd/cd/ld), and both systems already accept .iso per systems.yaml. It also
    avoids a multi-GB chdman compress, which per the ops notes thrashes the raidz2 pool. Convert
    to CHD later in a maintenance window if the space matters.
    """
    cid, gid = sysc.ra_console_id, g["id"]
    md5set = {(h.get("md5") or "").lower() for h in ra.get("hashes", []) if h.get("md5")}
    safe = _safe_name(ra, g)
    canon = Path(settings.canonical_path) / "roms" / folder
    canon.mkdir(parents=True, exist_ok=True)
    stage = _new_stage(gid)
    try:
        xds = _fetch_patch_xdeltas(info["path"], stage)
        if not xds:
            # Silent until now: both disc paths bailed here with no reason, so the Missing page
            # said "this system's patcher recorded no reason" for every one of them.
            _LAST_PATCH_FAIL[str(gid)] = ("%s — %s" % (Path(info["path"]).name,
                                                       _payload_note(info["path"])))
            return None

        hint = _base_hint(stage)

        # The readme names the exact file the patch was built against; a .bin means a CD-based
        # title whose base must be extracted as raw 2352-byte sectors, not cooked to an ISO.
        want_bin = str((hint or {}).get("base_name") or "").lower().endswith((".bin", ".cue"))

        def _try_base(base: Path, src_name: str | None = None) -> str | None:
            nonlocal tried_any
            base_iso = _dvd_base_iso(base, stage, want_bin=want_bin)
            if not base_iso:
                return None
            # THE base check. RAPatches readmes state the exact disc the patch was built
            # against, usually with its MD5 — and _stash_base_hint has always recorded it, but
            # nothing ever read it. Without this gate the fuzzy title matcher's pick is applied
            # blind, and it picks siblings: Disney-Pixar WALL-E for Cars, Guitar Hero II for a
            # Guitar Hero III hack, Tarzan Untamed for The Haunted Mansion, The Eternal Night
            # for Spyro: Dawn of the Dragon. The patch then produces garbage and the failure is
            # reported as "patch applied, but the result did not match the RA hash" — which
            # blames the patch for what is really the wrong base. All 7 PS2 patch failures on
            # 2026-08-04 were this.
            # ...but the readme is NOT infallible, so this is a strong preference, not a veto.
            # The RAPatches readmes for the Phoenix Games titles are mis-templated: Cinderella's
            # states "BitSize: 630 MB" alongside "Size (Bytes): 4,617,332,140", and Hansel and
            # Gretel's states Redump's exact byte size with a DIFFERENT md5. Vetoing on that
            # rejected the genuinely correct disc and reported "base disc not available from any
            # configured source" for a disc we had downloaded and were holding.
            # So: when the candidate's NAME is exactly the base the readme names, apply the patch
            # anyway and let the post-patch RA hash decide -- that is the real gate, and a wrong
            # base simply fails it. Name mismatch + md5 mismatch is still rejected outright,
            # which is what stops the sibling-disc guesses this check was added for.
            want_md5 = (hint or {}).get("base_md5")
            name_is_exact = False
            if want_md5 and hint.get("base_name"):
                from app.services.sourcing import _title_key as _tk
                cand = src_name or base.name
                name_is_exact = _tk(cand) == _tk(hint["base_name"]) and bool(_tk(cand))
            if want_md5:
                got_md5 = _md5_file(base_iso)
                if got_md5 != want_md5 and name_is_exact:
                    log.info("readme md5 disagrees but the name matches the readme's base "
                             "exactly — patching anyway; the RA hash is the real gate",
                             game=g.get("title"), base=(src_name or base.name)[:60],
                             readme_base=hint.get("base_name"), want=want_md5, got=got_md5)
                elif got_md5 != want_md5:
                    log.info("base disc rejected — readme md5 mismatch", game=g.get("title"),
                             base=(src_name or base.name)[:60], want=want_md5, got=got_md5)
                    if base_iso != base:
                        base_iso.unlink(missing_ok=True)
                    return None
                log.info("base disc verified against readme md5", game=g.get("title"),
                         base=base.name[:60])
            tried_any = True
            out_iso = stage / "out.iso"
            try:
                for xd in xds:
                    out_iso.unlink(missing_ok=True)
                    r = _xd3_decode(base_iso, xd, out_iso)
                    if r.returncode != 0 or not out_iso.exists():
                        continue
                    got = _hash_iso(cid, out_iso)
                    if got not in md5set:
                        continue
                    dest = canon / f"{safe}.iso"
                    shutil.move(str(out_iso), str(dest))
                    _onboard(db, sysc, folder, g, ra, dest, safe, got)
                    return "patch"
            finally:
                out_iso.unlink(missing_ok=True)
                if base_iso != base:            # only the extracted copy, never an owned disc
                    base_iso.unlink(missing_ok=True)
            return None

        # Prefer the base the patch's own readme names over anything inferred from the title.
        tried_any = False          # did we ever get a real base image to apply the patch to?
        owned = _owned_discs_dvd(folder)
        ordered: list[Path] = []
        named_but_unowned = False
        if hint.get("base_name"):
            from app.services.sourcing import _title_key
            hk = _title_key(hint["base_name"])
            exact = [p for p in owned if _title_key(p.name) == hk]
            if exact:
                log.info("base disc from patch readme", game=g.get("title"),
                         readme_base=hint["base_name"], matched=exact[0].name)
                ordered = exact
            else:
                named_but_unowned = True
                log.info("readme names a base we don't own", game=g.get("title"),
                         readme_base=hint["base_name"])
        # When the readme names the base and we do not own it, DO NOT fall back to fuzzy
        # title matching over the rest of the library: that is what produced the wrong-disc
        # patches above, and each attempt costs a multi-GB CHD extract. Go straight to
        # sourcing the named disc.
        fallback = [] if named_but_unowned else _dvd_base_candidates(info["base"] or g["title"], owned)
        for base in ordered or fallback:
            if _try_base(base) == "patch":
                return "patch"
        # no owned base matched — source a clean base disc, patch, then discard it
        urls, cookies = _source_base_disc_urls(folder, ra, hint.get("base_name")
                                               or info["base"] or g["title"])
        from app.services.sourcing import _download, _title_key
        if hint.get("base_name"):
            # _source_base_disc_urls falls back to a general archive.org title search, which
            # happily returns anything vaguely similarly named: a Psychonauts TRAILER (.mov),
            # screenshots of "Haunted House (Calvin Overhulser)" for Disney's The Haunted
            # Mansion, even Dangerous Dave (DAVE.EXE / LEVEL05.DD2). Each cost a full multi-GB
            # download before failing. The readme names the base exactly — require a match.
            hk = _title_key(hint["base_name"])
            keep = [u for u in urls if _title_key(Path(unquote(u)).name) == hk] or \
                   [u for u in urls
                    if difflib.SequenceMatcher(
                        None, hk, _title_key(Path(unquote(u)).name)).ratio() >= 0.85]
            if len(keep) != len(urls):
                log.info("filtered sourced base candidates against readme name",
                         game=g.get("title"), readme_base=hint["base_name"],
                         kept=len(keep), of=len(urls))
            urls = keep
        for url in urls:
            # Keep the URL's real extension — these items serve .iso/.chd/.zip/.7z alike, and
            # naming everything .chd made chd_extract choke with "invalid data" on a zip.
            ext = Path(unquote(url).split("?")[0]).suffix.lower()
            dl = stage / ("base_src" + (ext if ext in (".iso", ".chd", ".zip", ".7z", ".rar")
                                        else ".bin"))
            dl.unlink(missing_ok=True)
            if not _download(url, cookies, dl):
                continue
            # NOT tried_any here. Downloading a base is not the same as patching against one:
            # _try_base still has to accept it, and since the readme-md5 gate landed it often
            # does not. Setting the flag on download alone made a rejected base report "patch
            # applied, but the result did not match the RA hash" — blaming the patch for a
            # disc we never patched. _try_base is the single authority on whether a real base
            # was used.
            try:
                # Pass the URL's real filename: the download is staged as "base_src.chd", so
                # without this the name check above can never recognise the readme's base.
                if _try_base(dl, src_name=Path(unquote(url).split("?")[0]).name) == "patch":
                    return "patch"
            finally:
                dl.unlink(missing_ok=True)
        # Nothing worked — record WHICH wall we hit. "no base disc to patch" and "patched but
        # the output didn't match" are completely different problems with different fixes, and
        # collapsing them is how the fairy-tale titles came to report a patch failure when the
        # patch was never applied at all.
        rb = hint.get("base_name")
        if tried_any:
            why = "patch applied, but the result did not match the RA hash"
        elif urls:
            # We DID find and download a base; it just never got past the readme-md5 check.
            # Saying "not available from any configured source" here sent me hunting for a
            # source that was already working.
            why = ("found %d base candidate(s) but none was accepted (readme md5 mismatch)" %
                   len(urls)) + (f" — patch needs {rb}" if rb else "")
        else:
            why = ("base disc not available from any configured source"
                   + (f" (patch needs {rb})" if rb else ""))
        _LAST_PATCH_FAIL[str(gid)] = why
        return None
    finally:
        shutil.rmtree(stage, ignore_errors=True)


# ── CD systems (3DO/PSX/Saturn/Sega CD/PCE-CD/Neo Geo CD/PC-FX/Dreamcast) ──────

_SYNC12 = b"\x00" + b"\xff" * 10 + b"\x00"


def _iso2048_from_bin(binpath: Path, out: Path) -> bool:
    """Cook a raw MODE1/MODE2-2352 .bin into a 2048-byte/sector ISO — the form most disc
    translation xdeltas were authored against. Emits the user data of leading data sectors and
    stops at the first non-data (audio) sector, so multi-track discs yield just the data track."""
    if binpath.stat().st_size % 2352 != 0:
        return False
    wrote = 0
    with open(binpath, "rb") as f, open(out, "wb") as o:
        while True:
            sec = f.read(2352)
            if len(sec) < 2352 or sec[:12] != _SYNC12:
                break
            mode = sec[15]
            if mode == 1:
                o.write(sec[16:16 + 2048])
            elif mode == 2:                            # MODE2/Form1: 16 hdr + 8 subhdr + 2048
                o.write(sec[24:24 + 2048])
            else:
                break
            wrote += 1
    if wrote == 0:
        out.unlink(missing_ok=True)
    return wrote > 0


def _prepare_base_image(base_file: Path, stage: Path) -> Path | None:
    """Normalise a sourced/owned base into a raw single-file disc image (the first data BIN, or
    the ISO as-is). Handles CHD (chdman extractcd), zip/7z archives, and bare iso/bin/cue."""
    with open(base_file, "rb") as f:
        magic = f.read(8)
    if magic[:8] == b"MComprHD":                        # CHD → extractcd → base.cue + base.bin
        cue = stage / "base.cue"
        r = subprocess.run([CHDMAN, "extractcd", "-i", str(base_file), "-o", str(cue), "-f"],
                           capture_output=True, text=True, timeout=3600)
        binp = stage / "base.bin"
        return binp if r.returncode == 0 and binp.exists() else None
    if magic[:2] == b"PK" or magic[:6] == b"7z\xbc\xaf\x27\x1c":   # archive → extract image
        outdir = stage / "unz"
        outdir.mkdir(exist_ok=True)
        subprocess.run([SEVENZ, "e", str(base_file), f"-o{outdir}", "-y"],
                       capture_output=True, timeout=1800)
        imgs = [p for p in outdir.iterdir()
                if p.suffix.lower() in (".iso", ".bin", ".img", ".chd")]
        if not imgs:
            return None
        img = max(imgs, key=lambda p: p.stat().st_size)
        return _prepare_base_image(img, stage) if img.suffix.lower() == ".chd" else img
    return base_file                                   # bare iso/bin (use directly)


def _store_cd(out_img: Path, sector: int, canon: Path, safe: str, cid: int,
              target: str, stage: Path) -> Path | None:
    """Wrap a patched data image in a MODE1 cue, compress to CHD in the canonical library, and
    re-verify by extracting the CHD and rahashing — only a CHD that reproduces the RA hash stays."""
    cue = stage / "store.cue"
    mode = "MODE1/2048" if sector == 2048 else "MODE1/2352"
    cue.write_text(f'FILE "{out_img.name}" BINARY\n  TRACK 01 {mode}\n    INDEX 01 00:00:00\n')
    dest = canon / f"{safe}.chd"
    r = subprocess.run([CHDMAN, "createcd", "-i", str(cue), "-o", str(dest), "-f"],
                       capture_output=True, text=True, timeout=3600)
    if r.returncode != 0 or not dest.exists():
        dest.unlink(missing_ok=True)
        return None
    vcue = stage / "verify.cue"
    r = subprocess.run([CHDMAN, "extractcd", "-i", str(dest), "-o", str(vcue), "-f"],
                       capture_output=True, text=True, timeout=3600)
    if r.returncode == 0 and _rahash_cli(cid, vcue) == target:
        return dest
    dest.unlink(missing_ok=True)                        # CHD round-trip drifted — reject
    return None


def _owned_discs_cd(folder: str) -> list[Path]:
    d = Path(settings.canonical_path) / "roms" / folder
    exts = (".chd", ".cue", ".iso", ".bin")
    return [p for p in d.glob("*") if p.suffix.lower() in exts] if d.is_dir() else []


def _patch_disc_cd(db, sysc, folder: str, g: dict, ra: dict, info: dict) -> str | None:
    cid, gid = sysc.ra_console_id, g["id"]
    md5set = {(h.get("md5") or "").lower() for h in ra.get("hashes", []) if h.get("md5")}
    safe = _safe_name(ra, g)
    canon = Path(settings.canonical_path) / "roms" / folder
    canon.mkdir(parents=True, exist_ok=True)
    stage = _new_stage(gid)
    try:
        xds = _fetch_patch_xdeltas(info["path"], stage)
        if not xds:
            # Silent until now: both disc paths bailed here with no reason, so the Missing page
            # said "this system's patcher recorded no reason" for every one of them.
            _LAST_PATCH_FAIL[str(gid)] = ("%s — %s" % (Path(info["path"]).name,
                                                       _payload_note(info["path"])))
            return None

        def _apply_to_base(base_file: Path) -> str | None:
            """Extract the base to a data image, build the candidate source representations the
            translation may have targeted (2048 ISO and raw 2352 BIN), apply each xdelta, and keep
            the first result that hashes to the RA gate — stored as CHD."""
            img = _prepare_base_image(base_file, stage)
            if not img:
                return None
            reps = []                                  # (source_image, patched_sector_size)
            iso = stage / "base2048.iso"
            iso.unlink(missing_ok=True)
            if _iso2048_from_bin(img, iso):
                reps.append((iso, 2048))
            if img.stat().st_size % 2352 == 0:         # raw BIN as authored-against
                reps.append((img, 2352))
            else:                                      # already a cooked ISO
                reps.append((img, 2048))
            for src_img, sector in reps:
                for xd in xds:
                    out_img = stage / f"out{sector}.img"
                    out_img.unlink(missing_ok=True)
                    r = _xd3_decode(src_img, xd, out_img)
                    if r.returncode != 0 or not out_img.exists():
                        continue
                    got = _rahash_cli(cid, out_img)
                    if got not in md5set:
                        continue                       # wrong base/version — reject
                    dest = _store_cd(out_img, sector, canon, safe, cid, got, stage)
                    if not dest:
                        continue
                    _onboard(db, sysc, folder, g, ra, dest, safe, got)
                    return "patch"
            return None

        for base in _base_candidates(info["base"] or g["title"], _owned_discs_cd(folder)):
            if _apply_to_base(base) == "patch":
                return "patch"
        urls, cookies = _source_base_disc_urls(folder, ra, info["base"] or g["title"])
        from app.services.sourcing import _download
        for url in urls:
            dl = stage / "base.chd"
            dl.unlink(missing_ok=True)
            if not _download(url, cookies, dl):
                continue
            try:
                if _apply_to_base(dl) == "patch":
                    return "patch"
            finally:
                dl.unlink(missing_ok=True)
        need = info["base"] or g["title"]
        _LAST_PATCH_FAIL[str(gid)] = (
            "no base disc reproduced the RA hash — patch needs %s (tried %d source candidate%s)"
            % (need, len(urls), "" if len(urls) == 1 else "s")
            if urls else
            "base disc not available from any configured source (patch needs %s)" % need)
        return None
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _onboard(db, sysc, folder: str, g: dict, ra: dict, dest: Path, safe: str, got: str,
             prov: dict | None = None) -> None:
    """Register a hash-verified patched disc into the canonical library as OWNED.

    `prov` is the rebuild recipe (patch url + base identity); see RomFile.provenance.
    """
    hit = {"ra_game_id": g["id"], "title": g["title"], "hash_name": safe,
           "num_achievements": ra.get("num_achievements", 0), "points": ra.get("points", 0)}
    game = _get_or_create_game(db, hit, sysc.ra_console_id)
    _upsert_rom(db, dest, folder, sysc.hash_method, got, "matched", safe, game.id, {"ra": safe},
                None, prov)
    db.commit()
