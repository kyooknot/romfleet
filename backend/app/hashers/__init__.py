"""
Hasher module.

Each system family has a hasher that:
  1. Accepts a file path (may be .zip or .chd)
  2. Extracts ROM bytes as needed
  3. Returns the MD5 hash RA computes for that ROM

Usage:
    from app.hashers import get_hasher
    md5 = await get_hasher("psx_chd").hash_file("/roms/psx/Crash Bandicoot.chd")
"""

from __future__ import annotations
import hashlib, zipfile, asyncio, functools, tempfile, os, struct, re
from abc import ABC, abstractmethod
from pathlib import Path

# ── Tool resolution ───────────────────────────────────────────────────────────
# This module shells out to rahash / chdman / chd_extract / 7z by BARE NAME in ~40 places.
# romfleet-api runs under systemd with PATH=/usr/local/sbin:/usr/local/bin:... so they resolve
# there — but any other invocation (a manual run, cron, `pct exec`) gets the leaner
# /sbin:/bin:/usr/sbin:/usr/bin, and every locally-built tool in /usr/local/bin (rahash,
# chd_extract, chd_extract_cd, xdelta3n) vanishes with ENOENT.
#
# That is not hypothetical: a 15-hour sweep produced 122 "[Errno 2] No such file or directory:
# 'rahash'" failures, which the caller could only report as "hasher errored". Correctly
# downloaded discs — including the exact Wall-E (USA) (Rev 2) dump RA wants — were never
# hashed. Normalising PATH once here fixes every call site at once, including any added later,
# which per-call-site edits would not.
for _d in ("/usr/local/bin", "/usr/local/sbin", "/usr/games"):
    if _d not in os.environ.get("PATH", "").split(os.pathsep) and os.path.isdir(_d):
        os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")

# ── Helpers ───────────────────────────────────────────────────────────────────

def _md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()

def _read_zip(filepath: str, extensions: list[str]) -> bytes:
    import subprocess as _sp7, tempfile as _tmp7, shutil as _sh7, os as _os7
    SKIP_EXTS = {'.txt', '.nfo', '.jpg', '.png', '.gif', '.xml', '.dat',
                 '.cue', '.gdi', '.m3u', '.sfv', '.md5', '.sha1'}
    try:
        with zipfile.ZipFile(filepath) as zf:
            names = [n for n in zf.namelist() if not n.endswith('/')]
            for name in names:
                if any(name.lower().endswith(ext) for ext in extensions):
                    return zf.read(name)
            for name in names:
                if not any(name.lower().endswith(ext) for ext in SKIP_EXTS):
                    return zf.read(name)
        raise ValueError(f'No matching ROM in ZIP: {filepath}')
    except zipfile.BadZipFile:
        tmp_dir = _tmp7.mkdtemp()
        try:
            _sp7.run(['7z', 'e', filepath, '-o' + tmp_dir, '-y'], capture_output=True, timeout=60)
            candidates = [f for f in _os7.listdir(tmp_dir)
                         if not any(f.lower().endswith(e) for e in SKIP_EXTS)]
            if extensions:
                preferred = [f for f in candidates if any(f.lower().endswith(e) for e in extensions)]
                candidates = preferred if preferred else candidates
            if not candidates:
                raise ValueError(f'No matching ROM in 7z archive: {filepath}')
            with open(_os7.path.join(tmp_dir, candidates[0]), 'rb') as f:
                return f.read()
        finally:
            _sh7.rmtree(tmp_dir, ignore_errors=True)
def _normalise_n64(data: bytes) -> bytes:
    if len(data) < 4: return data
    magic = data[:4]
    if magic == b"\x80\x37\x12\x40": return data
    if magic == b"\x37\x80\x40\x12":
        arr = bytearray(data)
        for i in range(0, len(arr)-1, 2): arr[i], arr[i+1] = arr[i+1], arr[i]
        return bytes(arr)
    if magic == b"\x40\x12\x37\x80":
        arr = bytearray(data)
        for i in range(0, len(arr)-3, 4): arr[i],arr[i+1],arr[i+2],arr[i+3] = arr[i+3],arr[i+2],arr[i+1],arr[i]
        return bytes(arr)
    return data


def _chd_codec_tag(filepath: str) -> str:
    """Read the primary compression codec tag from a CHD v5 header (4 ASCII bytes at
    offset 16), e.g. 'zstd', 'lzma', 'cdzs', 'cdlz'. Returns '' if unreadable."""
    try:
        with open(filepath, 'rb') as f:
            f.seek(16)
            tag = f.read(4)
        return tag.decode('ascii')
    except (OSError, UnicodeDecodeError):
        return ''


async def _rahash_chd(filepath: str, console_id: int) -> str:
    """
    Hash a CHD by extracting via chdman then hashing with rahash.
    Detects CHD type from metadata:
      - CD/optical (CHT2 metadata) → chdman extractcd → CUE/BIN
      - Hard disk (DVD/GDDD metadata) → chdman extracthd → raw IMG
    """
    import subprocess as _sp, shutil as _sh, tempfile as _tmp, os as _os, uuid as _uuid

    # Detect CHD type from metadata tag
    info = _sp.run(['chdman', 'info', '-i', filepath],
                   capture_output=True, text=True, timeout=30)
    if info.returncode != 0:
        # The packaged chdman (0.251) can't decode this CHD's codec — notably `zstd`,
        # which was added to chdman ~0.264, and the CD-frame codecs (`cdzs`/`cdlz`/...)
        # it never supported at all. Fall back to the libchdr-based extractors, which
        # branch on the codec tag: raw/DVD-type (zstd/lzma/zlib — concatenated hunks ==
        # the disc image) use `chd_extract`; CD-frame codecs (2352B sector + 96B subcode
        # per frame) use `chd_extract_cd`, which de-interleaves the subcode and writes a
        # flat single-track .bin that rcheevos' default cdreader auto-detects.
        return await _rahash_chd_libchdr(filepath, console_id)
    is_hd = any(tag in info.stdout for tag in ("Tag='DVD '", "Tag='GDDD'", "Tag='HARD'"))

    tmp_dir = _os.path.join('/rahash_tmp', f'chd_{_uuid.uuid4().hex}')
    _os.makedirs(tmp_dir, exist_ok=True)
    try:
        if is_hd:
            # Hard disk CHD — extract as raw image
            img_path = _os.path.join(tmp_dir, 'disc.img')
            r = _sp.run(['chdman', 'extracthd', '-i', filepath, '-o', img_path],
                        capture_output=True, text=True, timeout=1800)
            if r.returncode != 0:
                raise RuntimeError(f'chdman extracthd failed: {r.stderr.strip()}')
            hash_target = img_path
        else:
            # CD/optical CHD — extract as CUE/BIN
            cue_path = _os.path.join(tmp_dir, 'disc.cue')
            r = _sp.run(['chdman', 'extractcd', '-i', filepath, '-o', cue_path],
                        capture_output=True, text=True, timeout=1800)
            if r.returncode != 0:
                raise RuntimeError(f'chdman extractcd failed: {r.stderr.strip()}')
            hash_target = cue_path

        r2 = _sp.run(['rahash', str(console_id), hash_target],
                     capture_output=True, text=True, timeout=120)
        if r2.returncode == 0 and r2.stdout.strip():
            return r2.stdout.strip()
        raise RuntimeError(f'rahash failed on extracted CHD: {r2.stderr.strip()}')
    finally:
        _sh.rmtree(tmp_dir, ignore_errors=True)


async def _rahash_chd_libchdr(filepath: str, console_id: int) -> str:
    """Extract a CHD with a libchdr-based tool, then rahash the result.

    Used when the packaged chdman is too old to decode the CHD codec. Branches on the
    CHD's codec tag (offset 16 of the header):
      - raw/DVD-type (zstd/lzma/zlib, no CD framing) → `chd_extract`: concatenated
        decompressed hunks equal the raw disc image (PS2/GameCube/Wii DVD CHDs).
      - CD-frame codecs (cdzs/cdlz/cdzl/cdfl/cdgz, 2352B sector + 96B subcode per
        frame) → `chd_extract_cd`: de-interleaves the subcode and writes a flat
        single-track .bin (rcheevos' default cdreader auto-detects sector size/header
        for a non-.cue/.gdi file with track <= 1, so no synthesized .cue is needed).
    """
    import subprocess as _sp, shutil as _sh, os as _os, uuid as _uuid

    tmp_dir = _os.path.join('/rahash_tmp', f'lchd_{_uuid.uuid4().hex}')
    _os.makedirs(tmp_dir, exist_ok=True)
    is_cd_frame = _chd_codec_tag(filepath).startswith('cd')
    try:
        if is_cd_frame:
            img = _os.path.join(tmp_dir, 'disc.bin')
            r = _sp.run(['chd_extract_cd', filepath, img],
                        capture_output=True, text=True, timeout=1800)
            if r.returncode != 0:
                raise RuntimeError(f'chd_extract_cd failed: {r.stderr.strip()}')
        else:
            img = _os.path.join(tmp_dir, 'disc.img')
            r = _sp.run(['chd_extract', filepath, img],
                        capture_output=True, text=True, timeout=1800)
            if r.returncode != 0:
                raise RuntimeError(f'chd_extract failed: {r.stderr.strip()}')
        r2 = _sp.run(['rahash', str(console_id), img],
                     capture_output=True, text=True, timeout=180)
        out = r2.stdout.strip()
        if r2.returncode == 0 and len(out) == 32 and all(c in '0123456789abcdef' for c in out.lower()):
            return out
        raise RuntimeError(f'rahash failed on libchdr-extracted image: {r2.stderr.strip() or out}')
    finally:
        _sh.rmtree(tmp_dir, ignore_errors=True)


class BaseHasher(ABC):
    """Every hasher is `async def`, but none of them actually awaits anything asynchronous:
    the work is chdman/rahash/7z subprocesses and file reads, run inline. On the event loop that
    means a multi-GB CHD extract blocks the whole API for minutes — /api/systems stops answering,
    the romfleet-health watchdog scores it dead and restarts the service, and any in-flight
    multi-GB download dies with it (Onimusha, killed at 3.3GB, curl_exit=-15 on 2026-08-05).

    Rather than thread 48 individual subprocess.run calls, offload at the boundary: each
    subclass's hash_file is wrapped so the real coroutine runs to completion on a worker thread
    with its own event loop. Safe because the hashers touch no loop-bound state — no aiohttp,
    httpx, Elasticsearch, redis, locks or queues; every internal `await` just chains to another
    hasher coroutine, which runs on that same worker loop.
    """

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        impl = cls.__dict__.get("hash_file")
        if impl is None or getattr(impl, "_offloaded", False):
            return

        @functools.wraps(impl)
        async def _offload(self, filepath: str, _impl=impl) -> str:
            return await asyncio.to_thread(lambda: asyncio.run(_impl(self, filepath)))

        _offload._offloaded = True
        cls.hash_file = _offload

    @abstractmethod
    async def hash_file(self, filepath: str) -> str:
        pass

# ── Cartridge hashers ─────────────────────────────────────────────────────────

class RawHasher(BaseHasher):
    EXTENSIONS = [".md",".gen",".sms",".gg",".gba",".gb",".gbc",".bin",".a26",
                  ".a78",".lnx",".lyx",".ngp",".ngc",".pce",".vb",".ws",".wsc",".col",
                  ".32x",".sg",".sv",".j64",".jag",".vec",".min",".ws",".wsc",
                  ".nes",".fds",".sfc",".smc",".n64",".z64",".v64",".nds",".gba",
                  ".int",".a52",".a78",".k7",".d64",".dsk",".tap",".tzx",".pce"]
    async def hash_file(self, filepath: str) -> str:
        p = Path(filepath)
        if p.suffix.lower() == ".zip":
            try:
                data = _read_zip(filepath, self.EXTENSIONS)
            except Exception:
                # Not a real zip - hash the raw file
                data = p.read_bytes()
        else:
            data = p.read_bytes()
        return _md5(data)

class LynxHasher(BaseHasher):
    """Strip 64-byte 'LYNX' header if present, then MD5 — matches rcheevos rc_hash_lynx."""
    EXTENSIONS = [".lnx", ".lyx", ".o"]
    async def hash_file(self, filepath: str) -> str:
        p = Path(filepath)
        if p.suffix.lower() == ".zip":
            try:
                data = _read_zip(filepath, self.EXTENSIONS)
            except Exception:
                data = p.read_bytes()
        else:
            data = p.read_bytes()
        if data[:4] == b"LYNX":
            data = data[64:]
        return _md5(data)

class NESHasher(BaseHasher):
    EXTENSIONS = [".nes"]
    async def hash_file(self, filepath: str) -> str:
        p = Path(filepath)
        try:
            data = _read_zip(filepath, self.EXTENSIONS) if p.suffix.lower() == ".zip" else p.read_bytes()
        except Exception:
            data = p.read_bytes()
        if data[:4] == b"NES\x1a":
            data = data[16:]
        return _md5(data)

class SNESHasher(BaseHasher):
    EXTENSIONS = [".sfc",".smc",".fig"]
    async def hash_file(self, filepath: str) -> str:
        p = Path(filepath)
        try:
            data = _read_zip(filepath, self.EXTENSIONS) if p.suffix.lower() == ".zip" else p.read_bytes()
        except Exception:
            data = p.read_bytes()
        if len(data) % 1024 == 512:
            data = data[512:]
        return _md5(data)

class N64Hasher(BaseHasher):
    EXTENSIONS = [".z64",".n64",".v64"]
    async def hash_file(self, filepath: str) -> str:
        # Use rahash (rcheevos), not a local byteswap+MD5. rcheevos normalises
        # N64 ROMs (incl. padding non-standard sizes) before hashing — a hand
        # rolled MD5 agrees for stock ROMs but DIVERGES for expanded hacks
        # (e.g. an SM64 hack that grows the ROM), which then never match.
        import subprocess as _sp, tempfile as _t, os as _o, shutil as _sh
        p = Path(filepath)
        if p.suffix.lower() == ".zip":
            data = _read_zip(filepath, self.EXTENSIONS)
            td = _t.mkdtemp()
            try:
                tf = _o.path.join(td, "rom.z64")
                with open(tf, "wb") as f: f.write(data)
                return _rahash_file(tf, 2)
            finally:
                _sh.rmtree(td, ignore_errors=True)
        return _rahash_file(filepath, 2)


def _rahash_file(filepath: str, console_id: int) -> str:
    import subprocess as _sp
    r = _sp.run(["rahash", str(console_id), filepath], capture_output=True, text=True, timeout=180)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    raise RuntimeError(f"rahash {console_id} failed for {filepath}: {r.stderr.strip()}")

def _normalise_n64(data: bytes) -> bytes:
    if len(data) < 4: return data
    magic = data[:4]
    if magic == b"\x80\x37\x12\x40": return data
    if magic == b"\x37\x80\x40\x12":
        arr = bytearray(data)
        for i in range(0, len(arr)-1, 2): arr[i], arr[i+1] = arr[i+1], arr[i]
        return bytes(arr)
    if magic == b"\x40\x12\x37\x80":
        arr = bytearray(data)
        for i in range(0, len(arr)-3, 4): arr[i],arr[i+1],arr[i+2],arr[i+3] = arr[i+3],arr[i+2],arr[i+1],arr[i]
        return bytes(arr)
    return data

class NDSHasher(BaseHasher):
    EXTENSIONS = [".nds"]
    async def hash_file(self, filepath: str) -> str:
        """
        rcheevos rc_hash_nintendo_ds hashes specific ROM sections (ARM9, ARM7,
        icon) — NOT a raw MD5. Handles both DS (console 18) and DSi (console 78).
        For ZIP files, extract the .nds first then pass to rahash.
        """
        import subprocess as _sp, tempfile as _tmp, os as _os
        p = Path(filepath)

        if p.suffix.lower() == ".zip":
            try:
                nds_data = _read_zip(filepath, self.EXTENSIONS)
            except Exception:
                nds_data = p.read_bytes()
            fd, tmp = _tmp.mkstemp(suffix=".nds")
            try:
                _os.write(fd, nds_data)
                _os.close(fd)
                r = _sp.run(["rahash", "18", tmp], capture_output=True, text=True, timeout=30)
                if r.returncode == 0 and r.stdout.strip():
                    return r.stdout.strip()
                raise RuntimeError(f"rahash failed on extracted NDS: {r.stderr.strip()}")
            finally:
                if _os.path.exists(tmp):
                    _os.unlink(tmp)
        else:
            r = _sp.run(["rahash", "18", filepath], capture_output=True, text=True, timeout=30)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
            raise RuntimeError(f"rahash failed: {r.stderr.strip()}")

# ── PS1 hasher — RA spec ──────────────────────────────────────────────────────
#
# RA hashes PS1 games by:
#   1. Reading the first data track (Track 01) BIN
#   2. Locating SYSTEM.CNF in the ISO 9660 filesystem
#   3. Parsing SYSTEM.CNF to find the boot executable path (e.g. cdrom:\SLUS_123.45;1)
#   4. Locating that executable in the ISO filesystem
#   5. MD5 of: executable filename (uppercase, no path/version) + executable file contents
#
# Reference: https://github.com/RetroAchievements/rcheevos (src/rhash/cdreader.c)

class PSXHasher(BaseHasher):
    async def hash_file(self, filepath: str) -> str:
        import subprocess as _sp
        p = Path(filepath)
        if p.suffix.lower() == ".chd":
            return await _rahash_chd(filepath, 12)
        elif p.suffix.lower() in (".bin", ".cue", ".iso"):
            result = _sp.run(["rahash", "12", filepath], capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout.strip()
            raise RuntimeError(f"rahash failed: {result.stderr}")
        elif p.suffix.lower() in _ARCHIVE_EXTS:
            return await _hash_archive_disc(filepath, 12)
        else:
            raise ValueError(f"Unsupported PSX format: {p.suffix}")

# ── PlayStation 2 ─────────────────────────────────────────────────────────────
# rc_hash_ps2 reads the boot executable path from SYSTEM.CNF and hashes it,
# same approach as PSX but with its own console ID (21).

class PS2Hasher(BaseHasher):
    async def hash_file(self, filepath: str) -> str:
        import subprocess as _sp
        p = Path(filepath)
        if p.suffix.lower() == ".chd":
            return await _rahash_chd(filepath, 21)
        elif p.suffix.lower() in (".bin", ".cue", ".iso"):
            result = _sp.run(["rahash", "21", filepath], capture_output=True, text=True, timeout=60)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            raise RuntimeError(f"rahash failed: {result.stderr.strip()}")
        elif p.suffix.lower() in _ARCHIVE_EXTS:
            return await _hash_archive_disc(filepath, 21)
        else:
            raise ValueError(f"Unsupported PS2 format: {p.suffix}")

# ── Saturn hasher ─────────────────────────────────────────────────────────────
#
# RA hashes Saturn games by:
#   MD5 of the first 512KB of the first data track
#   (the security ring / IP.BIN header region)

class SaturnHasher(BaseHasher):
    async def hash_file(self, filepath: str) -> str:
        p = Path(filepath)
        if p.suffix.lower() == ".chd":
            return await _rahash_chd(filepath, 39)
        elif p.suffix.lower() in (".bin", ".cue", ".iso"):
            import subprocess as _sp
            result = _sp.run(["rahash", str(39), filepath], capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout.strip()
            raise RuntimeError(f"rahash failed: {result.stderr}")
        elif p.suffix.lower() in _ARCHIVE_EXTS:
            return await _hash_archive_disc(filepath, 39)
        else:
            raise ValueError(f"Unsupported format for SaturnHasher: {p.suffix}")


class SegaCDHasher(BaseHasher):
    """Sega CD / Mega-CD (RA console 9). RA hashes MD5 of the first 512 BYTES of sector 0
    (the volume + ROM header, magic 'SEGADISCSYSTEM  ') of track 1 — see rcheevos
    rc_hash_sega_cd. It's the SAME function RA uses for Saturn, but we call it with the
    correct console id (9) so multi-disc (.m3u) dispatch and logging stay right."""
    async def hash_file(self, filepath: str) -> str:
        p = Path(filepath)
        if p.suffix.lower() == ".chd":
            return await _rahash_chd(filepath, 9)
        elif p.suffix.lower() in (".bin", ".cue", ".iso"):
            import subprocess as _sp
            result = _sp.run(["rahash", str(9), filepath], capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout.strip()
            raise RuntimeError(f"rahash failed: {result.stderr}")
        elif p.suffix.lower() in _ARCHIVE_EXTS:
            return await _hash_archive_disc(filepath, 9)
        else:
            raise ValueError(f"Unsupported format for SegaCDHasher: {p.suffix}")
# ── Wii / GameCube (stub) ─────────────────────────────────────────────────────


async def _rvz_to_iso_hash(filepath: str, console_id: int) -> str:
    """Convert RVZ to a temp ISO via dolphin-tool, hash it, then discard the ISO."""
    import subprocess as _sp, os as _os, uuid as _uuid
    tmp = f'/rahash_tmp/rvz_{_uuid.uuid4().hex}.iso'
    try:
        r = _sp.run(
            ['/usr/games/dolphin-tool', 'convert', '-i', filepath, '-o', tmp, '-f', 'iso'],
            capture_output=True, text=True, timeout=1800,
        )
        if r.returncode != 0 or not _os.path.exists(tmp):
            raise RuntimeError(f"dolphin-tool conversion failed: {r.stderr.strip()}")
        r2 = _sp.run(['rahash', str(console_id), tmp], capture_output=True, text=True, timeout=120)
        if r2.returncode == 0 and r2.stdout.strip():
            return r2.stdout.strip()
        raise RuntimeError(f"rahash failed on converted ISO: {r2.stderr.strip()}")
    finally:
        if _os.path.exists(tmp):
            _os.unlink(tmp)


class WiiHasher(BaseHasher):
    async def hash_file(self, filepath: str) -> str:
        import subprocess as _sp, shutil as _sh, os as _os
        p = Path(filepath)
        if p.name.startswith('._'):
            raise ValueError(f"Skipping macOS metadata file: {filepath}")
        if p.suffix.lower() in ('.rvz', '.iso', '.wad'):
            # dolphin-tool normalises the image so rcheevos partition offsets resolve correctly
            return await _rvz_to_iso_hash(filepath, 19)
        if p.suffix.lower() == '.chd':
            return await _rahash_chd(filepath, 19)
        if p.suffix.lower() in ('.zip', '.7z'):
            extracted = await _extract_disc_archive(filepath)
            tmp_dir = _os.path.dirname(extracted)
            try:
                ext = Path(extracted).suffix.lower()
                if ext in ('.rvz', '.iso', '.wad'):
                    return await _rvz_to_iso_hash(extracted, 19)
                if ext == '.chd':
                    return await _rahash_chd(extracted, 19)
                result = _sp.run(['rahash', '19', extracted], capture_output=True, text=True, timeout=30)
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
                raise RuntimeError(f"rahash failed for {filepath}: {result.stderr.strip()}")
            finally:
                _sh.rmtree(tmp_dir, ignore_errors=True)
        result = _sp.run(['rahash', '19', filepath], capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        raise RuntimeError(f"rahash failed for {filepath}: {result.stderr.strip()}")


# ── Arcade (MAME) — hash entire ZIP file ─────────────────────────────────────
class ArcadeHasher(BaseHasher):
    async def hash_file(self, filepath: str) -> str:
        import subprocess as _sp
        p = Path(filepath)
        if p.name.startswith('._'):
            raise ValueError(f"Skipping macOS metadata file: {filepath}")
        result = _sp.run(['rahash', '27', filepath], capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        raise RuntimeError(f"rahash failed for {filepath}: {result.stderr.strip()}")


# ── PC-8000/8800 ──────────────────────────────────────────────────────────────
class PC88Hasher(BaseHasher):
    async def hash_file(self, filepath: str) -> str:
        import subprocess as _sp, zipfile as _zf, tempfile as _tmp, os as _os
        p = Path(filepath)
        if p.name.startswith("._"):
            raise ValueError(f"Skipping macOS metadata file: {filepath}")
        # Extensions rahash 47 (PC-88) can handle
        _PC88_EXTS = (".d88", ".d77", ".88d", ".cpm", ".t88", ".cmt")
        # Extensions that exist in the wild but aren't supported by rahash
        _PC88_UNSUPPORTED = (".n80", ".n80e", ".n88", ".raw")

        if p.suffix.lower() in (".zip", ".7z"):
            # Handle both ZIP and 7z (some .zip files are actually 7z)
            tmp_path = None
            try:
                try:
                    with _zf.ZipFile(filepath) as z:
                        inner = [n for n in z.namelist()
                                 if n.lower().endswith(_PC88_EXTS) and not n.startswith("._")]
                        if not inner:
                            # Check if it contains a known-unsupported format before erroring
                            unsup = [n for n in z.namelist()
                                     if n.lower().endswith(_PC88_UNSUPPORTED) and not n.startswith("._")]
                            if unsup or not z.namelist():
                                raise NotImplementedError(
                                    f"Unsupported PC-88 format in ZIP: {filepath}")
                            raise RuntimeError(f"No supported PC-88 disk image in ZIP: {filepath}")
                        with _tmp.NamedTemporaryFile(suffix=Path(inner[0]).suffix, delete=False) as tf:
                            tf.write(z.read(inner[0]))
                            tmp_path = tf.name
                except (_zf.BadZipFile, NotImplementedError):
                    raise
                except Exception:
                    # Try 7z
                    import shutil as _sh
                    tmp_dir = _tmp.mkdtemp()
                    try:
                        r = _sp.run(["7z", "e", filepath, "-o" + tmp_dir, "-y"], capture_output=True, timeout=30)
                        d88s = [f for f in _os.listdir(tmp_dir)
                                if f.lower().endswith(_PC88_EXTS)]
                        if not d88s:
                            unsup = [f for f in _os.listdir(tmp_dir)
                                     if f.lower().endswith(_PC88_UNSUPPORTED)]
                            if unsup:
                                raise NotImplementedError(
                                    f"Unsupported PC-88 format in archive: {filepath}")
                            raise RuntimeError(f"No supported PC-88 disk image in archive: {filepath}")
                        tmp_path = _os.path.join(tmp_dir, d88s[0])
                        result = _sp.run(["rahash", "47", tmp_path], capture_output=True, text=True, timeout=30)
                        if result.returncode == 0 and result.stdout.strip():
                            return result.stdout.strip()
                        raise RuntimeError(f"rahash failed: {result.stderr.strip()}")
                    finally:
                        _sh.rmtree(tmp_dir, ignore_errors=True)
                result = _sp.run(["rahash", "47", tmp_path], capture_output=True, text=True, timeout=30)
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
                raise RuntimeError(f"rahash failed: {result.stderr.strip()}")
            finally:
                if tmp_path and _os.path.exists(tmp_path):
                    _os.unlink(tmp_path)
        result = _sp.run(["rahash", "47", filepath], capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        raise RuntimeError(f"rahash failed for {filepath}: {result.stderr.strip()}")


# ── CSO (CISO) decompression ──────────────────────────────────────────────────
async def _cso_to_iso(filepath: str) -> str:
    """
    Decompress a CSO/CISO to a temp ISO and return its path (caller unlinks).
    Bare rcheevos cannot parse CSO — the standard compressed PSP format — so
    PSP hashing must materialise the real ISO first. Uses maxcso when present
    (fast, threaded); otherwise a pure-Python CISO v1 reader.
    """
    import subprocess as _sp, shutil as _sh, os as _os, struct as _st, zlib as _zl, uuid as _uuid
    out = f'/rahash_tmp/cso_{_uuid.uuid4().hex}.iso'
    if _sh.which('maxcso'):
        r = _sp.run(['maxcso', '--decompress', filepath, '-o', out],
                    capture_output=True, text=True, timeout=900)
        if r.returncode == 0 and _os.path.exists(out) and _os.path.getsize(out) > 0:
            return out
        if _os.path.exists(out):
            _os.unlink(out)  # fall through to the Python reader
    try:
        with open(filepath, 'rb') as f, open(out, 'wb') as o:
            hdr = f.read(24)
            if len(hdr) < 24 or hdr[:4] != b'CISO':
                raise ValueError(f'Not a CISO image: {filepath}')
            total_bytes, block_size, _ver, align = _st.unpack('<QIBB', hdr[8:22])
            if not total_bytes or not block_size:
                raise ValueError(f'Corrupt CISO header: {filepath}')
            n_blocks = (total_bytes + block_size - 1) // block_size
            idx = _st.unpack(f'<{n_blocks + 1}I', f.read(4 * (n_blocks + 1)))
            remaining = total_bytes
            for i in range(n_blocks):
                start = (idx[i] & 0x7FFFFFFF) << align
                end = (idx[i + 1] & 0x7FFFFFFF) << align
                clen = end - start
                want = min(block_size, remaining)
                f.seek(start)
                # Stored uncompressed when the v1 high-bit flag is set OR (CISO v2)
                # the block's stored length is >= block_size, i.e. maxcso kept it raw
                # because deflate did not shrink it. Treating such v2 blocks as
                # deflate is what raised zlib "incorrect header check" before.
                if (idx[i] & 0x80000000) or clen >= block_size:
                    data = f.read(want)
                else:
                    raw = f.read(clen)
                    try:
                        data = _zl.decompress(raw, -15)   # raw deflate (standard)
                    except _zl.error:
                        data = _zl.decompress(raw)        # zlib-wrapped variant
                o.write(data[:want])
                remaining -= want
        return out
    except Exception:
        if _os.path.exists(out):
            _os.unlink(out)
        raise


# ── PSP ───────────────────────────────────────────────────────────────────────
class PSPHasher(BaseHasher):
    async def hash_file(self, filepath: str) -> str:
        import subprocess as _sp, shutil as _sh, os as _os

        async def _hash_iso_or_cso(path: str, label: str) -> str:
            iso = path
            is_cso = Path(path).suffix.lower() == ".cso"
            if is_cso:
                iso = await _cso_to_iso(path)
            try:
                result = _sp.run(["rahash", "41", iso], capture_output=True, text=True, timeout=120)
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
                raise RuntimeError(f"rahash failed for {label}: {result.stderr.strip()}")
            finally:
                if is_cso and _os.path.exists(iso):
                    _os.unlink(iso)

        p = Path(filepath)
        if p.suffix.lower() == ".chd":
            return await _rahash_chd(filepath, 41)
        if p.suffix.lower() in (".zip", ".7z"):
            extracted = await _extract_disc_archive(filepath)
            tmp_dir = _os.path.dirname(extracted)
            try:
                return await _hash_iso_or_cso(extracted, filepath)
            finally:
                _sh.rmtree(tmp_dir, ignore_errors=True)
        return await _hash_iso_or_cso(filepath, filepath)


# ── Archive extraction helper (GameCube / Wii) ────────────────────────────────
_DISC_ROM_EXTS = ('.rvz', '.iso', '.gcm', '.img', '.wbfs', '.cso',
                  '.chd', '.cue', '.bin', '.gdi', '.mdf', '.ccd')
# Archive wrappers the sourcer routinely encounters. 7z here is built with the Rar and Rar5
# codecs, so one tool covers all three.
_ARCHIVE_EXTS = ('.zip', '.7z', '.rar')
# Preference when an archive holds several files. A whole-disc container wins; a .cue beats the
# .bin it describes, because handing rahash the raw track loses the layout it needs to read.
_DISC_PREF = ('.chd', '.rvz', '.iso', '.gcm', '.wbfs', '.cso', '.gdi',
              '.cue', '.mdf', '.ccd', '.img', '.bin',
              # floppy images, so an archive holding .dsk/.d88 picks a disk rather than
              # whatever happened to sort first (a readme, an .m3u playlist)
              '.dsk', '.d88', '.woz', '.do', '.po', '.2mg', '.di1', '.fdi')


def _disc_rank(name: str) -> int:
    n = (name or "").lower()
    for i, e in enumerate(_DISC_PREF):
        if n.endswith(e):
            return i
    return len(_DISC_PREF)


async def _hash_archive_disc(filepath: str, console_id: int) -> str:
    """Unpack an archive-wrapped disc and hash the image inside.

    Sources hand back whatever they host, and archive-wrapped discs are common: .rar for PS2,
    .7z for PSX, .zip for Sega CD homebrew. Every disc hasher that lacked this branch raised
    "Unsupported format: .rar", which the sourcer recorded as a hasher/toolchain error -- so the
    Missing page reported a toolchain problem for what was actually a perfectly good dump in a
    wrapper nobody had taught it to open.
    """
    import os as _os, shutil as _sh, subprocess as _sp
    extracted = await _extract_disc_archive(filepath)
    tmp_dir = _os.path.dirname(extracted)
    try:
        if Path(extracted).suffix.lower() == ".chd":
            return await _rahash_chd(extracted, console_id)
        r = _sp.run(["rahash", str(console_id), extracted],
                    capture_output=True, text=True, timeout=900)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
        raise RuntimeError(f"rahash failed: {(r.stderr or '').strip()}")
    finally:
        _sh.rmtree(tmp_dir, ignore_errors=True)

async def _extract_disc_archive(filepath: str) -> str:
    """Extract a disc image from a .zip or .7z archive to a temp file.
    Returns path to extracted file; caller is responsible for cleanup.
    Archives in this collection contain a single .rvz/.iso/.gcm."""
    import subprocess as _sp, zipfile as _zf, shutil as _sh, tempfile as _tmp, os as _os, uuid as _uuid
    p = Path(filepath)
    tmp_dir = _os.path.join('/rahash_tmp', f'disc_{_uuid.uuid4().hex}')
    _os.makedirs(tmp_dir, exist_ok=True)
    try:
        if p.suffix.lower() == '.zip':
            try:
                with _zf.ZipFile(filepath) as zf:
                    members = [n for n in zf.namelist() if not n.endswith('/')]
                    preferred = sorted([n for n in members if any(
                        n.lower().endswith(e) for e in _DISC_ROM_EXTS)], key=_disc_rank)
                    target = preferred[0] if preferred else (members[0] if members else None)
                    if not target:
                        raise RuntimeError(f"Empty archive: {filepath}")
                    # Extract EVERY member, flattened, exactly as the `7z e` branch below does.
                    # Extracting only the preferred member orphans a .cue/.gdi from the tracks
                    # it references by name, and rahash then fails on a perfectly good dump:
                    # Propeller Arena (USA) (Proto).zip is 3x .bin + .cue, and the .cue arrived
                    # alone, so a registered source that HAD the game reported "none was a valid
                    # disc image for this console" (2026-08-17). Affects every zip-wrapped
                    # multi-track disc set, on every disc system.
                    for n in members:
                        base = _os.path.basename(n)
                        if not base:
                            continue
                        with zf.open(n) as src, open(_os.path.join(tmp_dir, base), "wb") as dst:
                            _sh.copyfileobj(src, dst)
                    extracted = _os.path.join(tmp_dir, _os.path.basename(target))
                    if not _os.path.exists(extracted):
                        raise RuntimeError(f"extraction produced no {target}")
                    return extracted
            except _zf.BadZipFile:
                pass  # fall through to 7z
        r = _sp.run(['7z', 'e', filepath, '-o' + tmp_dir, '-y'], capture_output=True, timeout=1800)
        candidates = sorted([f for f in _os.listdir(tmp_dir) if any(
            f.lower().endswith(e) for e in _DISC_ROM_EXTS)], key=_disc_rank)
        if not candidates:
            candidates = [f for f in _os.listdir(tmp_dir) if _os.path.isfile(_os.path.join(tmp_dir, f))]
        if not candidates:
            raise RuntimeError(f"No disc image found in archive: {filepath}")
        # `7z e` flattens every member into tmp_dir, so a cue/bin pair arrives intact and the
        # .cue we return still finds its track alongside it.
        return _os.path.join(tmp_dir, candidates[0])
    except Exception:
        _sh.rmtree(tmp_dir, ignore_errors=True)
        raise


# ── GameCube ──────────────────────────────────────────────────────────────────
class GameCubeHasher(BaseHasher):
    async def hash_file(self, filepath: str) -> str:
        import subprocess as _sp, shutil as _sh, os as _os
        p = Path(filepath)
        if p.name.startswith("._"):
            raise ValueError(f"Skipping macOS metadata file: {filepath}")
        if p.suffix.lower() == ".rvz":
            return await _rvz_to_iso_hash(filepath, 16)
        if p.suffix.lower() == ".chd":
            return await _rahash_chd(filepath, 16)
        if p.suffix.lower() in (".zip", ".7z"):
            extracted = await _extract_disc_archive(filepath)
            tmp_dir = _os.path.dirname(extracted)
            try:
                if Path(extracted).suffix.lower() == '.rvz':
                    return await _rvz_to_iso_hash(extracted, 16)
                result = _sp.run(["rahash", "16", extracted], capture_output=True, text=True, timeout=120)
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
                raise RuntimeError(f"rahash failed for {filepath}: {result.stderr.strip()}")
            finally:
                _sh.rmtree(tmp_dir, ignore_errors=True)
        result = _sp.run(["rahash", "16", filepath], capture_output=True, text=True, timeout=120)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        raise RuntimeError(f"rahash failed for {filepath}: {result.stderr.strip()}")

# ── Arduboy hasher ────────────────────────────────────────────────────────────
#
# RA spec (rcheevos rc_hash_arduboy / rc_hash_arduboyfx):
#   .hex / .zip  → rc_hash_text: normalize line endings to \n, MD5 the text
#   .arduboy     → rc_hash_arduboyfx: for each .hex/.bin in ZIP, build
#                  (lowercase_name\0 + CRC32_LE32 + decomp_size_LE64), sort by
#                  that data, MD5 of concatenation

class ArduBoyHasher(BaseHasher):
    async def hash_file(self, filepath: str) -> str:
        p = Path(filepath)
        suffix = p.suffix.lower()
        if suffix == ".hex":
            return self._hash_text(p.read_bytes())
        if suffix == ".zip":
            with zipfile.ZipFile(filepath) as z:
                hex_names = [n for n in z.namelist() if n.lower().endswith(".hex")]
                if not hex_names:
                    raise ValueError(f"No .hex file in ZIP: {filepath}")
                return self._hash_text(z.read(hex_names[0]))
        if suffix == ".arduboy":
            return self._hash_arduboyfx(filepath)
        raise ValueError(f"Unsupported Arduboy extension: {p.suffix}")

    @staticmethod
    def _hash_text(data: bytes) -> str:
        # Mirrors rcheevos rc_hash_text: each line hashed as-is + normalized \n
        md5 = hashlib.md5(usedforsecurity=False)
        i = 0
        while True:
            j = i
            while j < len(data) and data[j] != 0x0D and data[j] != 0x0A:
                j += 1
            md5.update(data[i:j])
            md5.update(b"\n")
            if j < len(data) and data[j] == 0x0D:
                j += 1
            if j < len(data) and data[j] == 0x0A:
                j += 1
            i = j
            if i >= len(data):
                break
        return md5.hexdigest()

    @staticmethod
    def _hash_arduboyfx(filepath: str) -> str:
        # Mirrors rc_hash_arduboyfx: hash ZIP metadata for .hex/.bin entries
        # Entry format: lowercase_name\0 + CRC32_LE32 + decomp_size_LE64
        import struct as _st
        with zipfile.ZipFile(filepath) as z:
            entries = []
            for info in z.infolist():
                name = info.filename
                if not (name.lower().endswith(".hex") or name.lower().endswith(".bin")):
                    continue
                norm = name.replace("\\", "/").lower()
                entry = norm.encode("utf-8") + b"\x00"
                entry += _st.pack("<I", info.CRC & 0xFFFFFFFF)
                entry += _st.pack("<Q", info.file_size)
                entries.append(entry)
            if not entries:
                raise ValueError(f"No .hex or .bin files in .arduboy: {filepath}")
            entries.sort()
            md5 = hashlib.md5(usedforsecurity=False)
            for e in entries:
                md5.update(e)
            return md5.hexdigest()


# ── Registry ──────────────────────────────────────────────────────────────────

_HASHER_MAP: dict[str, BaseHasher] = {
    "raw":         RawHasher(),
    "lynx":        LynxHasher(),
    "nes":         NESHasher(),
    "snes":        SNESHasher(),
    "n64":         N64Hasher(),
    "nds":         NDSHasher(),
    "psx_chd":     PSXHasher(),
    "psx_cue":     PSXHasher(),
    "ps2":          PS2Hasher(),
    "saturn_chd":  SaturnHasher(),
    "segacd_chd":  SegaCDHasher(),
    "wii_chd":     WiiHasher(),
    "wii":         WiiHasher(),
    "arcade":      ArcadeHasher(),
    "pc88":         PC88Hasher(),
    "psp":          PSPHasher(),
    "gamecube":     GameCubeHasher(),
    "arduboy":      ArduBoyHasher(),
}

def get_hasher(hash_method: str) -> BaseHasher:
    h = _HASHER_MAP.get(hash_method)
    if not h:
        raise ValueError(f"Unknown hash method: {hash_method}")
    return h

# ── Dreamcast hasher ──────────────────────────────────────────────────────────
#
# RA spec:
#   1. First 512 bytes of sector 0 (volume header + ROM header)
#      First 16 bytes must be "SEGA SEGAKATANA "
#   2. Contents of the primary executable (identified in volume header)
#   3. MD5 of buffer
#
# GD-ROM CHDs need a dedicated extraction path (see _rahash_dreamcast_chd):
# chdman's `extractcd` mis-decompresses the high-density (track 3+) session
# when a Dreamcast CHD's huge low-density/high-density intersession gap is
# present -- it writes plausible-looking sector headers but all-zero user
# data, so rahash never finds "SEGA SEGAKATANA " and errors out. This is a
# known chdman limitation with Dreamcast GD-ROM bin/cue round-tripping
# (see mamedev/mame#11903, #6466 -- fixed upstream by PR #12191 / MAME 0.265+,
# not present in the chdman 0.251 shipped by Debian bookworm's mame-tools).
#
# VERIFIED 2026-08-05: the fix is real. chdman 0.276 on the pve HOST (Debian trixie) extracts a
# library Dreamcast CHD correctly -- `extractcd -o out.gdi` yields tracks carrying
# "SEGA SEGAKATANA" and real data, no all-zero corruption. This container is bookworm and pinned
# to 0.251, which is the only reason the raw-sector scan below still exists.
#
# Replacing this with a plain extractcd once the container can run >=0.265 would also fix the
# one thing the scan cannot do: it locates the high-density session by the RAW-sector sync
# pattern, so a CHD built from a GDI with COOKED 2048-byte MODE1 data tracks has no sync to find
# and fails, even though chdman tagged it CHGD correctly. Resident Evil 2: Special Edition is
# stored as .gdi + tracks for exactly that reason (~9% larger than CHD).
# Note the invocation: a GD-ROM must be extracted with a .gdi output. `-o out.cue -ob out.bin`
# fails outright.

# LIMITATION: this whole approach assumes the CHD holds RAW sectors, because it locates the
# high-density session by scanning for the raw-sector SYNC pattern. A CHD built from a GDI whose
# data tracks are COOKED (2048-byte MODE1, as Redump-style GDIs often are) has no sync pattern
# to find and fails with "Could not locate GD-ROM high-density session" — even though chdman
# tagged it correctly as CHGD. That is a limit of this workaround, NOT of CHD: the 128 Dreamcast
# CHDs in the library are MODE1_RAW and hash fine. Store such a disc as its .gdi + tracks
# (a supported extension) until this can read cooked sectors too; the space cost is ~9%.
_DC_UNIT = 2448     # raw CD frame incl. 96-byte subchannel, per chdman extractraw
_DC_SECTOR = 2352   # plain MODE1/AUDIO sector (subchannel stripped)
_DC_SYNC = bytes([0x00] + [0xFF] * 10 + [0x00])
_DC_IP_BIN_SIG = b"SEGA SEGAKATANA "


def _dc_scan_for_sync(raw_path: str, from_unit: int, to_unit: int, require_sig: bool = False):
    """Scan a chdman `extractraw` CD dump for the next raw-sector sync pattern
    (optionally requiring the Dreamcast IP.BIN signature right after it)."""
    with open(raw_path, 'rb') as f:
        u = max(from_unit, 0)
        CHUNK = 4096
        while u < to_unit:
            f.seek(u * _DC_UNIT)
            buf = f.read(min(CHUNK, to_unit - u) * _DC_UNIT)
            if not buf:
                break
            n = len(buf) // _DC_UNIT
            for i in range(n):
                cell = buf[i * _DC_UNIT:i * _DC_UNIT + 32]
                if cell[:12] != _DC_SYNC:
                    continue
                if require_sig and cell[16:32] != _DC_IP_BIN_SIG:
                    continue
                return u + i
            u += n
    return None


CHDMAN276 = "/usr/local/bin/chdman276"


async def _dreamcast_chd_via_276(filepath: str, console_id: int) -> str | None:
    """Hash a Dreamcast CHD the simple way: extractcd to a .gdi, then rahash it.

    This is what the raw-sector scan below has always been a substitute for. chdman's extractcd
    corrupted GD-ROM discs until MAME 0.265 (PR #12191), and bookworm pins mame-tools to 0.251 —
    so /opt/chdman276 carries the trixie 0.276 build with its own loader and libs (it needs
    GLIBC_2.38; this container has 2.36). See /usr/local/bin/chdman276.

    Besides being far simpler, this path handles CHDs the scan cannot: it locates the
    high-density session by RAW-sector sync pattern, so a CHD built from a GDI with COOKED
    2048-byte MODE1 data tracks has no sync to find. Verified round-trip on exactly such a disc
    (Resident Evil 2: Special Edition) — gdi -> chd -> extractcd -> rahash reproduces
    709749676bb335a1f4e134b030254642.

    Returns None if the tool is absent or anything goes wrong, so the caller falls back.
    """
    if not os.path.exists(CHDMAN276):
        return None
    import subprocess as _sp, tempfile as _tf, shutil as _sh
    tmp = _tf.mkdtemp(prefix="dc276_", dir="/rahash_tmp" if os.path.isdir("/rahash_tmp") else None)
    try:
        out = os.path.join(tmp, "disc.gdi")
        r = _sp.run([CHDMAN276, "extractcd", "-i", filepath, "-o", out],
                    capture_output=True, timeout=3600)
        if r.returncode != 0 or not os.path.exists(out):
            log.info("dreamcast: chdman276 extractcd failed, falling back to raw scan",
                     rc=r.returncode, err=(r.stderr or b"")[-160:].decode("utf-8", "replace"))
            return None
        h = _sp.run(["rahash", str(console_id), out], capture_output=True, text=True, timeout=1800)
        if h.returncode == 0 and h.stdout.strip():
            return h.stdout.strip().split()[-1].lower()
        log.info("dreamcast: rahash rejected the 0.276 extract, falling back",
                 err=(h.stderr or "")[:160])
        return None
    except Exception as e:  # noqa
        log.info("dreamcast: 0.276 path errored, falling back", err=str(e)[:160])
        return None
    finally:
        _sh.rmtree(tmp, ignore_errors=True)


async def _rahash_dreamcast_chd(filepath: str, console_id: int = 40) -> str:
    """
    Extract a Dreamcast GD-ROM CHD and hash it with rahash, working around
    chdman extractcd's data-corruption bug for these discs (see module notes
    above). Strategy:
      1. `chdman extractraw` the full raw CD frame stream (correct content,
         but its unit index doesn't reliably line up with cue-style logical
         sector numbers around the low/high-density gap or later track
         boundaries -- chdman's own hunk/pad bookkeeping drifts by a handful
         of sectors here).
      2. Locate track 3 (the high-density session) by searching for its
         "SEGA SEGAKATANA " IP.BIN signature directly, rather than trusting
         FRAMES-only arithmetic.
      3. For any additional data tracks after track 3 (rare multi-track
         GD-ROMs, e.g. Shenmue's CD-audio interludes), locate each by
         scanning forward from the previous data track's verified end for
         the next raw-sector sync pattern.
      4. Rebuild a CUE + BIN (subchannel stripped) from these verified
         offsets and hand it to rahash.
    """
    # Prefer the straightforward extractcd path when the fixed chdman is available.
    got = await _dreamcast_chd_via_276(filepath, console_id)
    if got:
        return got
    import subprocess as _sp, shutil as _sh, os as _os, uuid as _uuid, re as _re

    _MODE_MAP = {
        'MODE1_RAW': 'MODE1/2352', 'MODE1': 'MODE1/2352',
        'MODE2_RAW': 'MODE2/2352', 'MODE2': 'MODE2/2352',
        'AUDIO': 'AUDIO',
    }

    def _is_data(mode: str) -> bool:
        return mode.upper().startswith('MODE')

    info = _sp.run(['chdman', 'info', '-i', filepath],
                   capture_output=True, text=True, timeout=30)

    # Only true GD-ROM dumps (chdman's 'CHGD' metadata tag) hit the
    # extractcd corruption bug this function works around. Some Dreamcast
    # releases (mostly unlicensed homebrew burned as a plain single-session
    # CD) are stored as ordinary CD CHDs ('CHT2' tag) instead -- those are
    # unaffected, so fall back to the generic extractcd path for them.
    if "Tag='CHGD'" not in info.stdout:
        return await _rahash_chd(filepath, console_id)

    tracks = []
    for m in _re.finditer(
        r"TRACK:(\d+)\s+TYPE:(\S+)\s+SUBTYPE:\S+\s+FRAMES:(\d+)\s+PAD:(\d+)",
        info.stdout,
    ):
        tracks.append((int(m.group(1)), m.group(2), int(m.group(3))))
    if not tracks:
        raise RuntimeError(f'Could not parse CD track metadata for {filepath}')
    tracks.sort(key=lambda t: t[0])
    frames_by_num = {n: f for n, _m, f in tracks}
    mode_by_num = {n: mo for n, mo, _f in tracks}

    if 3 not in frames_by_num or not _is_data(mode_by_num[3]):
        raise RuntimeError(f'Unexpected Dreamcast track layout (no data track 3) for {filepath}: {tracks}')

    tmp_dir = _os.path.join('/rahash_tmp', f'dcraw_{_uuid.uuid4().hex}')
    _os.makedirs(tmp_dir, exist_ok=True)
    try:
        raw_path = _os.path.join(tmp_dir, 'raw.bin')
        r = _sp.run(['chdman', 'extractraw', '-i', filepath, '-o', raw_path],
                    capture_output=True, text=True, timeout=1800)
        if r.returncode != 0 or not _os.path.exists(raw_path):
            raise RuntimeError(f'chdman extractraw failed: {r.stderr.strip()}')

        starts = {1: 0}
        # Locate the high-density session (data track 3) by its IP.BIN sync+
        # signature, but START the search PAST track 1. Some discs duplicate the
        # "SEGA SEGAKATANA " signature inside the low-density data track 1 (e.g.
        # Cardcaptor Sakura ~unit 3156, Chicken Run ~unit 6685); a fixed start of
        # 2000 could lock onto that decoy and mislocate the session. Track 1 ends
        # at frames[1] and track 2 is audio (no sync/signature), so the first
        # signature at/after track 1's end is always the genuine track 3.
        hd_start = _dc_scan_for_sync(raw_path, max(frames_by_num.get(1, 0), 16),
                                     200000, require_sig=True)
        if hd_start is None:
            raise RuntimeError(f'Could not locate GD-ROM high-density session in {filepath}')
        starts[3] = hd_start
        if 2 in frames_by_num:
            # Track 2 (low-density audio) sits right after track 1. Back-computing
            # hd_start - frames[2] can underflow negative when chdman drops track
            # 2's (often huge) pad frames from the raw stream, crashing the seek;
            # anchor it after track 1 instead. Its raw read offset only affects
            # the ignored audio content in the rebuilt BIN, never the hash.
            starts[2] = min(starts[1] + frames_by_num.get(1, 0), hd_start)

        track_nums = sorted(frames_by_num)
        last_anchored = 3
        for n in track_nums:
            if n <= 3:
                continue
            if _is_data(mode_by_num[n]):
                search_from = starts[last_anchored] + frames_by_num[last_anchored]
                naive = search_from
                for m in track_nums:
                    if last_anchored < m < n:
                        naive += frames_by_num[m]
                found = _dc_scan_for_sync(raw_path, search_from, naive + 5000)
                if found is None:
                    raise RuntimeError(f'Could not locate track {n} data start in {filepath}')
                starts[n] = found
                last_anchored = n

        # fill in non-data (audio) tracks between anchored data tracks by
        # splitting the verified gap proportionally by declared FRAMES --
        # their content doesn't matter, rc_hash never reads audio tracks
        i = 0
        while i < len(track_nums):
            n = track_nums[i]
            if n in starts:
                i += 1
                continue
            j = i
            while j < len(track_nums) and track_nums[j] not in starts:
                j += 1
            prev_n = track_nums[i - 1]
            gap_start = starts[prev_n] + frames_by_num[prev_n]
            if j < len(track_nums):
                gap_end = starts[track_nums[j]]
            else:
                gap_end = gap_start + sum(frames_by_num[track_nums[k]] for k in range(i, j))
            total_declared = sum(frames_by_num[track_nums[k]] for k in range(i, j))
            gap_len = gap_end - gap_start
            cursor = gap_start
            for k in range(i, j):
                tn = track_nums[k]
                if k == j - 1:
                    length = gap_end - cursor
                else:
                    length = round(frames_by_num[tn] * gap_len / total_declared) if total_declared else 0
                starts[tn] = cursor
                cursor += length
            i = j

        bin_path = _os.path.join(tmp_dir, 'disc.bin')
        cue_path = _os.path.join(tmp_dir, 'disc.cue')

        def _write_unit_range(fout, fin, start_unit, count):
            CHUNK = 8192
            remaining = count
            u = start_unit
            while remaining > 0:
                n = min(CHUNK, remaining)
                fin.seek(u * _DC_UNIT)
                buf = fin.read(n * _DC_UNIT)
                if len(buf) < n * _DC_UNIT:
                    buf = buf + b'\x00' * (n * _DC_UNIT - len(buf))
                out = bytearray(n * _DC_SECTOR)
                for i2 in range(n):
                    out[i2 * _DC_SECTOR:(i2 + 1) * _DC_SECTOR] = buf[i2 * _DC_UNIT:i2 * _DC_UNIT + _DC_SECTOR]
                fout.write(out)
                remaining -= n
                u += n

        cue_lines = ['FILE "disc.bin" BINARY']
        sector_cursor = 0
        with open(raw_path, 'rb') as fin, open(bin_path, 'wb') as fout:
            for num, mode, frames in tracks:
                _write_unit_range(fout, fin, starts[num], frames)
                cue_mode = _MODE_MAP.get(mode, 'MODE1/2352')
                m_, rem = divmod(sector_cursor, 60 * 75)
                s_, f_ = divmod(rem, 75)
                cue_lines.append(f'  TRACK {num:02d} {cue_mode}')
                cue_lines.append(f'    INDEX 01 {m_:02d}:{s_:02d}:{f_:02d}')
                sector_cursor += frames

        with open(cue_path, 'w') as f:
            f.write('\n'.join(cue_lines) + '\n')

        r2 = _sp.run(['rahash', str(console_id), cue_path],
                     capture_output=True, text=True, timeout=120)
        if r2.returncode == 0 and r2.stdout.strip():
            return r2.stdout.strip()
        raise RuntimeError(f'rahash failed on extracted Dreamcast CHD: {r2.stderr.strip()}')
    finally:
        _sh.rmtree(tmp_dir, ignore_errors=True)


class DreamcastHasher(BaseHasher):
    async def hash_file(self, filepath: str) -> str:
        p = Path(filepath)
        if p.suffix.lower() == ".chd":
            return await _rahash_dreamcast_chd(filepath, 40)
        elif p.suffix.lower() in (".bin", ".cue", ".iso", ".gdi"):
            import subprocess as _sp
            result = _sp.run(["rahash", str(40), filepath], capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout.strip()
            raise RuntimeError(f"rahash failed: {result.stderr}")
        elif p.suffix.lower() in _ARCHIVE_EXTS:
            return await _hash_archive_disc(filepath, 40)
        else:
            raise ValueError(f"Unsupported format for DreamcastHasher: {p.suffix}")
# ── 3DO hasher ────────────────────────────────────────────────────────────────
#
# RA spec:
#   Hash volume header (first 132 bytes of sector 0) + LaunchMe file contents

class ThreeDOHasher(BaseHasher):
    async def hash_file(self, filepath: str) -> str:
        p = Path(filepath)
        if p.suffix.lower() == ".chd":
            return await _rahash_chd(filepath, 43)
        elif p.suffix.lower() in (".bin", ".cue", ".iso"):
            import subprocess as _sp
            result = _sp.run(["rahash", str(43), filepath], capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout.strip()
            raise RuntimeError(f"rahash failed: {result.stderr}")
        elif p.suffix.lower() in _ARCHIVE_EXTS:
            return await _hash_archive_disc(filepath, 43)
        else:
            raise ValueError(f"Unsupported format for ThreeDOHasher: {p.suffix}")

async def _hash_cdi_v6_jaguar(filepath: str) -> str:
    """
    Hash a CDI v6 Jaguar CD image.

    Mirrors rc_hash_jaguar_cd from rcheevos/src/rhash/hash_disc.c.

    Pass 1: Scan sectors for "TARA IPARPVODED TA AEHDAREA RT I" (byteswapped
    ATARI header). Use the rcheevos byteswap size formula. Hash the boot code.

    Pass 2 (homebrew): If pass 1 hash == the known homebrew bootloader hash
    (254487b59ab21bc005338e85cbf9fd2f), all homebrews share the same boot stub.
    Scan for KART track 2 data: look for b'RT!IRTKA' at sector offset 0x5E,
    read size at 0xA6 (byteswap formula), hash from 0xAA.
    Return the first KART hash that produces a non-empty result.
    """
    import struct as _st, hashlib as _hl

    HEADER_NORMAL   = b'ATARI APPROVED DATA HEADER ATRI '
    HEADER_SWAPPED  = b'TARA IPARPVODED TA AEHDAREA RT I'
    HOMEBREW_HASH   = "254487b59ab21bc005338e85cbf9fd2f"
    SECTOR          = 2352

    with open(filepath, 'rb') as _f:
        raw_file = _f.read()

    desc_size   = _st.unpack('<I', raw_file[-4:])[0]
    raw         = raw_file[:len(raw_file) - desc_size]
    num_sectors = len(raw) // SECTOR

    def read_sector(s):
        return bytearray(raw[s * SECTOR:(s + 1) * SECTOR])

    def bswap16(buf):
        for _i in range(0, len(buf) - 1, 2):
            buf[_i], buf[_i + 1] = buf[_i + 1], buf[_i]

    def bs_uint32(buf, off):
        """rcheevos byteswap size formula."""
        return (buf[off] << 16) | (buf[off+1] << 24) | buf[off+2] | (buf[off+3] << 8)

    def hash_bytes(start_sector, start_offset, size, do_bswap):
        md5 = _hl.md5(usedforsecurity=False)
        remaining = size
        sector = start_sector
        offset = start_offset
        while remaining > 0:
            buf = read_sector(sector)
            if do_bswap:
                bswap16(buf)
            chunk = min(remaining, SECTOR - offset)
            md5.update(bytes(buf[offset:offset + chunk]))
            remaining -= chunk
            offset = 0
            sector += 1
        return md5.hexdigest()

    # ── Pass 1: find session-2 data track ─────────────────────────────────────
    p1_sector = p1_offset = None
    byteswapped = False
    for s in range(num_sectors):
        buf = read_sector(s)
        for sig, bswp in [(HEADER_SWAPPED, True), (HEADER_NORMAL, False)]:
            idx = buf.find(sig, 64)
            if idx != -1:
                p1_sector, p1_offset, byteswapped = s, idx, bswp
                break
        if p1_sector is not None:
            break

    if p1_sector is None:
        raise RuntimeError("Jaguar CD boot header not found in CDI v6 raw data")

    buf = read_sector(p1_sector)
    off = p1_offset + 32 + 4
    if byteswapped:
        size = bs_uint32(buf, off)
    else:
        size = _st.unpack('>I', bytes(buf[off:off+4]))[0]
    off += 4

    if size == 0 or size > 0x400000:
        raise RuntimeError(f"Implausible boot code size: {size}")

    h1 = hash_bytes(p1_sector, off, size, byteswapped)

    if h1 != HOMEBREW_HASH or not byteswapped:
        return h1

    # ── Pass 2: KART homebrew game data ───────────────────────────────────────
    # Scan all sectors for KART signature at offset 0x5E, pick the one with
    # the largest (most likely real game) payload.
    kart_candidates = []
    for s in range(num_sectors):
        buf = read_sector(s)
        if bytes(buf[0x5E:0x66]) == b'RT!IRTKA':
            size2 = bs_uint32(buf, 0xA6)
            if 0 < size2 <= 0x400000:
                kart_candidates.append((size2, s))

    if not kart_candidates:
        raise RuntimeError("Homebrew KART data not found in CDI v6")

    # Use the largest KART payload (the actual game code, not helper stubs)
    kart_candidates.sort(reverse=True)
    _, kart_sector = kart_candidates[0]
    buf = read_sector(kart_sector)
    size2 = bs_uint32(buf, 0xA6)
    return hash_bytes(kart_sector, 0xAA, size2, True)

# ── Atari Jaguar CD hasher ────────────────────────────────────────────────────
#
# RA spec:
#   Header block in first sector → location + size of boot code → hash boot code
#   If hash == 254487b59ab21bc005338e85cbf9fd2f (homebrew bootloader):
#     Look in track 2 for another header block → hash that boot code instead

JAGUAR_CD_HOMEBREW_HASH = "254487b59ab21bc005338e85cbf9fd2f"

class JaguarCDHasher(BaseHasher):
    async def hash_file(self, filepath: str) -> str:
        import subprocess as _sp, struct as _st
        p = Path(filepath)
        if p.suffix.lower() == ".chd":
            return await _rahash_chd(filepath, 77)
        elif p.suffix.lower() == ".cdi":
            # Check CDI version — rcheevos only supports up to v3.5 (0x80000004)
            with open(filepath, 'rb') as _f:
                _f.seek(-8, 2)
                version = _st.unpack('<I', _f.read(4))[0]
            if version > 0x80000004:
                return await _hash_cdi_v6_jaguar(filepath)
            result = _sp.run(["rahash", "77", filepath], capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout.strip()
            raise RuntimeError(f"rahash failed: {result.stderr}")
        elif p.suffix.lower() in (".cue", ".bin"):
            result = _sp.run(["rahash", "77", filepath], capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout.strip()
            raise RuntimeError(f"rahash failed: {result.stderr}")
        else:
            raise ValueError(f"Unsupported Jaguar CD format: {p.suffix}")
# ── Neo Geo CD hasher ─────────────────────────────────────────────────────────
#
# RA spec:
#   Parse IPL.TXT → each line is a file to load
#   PRG files are executable code → load each PRG into buffer → hash buffer

class NeoGeoCDHasher(BaseHasher):
    async def hash_file(self, filepath: str) -> str:
        p = Path(filepath)
        if p.suffix.lower() == ".chd":
            return await _rahash_chd(filepath, 56)
        elif p.suffix.lower() in (".bin", ".cue", ".iso"):
            import subprocess as _sp
            result = _sp.run(["rahash", str(56), filepath], capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout.strip()
            raise RuntimeError(f"rahash failed: {result.stderr}")
        elif p.suffix.lower() in _ARCHIVE_EXTS:
            return await _hash_archive_disc(filepath, 56)
        else:
            raise ValueError(f"Unsupported format for NeoGeoCDHasher: {p.suffix}")
# ── PC Engine CD hasher ───────────────────────────────────────────────────────
#
# RA spec:
#   Read 128 bytes from sector 1 of data track
#   Verify "PC Engine CD-ROM SYSTEM" at offset 32
#   Last 22 bytes = disc title → copy to buffer
#   Bytes 0-2 = little-endian sector index for boot code
#   Byte 3 = number of sectors for boot code
#   Append boot code sectors to buffer → hash

class PCEngineCDHasher(BaseHasher):
    async def hash_file(self, filepath: str) -> str:
        p = Path(filepath)
        if p.suffix.lower() == ".chd":
            return await _rahash_chd(filepath, 76)
        elif p.suffix.lower() in (".bin", ".cue", ".iso"):
            import subprocess as _sp
            result = _sp.run(["rahash", "76", filepath], capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout.strip()
            raise RuntimeError(f"rahash failed: {result.stderr}")
        elif p.suffix.lower() in _ARCHIVE_EXTS:
            return await _hash_archive_disc(filepath, 76)
        else:
            raise ValueError(f"Unsupported format for PCEngineCDHasher: {p.suffix}")
# ── PC-FX hasher ──────────────────────────────────────────────────────────────
#
# RA spec:
#   Read 32 bytes from sector 0 — verify "PC-FX:Hu_CD-ROM"
#   Read 128 bytes from sector 1 into buffer (volume header + disc title)
#   32-bit LE at buffer[32] = first boot sector
#   32-bit LE at buffer[36] = number of boot sectors
#   Append boot code to buffer → hash

class PCFXHasher(BaseHasher):
    async def hash_file(self, filepath: str) -> str:
        import subprocess as _sp
        p = Path(filepath)
        if p.suffix.lower() == ".chd":
            return await _rahash_chd(filepath, 49)
        elif p.suffix.lower() in (".bin", ".cue", ".iso"):
            result = _sp.run(["rahash", str(49), filepath], capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout.strip()
            raise RuntimeError(f"rahash failed: {result.stderr}")
        elif p.suffix.lower() in (".zip", ".7z"):
            import zipfile as _zf, tempfile as _tmp, shutil as _sh, os as _os
            tmp_dir = _tmp.mkdtemp()
            try:
                with _zf.ZipFile(filepath) as z:
                    z.extractall(tmp_dir)
                cues = [f for f in _os.listdir(tmp_dir) if f.lower().endswith(".cue")]
                if not cues:
                    raise RuntimeError(f"No .cue file found in ZIP: {filepath}")
                cue_path = _os.path.join(tmp_dir, cues[0])
                result = _sp.run(["rahash", "49", cue_path], capture_output=True, text=True, timeout=60)
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
                raise RuntimeError(f"rahash failed on extracted CUE: {result.stderr.strip()}")
            finally:
                _sh.rmtree(tmp_dir, ignore_errors=True)
        else:
            raise NotImplementedError(f"Unsupported format for PCFXHasher: {p.suffix}")
# ── Generic rahash hasher (no-achievement-yet systems with working hashes) ────
class RahashHasher(BaseHasher):
    """
    Run rahash (rcheevos) for a fixed console id. For systems RA can already
    hash even before achievement sets exist: Commodore 64 (30), Nintendo 3DS
    (62, decrypted dumps only — our rahash has no AES keys), MS-DOS (26, .dosz).
    Extracts a zip wrapper first; .dosz is rcheevos's own container, hashed whole.
    """
    def __init__(self, console_id: int):
        self.console_id = console_id

    async def hash_file(self, filepath: str) -> str:
        import zipfile as _zf, shutil as _sh, tempfile as _tmp, os as _os
        p = Path(filepath)
        if p.suffix.lower() == ".zip":
            # Preserve the inner file's extension — rcheevos dispatches C64 by it
            # (.d64 vs .t64 vs .prg vs .crt all hash differently).
            _SKIP = (".txt", ".nfo", ".dat", ".xml", ".jpg", ".png", ".sfv", ".md5")
            tmp_dir = _tmp.mkdtemp()
            try:
                with _zf.ZipFile(filepath) as z:
                    members = [n for n in z.namelist()
                               if not n.endswith("/") and not _os.path.basename(n).startswith("._")
                               and not n.lower().endswith(_SKIP)]
                    if not members:
                        raise RuntimeError(f"No ROM in archive: {filepath}")
                    inner = _os.path.join(tmp_dir, _os.path.basename(members[0]))
                    with open(inner, "wb") as f: f.write(z.read(members[0]))
                return _rahash_file(inner, self.console_id)
            finally:
                _sh.rmtree(tmp_dir, ignore_errors=True)
        return _rahash_file(filepath, self.console_id)


# ── Update registry ───────────────────────────────────────────────────────────

_HASHER_MAP.update({
    "dreamcast_chd":  DreamcastHasher(),
    "3do_chd":        ThreeDOHasher(),
    "jaguarcd":       JaguarCDHasher(),   # raw .cue/.bin + .cdi (v6 via _hash_cdi_v6_jaguar)
    "jaguarcd_chd":   JaguarCDHasher(),   # legacy alias
    "neogeocd_chd":   NeoGeoCDHasher(),
    "pcenginecd_chd": PCEngineCDHasher(),
    "pcfx_chd":       PCFXHasher(),
    "c64":            RahashHasher(30),   # Commodore 64
    "n3ds":           RahashHasher(62),   # Nintendo 3DS (decrypted)
    "dos":            RahashHasher(26),   # MS-DOS (.dosz)
})

# ── Disc-system raw MD5 utilities (shared by scanner + patcher) ───────────────
# For GameCube/Wii, the RA hash is a rahash signature, NOT the raw file MD5.
# Patch READMEs specify the raw Redump ISO MD5, so we need a separate computation.

_DISC_IMAGE_EXTS = {".iso", ".gcm", ".img", ".wbfs"}


def compute_disc_raw_md5(fp_str: str) -> str:
    """Stream-compute the raw Redump file MD5 of a disc image (minimal disk I/O).

    Handles: .iso/.gcm (direct), .rvz (dolphin-tool convert), .zip/.7z (extract first).
    """
    import subprocess as _sp, shutil as _sh, tempfile as _tmp, hashlib as _hl2, zipfile as _zf2

    fp = Path(fp_str)
    suf = fp.suffix.lower()

    def _stream_md5(fh) -> str:
        h = _hl2.md5(usedforsecurity=False)
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
        return h.hexdigest().lower()

    if suf == ".zip":
        with _zf2.ZipFile(str(fp)) as z:
            for nm in z.namelist():
                inner_suf = Path(nm).suffix.lower()
                if inner_suf in _DISC_IMAGE_EXTS:
                    with z.open(nm) as fh:
                        return _stream_md5(fh)
                elif inner_suf == ".rvz":
                    td = _tmp.mkdtemp()
                    try:
                        z.extract(nm, td)
                        rvz = Path(td) / nm
                        out = rvz.with_suffix(".iso")
                        _sp.run(["/usr/games/dolphin-tool", "convert", "-f", "iso",
                                 "-i", str(rvz), "-o", str(out)],
                                capture_output=True, timeout=1800)
                        with open(out, "rb") as fh:
                            return _stream_md5(fh)
                    finally:
                        _sh.rmtree(td, ignore_errors=True)
        raise RuntimeError(f"No disc image found in {fp}")

    elif suf == ".7z":
        td = _tmp.mkdtemp()
        try:
            _sp.run(["7z", "e", str(fp), "-o" + td, "-y"],
                    capture_output=True, timeout=300)
            for f in Path(td).iterdir():
                if f.suffix.lower() in _DISC_IMAGE_EXTS:
                    with open(f, "rb") as fh:
                        return _stream_md5(fh)
            raise RuntimeError(f"No disc image found in {fp}")
        finally:
            _sh.rmtree(td, ignore_errors=True)

    elif suf == ".rvz":
        td = _tmp.mkdtemp()
        try:
            out = Path(td) / (fp.stem + ".iso")
            _sp.run(["/usr/games/dolphin-tool", "convert", "-f", "iso",
                     "-i", str(fp), "-o", str(out)],
                    capture_output=True, timeout=1800)
            with open(out, "rb") as fh:
                return _stream_md5(fh)
        finally:
            _sh.rmtree(td, ignore_errors=True)

    elif suf in _DISC_IMAGE_EXTS:
        with open(fp, "rb") as fh:
            return _stream_md5(fh)

    raise RuntimeError(f"Unsupported disc format: {suf}")


def extract_disc_image(fp_str: str):
    """Extract/convert a disc image to a temporary ISO file.

    Returns (Path, cleanup_fn) where cleanup_fn() removes the temp dir.
    For direct ISO files, cleanup_fn is a no-op.
    Raises RuntimeError on failure.
    """
    import subprocess as _sp, shutil as _sh, tempfile as _tmp, zipfile as _zf2

    fp = Path(fp_str)
    suf = fp.suffix.lower()
    td = None

    try:
        if suf == ".zip":
            td = _tmp.mkdtemp()
            with _zf2.ZipFile(str(fp)) as z:
                for nm in z.namelist():
                    inner_suf = Path(nm).suffix.lower()
                    if inner_suf in _DISC_IMAGE_EXTS:
                        z.extract(nm, td)
                        ep = Path(td) / nm
                        return ep, lambda d=td: _sh.rmtree(d, ignore_errors=True)
                    elif inner_suf == ".rvz":
                        z.extract(nm, td)
                        rvz = Path(td) / nm
                        out = rvz.with_suffix(".iso")
                        r = _sp.run(["/usr/games/dolphin-tool", "convert", "-f", "iso",
                                     "-i", str(rvz), "-o", str(out)],
                                    capture_output=True, timeout=1800)
                        if r.returncode != 0 or not out.exists():
                            raise RuntimeError("dolphin-tool failed for zip-of-rvz")
                        rvz.unlink(missing_ok=True)
                        return out, lambda d=td: _sh.rmtree(d, ignore_errors=True)
            raise RuntimeError(f"No disc image found in {fp}")

        elif suf == ".7z":
            td = _tmp.mkdtemp()
            _sp.run(["7z", "e", str(fp), "-o" + td, "-y"],
                    capture_output=True, timeout=300)
            for f in Path(td).iterdir():
                if f.suffix.lower() in _DISC_IMAGE_EXTS:
                    return f, lambda d=td: _sh.rmtree(d, ignore_errors=True)
            raise RuntimeError(f"No disc image found in {fp}")

        elif suf == ".rvz":
            td = _tmp.mkdtemp()
            out = Path(td) / (fp.stem + ".iso")
            r = _sp.run(["/usr/games/dolphin-tool", "convert", "-f", "iso",
                         "-i", str(fp), "-o", str(out)],
                        capture_output=True, timeout=1800)
            if r.returncode != 0 or not out.exists():
                raise RuntimeError("dolphin-tool conversion failed")
            return out, lambda d=td: _sh.rmtree(d, ignore_errors=True)

        elif suf in _DISC_IMAGE_EXTS:
            return fp, lambda: None  # Direct ISO — caller must not delete it

        raise RuntimeError(f"Unsupported disc format: {suf}")

    except Exception:
        if td:
            _sh.rmtree(td, ignore_errors=True)
        raise
