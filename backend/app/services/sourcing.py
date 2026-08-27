"""Auto-source missing games from archive.org and verify by hash before keeping them.

Trust gate: a download is only kept if the system's hasher reproduces the RA hash that made
it a wanted game — so wrong/renamed/corrupt files are always rejected. Source master is
read-only, so verified acquisitions land in the canonical library.

Sources are registry-driven (`data/archives.json`), tried in order per system:
  1. cart  — No-Intro `ni-roms` per-game zip members (verify = system hasher; ~42 systems)
  2. disc  — per-region archive.org items (Redump CHD/RVZ/ISO), matched via the item's file
             listing then hash-verified (GameCube/PSX/PSP/Dreamcast/…)
  3. search — anything else on archive.org, found by title search, still hash-verified
Filename resolution is hash-first (No-Intro DAT md5→name) then name/fuzzy — the hash decides.
"""
from __future__ import annotations
import asyncio
import difflib
import json
import re
import shutil
import subprocess
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote, unquote
from app.core.config import settings
from app.core.systems import get_system_by_folder
from app.hashers import get_hasher
from app.services.ingest import _get_or_create_game, _upsert_rom
import structlog

log = structlog.get_logger()

ARCHIVE = "https://archive.org"
NIROMS = f"{ARCHIVE}/download/ni-roms/roms"
_ART = {"THE", "A", "AN"}
_REG = None
_ITEM_IDX: dict[str, dict] = {}    # archive.org item -> {title-key -> [filenames]}
_MEMBERS: dict[str, dict] = {}     # ni-roms zip -> {norm member -> [basenames]}

# RA console id (for `ni-roms` verify uses SYSTEM_ZIP; kept for the tick's cartridge gate)
SYSTEM_ZIP = {1: "Sega - Mega Drive - Genesis.zip", 11: "Sega - Master System - Mark III.zip",
              15: "Sega - Game Gear.zip", 7: "Nintendo - Nintendo Entertainment System (Headered).zip",
              3: "Nintendo - Super Nintendo Entertainment System.zip",
              2: "Nintendo - Nintendo 64 (BigEndian).zip", 4: "Nintendo - Game Boy.zip",
              6: "Nintendo - Game Boy Color.zip", 5: "Nintendo - Game Boy Advance.zip",
              8: "NEC - PC Engine - TurboGrafx-16.zip", 16: "gamecube", 39: "saturn", 12: "psx"}


def _registry() -> dict:
    global _REG
    if _REG is None:
        try:
            _REG = json.loads((Path(__file__).parent.parent / "data" / "archives.json").read_text())
        except Exception:  # noqa
            _REG = {"cart": {"systems": {}}, "disc": {}}
    return _REG


def _cookies() -> str | None:
    ini = Path.home() / ".config/internetarchive/ia.ini"
    if not ini.is_file():
        return None
    u = s = None
    for line in ini.read_text().splitlines():
        if line.startswith("logged-in-user"):
            u = line.split("=", 1)[1].split(";")[0].strip()
        elif line.startswith("logged-in-sig"):
            s = line.split("=", 1)[1].split(";")[0].strip()
    return f"logged-in-user={u}; logged-in-sig={s}" if u and s else None


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().upper()
    s = s.replace("'", "").replace("`", "")   # apostrophes drop with NO space (Luigi's -> LUIGIS)
    return " ".join(t for t in re.sub(r"[^A-Z0-9]+", " ", s).split() if t not in _ART)


def _title_key(name: str) -> str:
    """Region/language/ext-stripped normalized title, for matching across DB naming schemes."""
    base = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", name or "")
    base = re.sub(r"\s*\([^)]*\)|\s*\[[^\]]*\]", "", base)
    # non-paren disc markers (Arquivista uses "<Title> (Region) - CD1.chd"): strip so a
    # multi-disc member collapses to the same title-key as its single-disc RA/DB title.
    base = re.sub(r"\s*-\s*CD\s*\d+\b", "", base, flags=re.I)
    return _norm(base)


def _attempt_reason(a: dict) -> str:
    """Turn an attempt's counters into a one-line cause for the Missing page.

    The distinction that matters is "we never found anything to try" vs "we tried and it
    failed", and among failures, whose fault it was. A wrong dump is a fact about the archive;
    a download or hasher failure is a fault on our side and should be actionable.
    """
    if a["hash_error"]:
        return (f"hasher errored on {a['hash_error']} download(s) — toolchain problem, "
                "not a wrong dump")
    if a["hash_reject"]:
        return (f"downloaded {a['hash_reject']} file(s), none was a valid disc image for this "
                "console — the archives only have other-platform or non-game files")
    if a["hash_mismatch"]:
        return (f"downloaded {a['hash_mismatch']} candidate(s), none matched the RA hash "
                "— archives have a different dump")
    if a["dl_failed"] and not a["downloaded"]:
        return f"all {a['dl_failed']} candidate download(s) failed — source unreachable?"
    if not a["candidates"]:
        return "no candidate file found in any configured source"
    return "no hash-matching source found"


def _download(url: str, cookies: str | None, dest: Path, timeout: int | None = None) -> bool:
    """curl -L -b keeps the auth cookie across archive.org's cross-host download redirect.

    Two things this has to get right, both learned the hard way:

    1. CHECK CURL'S EXIT CODE, not just %{http_code}. When a transfer is cut off mid-body the
       status line has long since been parsed, so http_code is still 200 while curl exits 28
       (timeout) / 18 (partial) / 56 (recv error) and `dest` holds a TRUNCATED file. The old
       test — http_code == 200 and size > 0 — passed a 10MB fragment of a 3GB disc as success.
       The caller then hash-checked the fragment, failed, deleted it, and reported "no
       hash-matching source found", making a broken download look like a wrong dump.
    2. BOUND ON STALL, NOT TOTAL TIME. A fixed `-m` kills healthy multi-GB transfers: at the
       ~2MB/s we get from archive.org a 3GB disc needs ~25min and the old 1800s cap sat right
       on top of that. We now abort only if throughput stays under `stall_bytes` for
       `stall_secs` — dead connections die fast, slow-but-progressing ones finish. `timeout`
       is still honoured when a caller passes one explicitly (short metadata/index fetches).
    """
    stall_bytes, stall_secs = 2048, 300
    cmd = ["curl", "-sL", "--fail-with-body", "-w", "%{http_code}", "-o", str(dest),
           "--speed-limit", str(stall_bytes), "--speed-time", str(stall_secs),
           "--connect-timeout", "60", "--retry", "2", "--retry-delay", "5"]
    if timeout:
        cmd += ["-m", str(timeout)]
    if cookies:
        cmd[1:1] = ["-b", cookies]
    r = subprocess.run(cmd + [url], capture_output=True, text=True)
    ok = r.returncode == 0 and (r.stdout or "").strip()[-3:] == "200" \
        and dest.exists() and dest.stat().st_size > 0
    if not ok and dest.exists():
        # never leave a partial behind for a later run to mistake for a complete file
        log.warning("download failed", url=url[:120], curl_exit=r.returncode,
                    http=(r.stdout or "").strip()[-3:], got_bytes=dest.stat().st_size)
        dest.unlink(missing_ok=True)
    return ok


def _get_json(url: str, cookies: str | None = None, timeout: int = 60):
    cmd = ["curl", "-sL", "-m", str(timeout)]
    if cookies:
        cmd += ["-b", cookies]
    r = subprocess.run(cmd + [url], capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except Exception:  # noqa
        return None


def _get_text(url: str, timeout: int = 30) -> str | None:
    """Fetch a page as text with a browser UA (some homebrew sites gate scraping on it)."""
    r = subprocess.run(["curl", "-sL", "-m", str(timeout), "-A", "Mozilla/5.0", url],
                       capture_output=True, text=True)
    return r.stdout or None


def _member_index(zipname: str, cookies: str) -> dict:
    """ni-roms zip member basenames indexed by normalized name (fetched + cached once)."""
    if zipname in _MEMBERS:
        return _MEMBERS[zipname]
    out = Path("/tmp") / f"nilist_{abs(hash(zipname))}.html"
    idx: dict[str, list] = {}
    if not _download(f"{NIROMS}/{quote(zipname)}/", cookies, out, timeout=120):
        log.warning("ni-roms listing unavailable — not caching, will retry", zip=zipname)
        return {}                      # same no-cache-on-failure rule as _item_index
    for enc in re.findall(r"\.zip/([^\"/]+?\.zip)\"", out.read_text(errors="ignore")):
        m = unquote(enc)[:-4]
        idx.setdefault(_norm(m), []).append(m)
    out.unlink(missing_ok=True)
    _MEMBERS[zipname] = idx
    return idx


_ITEM_FILES: dict[str, list] = {}


def _item_files(item: str, cookies: str | None = None) -> list:
    """Every file archive.org publishes for an item, WITH its md5/sha1/crc32/size.

    The metadata API has always returned these and _item_index threw them away, keeping only
    filenames — so candidate choice rested entirely on names, which are wildly inconsistent
    across uploaders ("Crazy Frog Racer 2.rar", "SLUS_209.76.Ford Racing 3.iso").
    """
    if item in _ITEM_FILES:
        return _ITEM_FILES[item]
    data = _get_json(f"{ARCHIVE}/metadata/{quote(item)}", cookies)
    if not data or "files" not in data:
        log.warning("item metadata unavailable — not caching, will retry", item=item)
        return []                      # same no-cache-on-failure rule as _item_index
    _ITEM_FILES[item] = data.get("files", []) or []
    return _ITEM_FILES[item]


def _md5_matches(item: str, md5set: set, cookies: str | None = None) -> list:
    """Files in an item whose PUBLISHED md5 is one RA accepts — a certain hit, no download.

    Only meaningful where RA's hash is the file's own hash: verified true for gb, gamegear,
    mastersystem, megadrive and snes (raw ROM md5) and nes (header-stripped). For disc systems
    RA hashes the boot executable INSIDE the image, so no published file hash can ever match
    and the disc must be downloaded to be judged — which is exactly why PS2/PSX/PSP cannot be
    pre-verified this way, and why they waste the most bandwidth on wrong guesses.
    """
    if not md5set:
        return []
    out = []
    # NOTE: this can only ever fire where RA's hash is the FILE's hash. It is not, for:
    #   * disc systems  — RA hashes the boot executable inside the image
    #   * arcade        — RA hashes the ROM members inside the .zip/.7z, so a MAME set's
    #                     published md5 never matches. Dream Shopper sat "missing" with a
    #                     perfectly good dremshpr.zip on archive.org: published md5 31e4679d,
    #                     RA wants 3fd40386, and the arcade hasher produces exactly that from
    #                     the same file. Those systems must download-then-hash.
    for f in _item_files(item, cookies):
        m = (f.get("md5") or "").lower()
        if m and m in md5set:
            out.append(f)
    return out


def _item_index(item: str, cookies: str | None = None) -> dict:
    """archive.org item file basenames indexed by region-stripped title-key (metadata API)."""
    if item in _ITEM_IDX:
        return _ITEM_IDX[item]
    idx: dict[str, list] = {}
    data = _get_json(f"{ARCHIVE}/metadata/{quote(item)}", cookies)
    if not data or "files" not in data:
        # DO NOT CACHE. _get_json returns None for a timeout, a 429, or an HTML error page,
        # and caching that empty result made one transient hiccup look like "this source has
        # nothing" for the entire process. With 33 PS2 letter collections registered, a single
        # blip on the "c" item silently removed Cinderella's base disc for a whole 9-hour run,
        # which then reported "base disc not available from any configured source".
        log.warning("item metadata unavailable — not caching, will retry", item=item)
        return {}
    for f in data.get("files", []):
        name = f.get("name", "")
        if not name or name.lower().endswith((".xml", ".sqlite", ".torrent", ".txt")):
            continue
        # key by the file BASENAME, not the full path — items with subdirs (e.g. chd_pcecd's
        # CHD-PCECD/, CHD-TGCD/) would otherwise bake the subdir into the title-key and never match.
        idx.setdefault(_title_key(name.split("/")[-1]), []).append(name)
    _ITEM_IDX[item] = idx
    return idx


# ── Torrent sources ──────────────────────────────────────────────────────────
# Post-Myrient (2026-03-31) several clean No-Intro/Redump sets are torrent-only. A torrent
# source downloads ONE file out of a big set torrent (metadata-only listing → selective
# --select-file leech), extracts it if it's a .7z/.zip, then hash-verifies against the RA gate
# exactly like every other source. aria2c does both; archive.org-hosted torrents carry HTTP
# webseeds (GetRight/URL-list) so a single file pulls at HTTP speed even with a dead BT swarm.
# GENERIC: any system can register {"kind":"torrent","torrent_url"|"magnet":...} in its disc list.
_TORRENT_IDX: dict[str, dict] = {}     # ref -> {title-key: [(file_index, path)]}
_TORRENT_FILE: dict[str, str] = {}     # ref -> aria2c source arg (magnet, or cached .torrent path)
_TFILE_RE = re.compile(r"^\s*(\d+)\|(\S.*)$")   # aria2c --show-files line: "<idx>|./path"


def _torrent_ref(src: dict) -> str | None:
    return src.get("magnet") or src.get("torrent_url")


def _torrent_source_arg(ref: str, cookies: str | None) -> str | None:
    """The arg to hand aria2c: a magnet as-is, or a cached local .torrent fetched once (with
    archive.org auth cookies — some items 401 the .torrent). Cached per ref."""
    if ref in _TORRENT_FILE:
        return _TORRENT_FILE[ref]
    if ref.startswith("magnet:"):
        _TORRENT_FILE[ref] = ref
        return ref
    stage = Path(settings.canonical_path) / "_staging" / "torrent"
    stage.mkdir(parents=True, exist_ok=True)
    dest = stage / f"src_{abs(hash(ref))}.torrent"
    if _download(ref, cookies, dest, timeout=180) and dest.stat().st_size > 200:
        _TORRENT_FILE[ref] = str(dest)
        return str(dest)
    dest.unlink(missing_ok=True)
    return None


def _torrent_index(ref: str, cookies: str | None) -> dict:
    """{title-key -> [(file_index, path)]} for the whole set torrent — fetched once via a
    metadata-only listing (these torrents hold thousands of files), cached per ref."""
    if ref in _TORRENT_IDX:
        return _TORRENT_IDX[ref]
    idx: dict[str, list] = {}
    arg = _torrent_source_arg(ref, cookies)
    if arg:
        try:
            r = subprocess.run(
                ["aria2c", "--show-files=true", "--bt-metadata-only=true",
                 "--bt-save-metadata=false", "--bt-tracker-timeout=30", arg],
                capture_output=True, text=True, timeout=180)
            for line in (r.stdout or "").splitlines():
                m = _TFILE_RE.match(line)
                if not m:
                    continue
                path = m.group(2).strip()
                if "____padding_file" in path or path.endswith("/"):
                    continue
                idx.setdefault(_title_key(path.split("/")[-1]), []).append((int(m.group(1)), path))
        except Exception:  # noqa
            pass
    _TORRENT_IDX[ref] = idx
    return idx


def _torrent_candidates(src: dict, ra: dict, cookies: str | None):
    """Yield (ref, file_index, path) for set-torrent files matching the wanted game — exact
    title-key (from RA title + every hash name) first, then fuzzy≥0.82. Hash-verify decides."""
    ref = _torrent_ref(src)
    if not ref:
        return
    idx = _torrent_index(ref, cookies)
    if not idx:
        return
    keys = {_title_key(ra.get("title", ""))}
    for h in ra.get("hashes", []):
        if h.get("name"):
            keys.add(_title_key(h["name"]))
    keys.discard("")
    seen: set = set()
    for k in keys:
        for fi, path in idx.get(k, []):
            if fi not in seen:
                seen.add(fi)
                yield (ref, fi, path)
    scored = sorted(
        ((max((difflib.SequenceMatcher(None, k, fk).ratio() for k in keys), default=0.0), fi, path)
         for fk, lst in idx.items() for fi, path in lst),
        key=lambda x: x[0], reverse=True)
    for r, fi, path in scored[:5]:
        if r < 0.82:
            break
        if fi not in seen:
            seen.add(fi)
            yield (ref, fi, path)


def _torrent_fetch(ref: str, file_index: int, path: str, dest_dir: Path,
                   cookies: str | None) -> Path | None:
    """aria2c-download ONLY file_index from the set torrent into dest_dir (leech, no seed;
    fail-fast on a dead swarm). Returns the fetched file, located by basename."""
    arg = _torrent_source_arg(ref, cookies)
    if not arg:
        return None
    try:
        subprocess.run(
            ["aria2c", f"--select-file={file_index}", "--seed-time=0", "--bt-stop-timeout=180",
             "--bt-tracker-timeout=30", "--bt-max-peers=60", "--max-connection-per-server=4",
             "--summary-interval=0", "--console-log-level=warn", f"--dir={dest_dir}", arg],
            capture_output=True, text=True, timeout=3000)
    except Exception:  # noqa
        return None
    target = Path(path).name
    for f in dest_dir.rglob("*"):    # skip 0-byte placeholders aria2c makes for adjacent files
        if f.is_file() and f.name == target and f.stat().st_size > 0:
            return f
    return None


def _prov(kind: str, url: str | None = None, **extra) -> dict:
    """Provenance record for a newly acquired file — see RomFile.provenance.

    Every acquisition path already knows exactly where the bytes came from; this just stops us
    throwing that away. Written once at onboard time and never overwritten by a disk rescan.
    """
    import datetime as _dt
    d = {"kind": kind, "verified": True,
         "at": _dt.datetime.utcnow().isoformat(timespec="seconds")}
    if url:
        d["url"] = url
    d.update({k: v for k, v in extra.items() if v is not None})
    return d


def _sources_for(folder: str) -> list[dict]:
    reg = _registry()
    out = []
    czip = reg.get("cart", {}).get("systems", {}).get(folder)
    if czip:
        out.append({"kind": "cart", "zip": czip})
    for d in reg.get("disc", {}).get(folder, []):
        if isinstance(d, dict) and (d.get("item") or d.get("base")
                                    or d.get("magnet") or d.get("torrent_url")):
            out.append({"kind": d.get("kind", "disc"), **d})
    return out


_MC: dict[str, list] = {}    # mariocube letter dir -> [wad filenames]


def _mc_names(url: str) -> list[str]:
    if url in _MC:
        return _MC[url]
    out = Path("/tmp") / f"mc_{abs(hash(url))}.txt"
    names: list[str] = []
    if _download(url, None, out, timeout=90):
        txt = re.sub(r"\x1b\[[0-9;]*m", "", out.read_text(errors="ignore"))
        # each entry: <14-digit date> <size like 12.9M / 182K / 5B> <name>
        names = [n.strip() for n in re.findall(r"^\d{14}\s+[\d.]+[KMGTB]*\s+(.+)$", txt, re.M)
                 if n.strip().lower().endswith(".wad")]
        out.unlink(missing_ok=True)
    _MC[url] = names
    return names


def _candidates(src: dict, ra: dict, ni_by_md5: dict, cookies: str):
    """Yield (download_url, dest_basename, display_name) for a source, best first."""
    hashes = ra.get("hashes", [])
    pref = ra.get("preferred_hash")
    ordered = sorted(hashes, key=lambda x: 0 if x.get("md5") == pref else 1)
    if src["kind"] == "cart":
        zipname = src["zip"]
        seen = set()

        def cart(cand):
            if cand and cand not in seen:
                seen.add(cand)
                return (f"{NIROMS}/{quote(zipname)}/{quote(cand)}.zip", f"{cand}.zip", cand)
        for h in ordered:                                     # phase 1: hash + RA name
            md5 = (h.get("md5") or "").lower()
            for c in (ni_by_md5.get(md5), re.sub(r"\.\w{1,4}$", "", h.get("name", "")) or None):
                r = cart(c)
                if r:
                    yield r
        idx = _member_index(zipname, cookies)                 # phase 2: fuzzy vs listing
        for h in ordered:
            for m in idx.get(_norm(re.sub(r"\.\w{1,4}$", "", h.get("name", ""))), []):
                r = cart(m)
                if r:
                    yield r
    elif src["kind"] == "mariocube":       # alphabetized WiiWare/VC WAD repo (not archive.org)
        base = src["base"].rstrip("/")
        key = _title_key(ra.get("title", ""))
        first = next((c for c in _norm(ra.get("title", "")) if c.isalnum()), "")
        sub = "0-9" if first.isdigit() else (first or "A")
        seen = set()
        for name in _mc_names(quote(f"{base}/{sub}/", safe=":/")):  # encode spaces/commas/&
            nk = _title_key(name)
            if (nk == key or (key and key in nk)) and name not in seen:
                seen.add(name)
                yield (quote(f"{base}/{sub}/{name}", safe=":/"), Path(name).name, name)
    else:  # disc item — match RA title against the item's file listing
        idx = _item_index(src["item"], cookies)
        # ORDER matters, so this is a list not a set: keys built from RA HASH NAMES come first
        # (preferred hash first, via `ordered`), the RA display title last. RA's dump is often
        # titled differently from the game — RA 34612 "Marble Saga: Kororinpa" wants the dump
        # "Marbles! Balance Challenge (Europe)" — and a set's arbitrary iteration order would
        # let the display-title key pull down unrelated regional dumps first.
        keys: list[str] = []
        for h in ordered:
            k = _title_key(h.get("name", ""))
            if k and k not in keys:
                keys.append(k)
        tk = _title_key(ra.get("title", ""))
        if tk and tk not in keys:
            keys.append(tk)
        seen, matched = set(), False

        def disc(fn):
            return (f"{ARCHIVE}/download/{quote(src['item'])}/{quote(fn)}", Path(fn).name, fn)
        # One title-key can cover a dozen region/revision dumps ("Disney-Pixar WALL-E" matches
        # 14 files), but RA wants exactly ONE of them. Yield the file whose basename IS an RA
        # hash name first — otherwise we download ~1.9GB wrong-region discs until the gate
        # happens to hit the right one. Order within each tier is preserved.
        ra_names = {(h.get("name") or "").strip().lower()
                    for h in ra.get("hashes", []) if h.get("name")}

        def _exact_first(fns: list) -> list:
            if not ra_names:
                return fns
            def rank(fn):
                stem = Path(fn).name.rsplit(".", 1)[0].strip().lower()
                return 0 if stem in ra_names else 1
            return sorted(fns, key=rank)
        for k in keys:
            for fn in _exact_first(idx.get(k, [])):
                if fn not in seen:
                    seen.add(fn)
                    matched = True
                    yield disc(fn)
        if not matched:   # archive filename differs (typo / dropped 's) — fuzzy, hash still verifies
            import difflib
            gk = _title_key(ra.get("title", ""))
            scored = sorted(((difflib.SequenceMatcher(None, gk, fk).ratio(), fn)
                             for fk, fns in idx.items() for fn in fns), reverse=True)
            for r, fn in scored[:3]:
                if r < 0.82:
                    break
                if fn not in seen:
                    seen.add(fn)
                    yield disc(fn)


_DISC_EXT = (".iso", ".rvz", ".gcm", ".7z", ".zip", ".rar", ".nkit", ".cso", ".chd", ".bin", ".cue")
_DISC_RE = re.compile(r"\s*\((?:Disc|CD|Disk)\s*\d+(?:\s*/\s*\d+)?\)|\s*-\s*CD\s*\d+\b", re.I)


def _strip_disc(name: str) -> str:
    """Drop a "(Disc 1)"/"(Disc 1/2)"/"- CD1" marker so a multi-disc member reads as the base title."""
    return _DISC_RE.sub("", name or "").strip()


def _article_variants(title: str) -> set[str]:
    """"The Foo" ↔ "Foo, The" (both directions) so a leading-article DB title matches a
    Redump comma-suffixed dump name and vice-versa."""
    out = {title}
    m = re.match(r"^(The|A|An)\s+(.+)$", title, re.I)
    if m:
        out.add(f"{m.group(2)}, {m.group(1)}")
    m = re.match(r"^(.+),\s*(The|A|An)$", title, re.I)
    if m:
        out.add(f"{m.group(2)} {m.group(1)}")
    return {t.strip() for t in out if t.strip()}


def _clean_title(t: str) -> str:
    """RA display titles carry category tags (~Homebrew~, ~Demo~) that no storefront knows."""
    return re.sub(r"~[^~]*~", "", t or "").strip()


def _search_titles(ra: dict) -> list[str]:
    """Plain (region/disc/tag-stripped) title strings to query archive.org with — derived from
    the RA display title AND each region-tagged hash name, plus article-reordered forms."""
    seen, out = set(), []
    cands = {re.sub(r"~[^~]*~", "", ra.get("title", "")).strip()}
    for h in ra.get("hashes", []):
        nm = h.get("name") or ""
        if nm:
            # strip disc marker, then region/lang parens + brackets → plain game name
            base = re.sub(r"\s*\([^)]*\)|\s*\[[^\]]*\]", "", _strip_disc(nm)).strip()
            if base:
                cands.add(base)
    for c in cands:
        for v in _article_variants(c):
            k = _norm(v)
            if v and k not in seen:
                seen.add(k)
                out.append(v)
    return out


_SEARCH_MAX_CANDIDATES = 4


# archive.org search terms that pin a query to the right console. The generic search is
# `title:(X) AND mediatype:(software)` with NO platform constraint, so a PS2 query happily
# returns PC and PS3 uploads — and because those often rank above the real disc inside the
# row cap, the console release is never even seen. Every "downloaded N file(s), none was a
# valid disc image for this console" on PS2 traced back to this: Worms 3D pulled a PC build,
# Madagascar a PS3 (BLUS) upload, Ford Racing 3 a PC .rar. Meanwhile a filtered query finds
# `slus-209.76.-ford-racing-3` and `worms-3d-ps2-csa` — the actual discs.
_PLATFORM_TERMS: dict[int, tuple[str, ...]] = {
    21: ("PS2", "PlayStation 2", "SLUS", "SLES"),        # PlayStation 2
    12: ("PS1", "PlayStation", "SLUS", "SCUS"),          # PlayStation
    41: ("PSP", "PlayStation Portable", "ULUS", "ULES"),  # PSP
    16: ("GameCube", "Nintendo GameCube", "GCN"),
    19: ("Wii", "Nintendo Wii"),
    40: ("Dreamcast", "Sega Dreamcast"),
    39: ("Saturn", "Sega Saturn"),
    9:  ("Sega CD", "Mega CD"),
}


def _platform_query(console_id: int | None) -> str:
    terms = _PLATFORM_TERMS.get(console_id or -1)
    if not terms:
        return ""
    ors = " OR ".join('subject:("%s")' % t for t in terms)
    return " AND (%s OR collection:(redump))" % ors


def _search_candidates(ra: dict, cookies: str, console_id: int | None = None):
    """Last resort: any archive.org item found by title search. Queries are built from the RA
    display title AND the region-tagged hash names (RA's real dumps are region-named, e.g.
    "Book of Watermarks, The (Japan)", which the English display title never finds). Match-keys
    include every hash name so version/region-tagged files still rank; hash-verify decides the
    build. Capped to bound big downloads."""
    titles = _search_titles(ra)
    if not titles:
        return
    keys = {_title_key(t) for t in titles}
    for h in ra.get("hashes", []):
        if h.get("name"):
            keys.add(_title_key(h["name"]))
    keys.discard("")
    seen_items: set[str] = set()
    cands = []
    md5set = {(h.get("md5") or "").lower() for h in ra.get("hashes", []) if h.get("md5")}
    # For DISC systems RA's md5 is a content hash of the boot executable, so it can never match
    # a published file hash. Redump's DAT carries the file-level md5 of each canonical dump, so
    # add those: a candidate whose published md5 is a known Redump disc image for this console
    # is a genuine dump and worth the download; anything else is a re-rip or another platform.
    if console_id:
        try:
            from app.services.dat_source import dat_md5s
            md5set = md5set | dat_md5s("redump", console_id)
        except Exception as e:  # noqa
            log.warning("redump dat unavailable", console=console_id, err=str(e)[:120])
    plat = _platform_query(console_id)
    # Platform-pinned query FIRST so the real console release is seen before the row cap is
    # reached; the unfiltered query still runs after it as a fallback for items with no
    # platform metadata.
    queries = []
    for title in titles[:4]:            # cap queries to stay gentle on archive.org
        if plat:
            queries.append(f"title:({title}) AND mediatype:(software){plat}")
        queries.append(f"title:({title}) AND mediatype:(software)")
    for raw_q in queries:
        q = quote(raw_q)
        data = _get_json(f"{ARCHIVE}/advancedsearch.php?q={q}&fl[]=identifier&rows=8&output=json")
        for doc in ((data or {}).get("response", {}) or {}).get("docs", [])[:8]:
            item = doc.get("identifier")
            if not item or item in seen_items:
                continue
            seen_items.add(item)
            # Certain matches first: a published md5 RA accepts needs no name guessing at all.
            for f in _md5_matches(item, md5set, cookies):
                nm = f.get("name") or ""
                log.info("archive.org file matches an RA md5 — no name guessing needed",
                         item=item, file=nm[:70], md5=f.get("md5"))
                # cands entries are (rank, item, filename) — the URL is built at yield time.
                # Rank 1.5 puts a hash-certain file ahead of every name-similarity guess.
                cands.append((1.5, item, nm))
            for fk, fns in _item_index(item, cookies).items():
                r = 1.0 if fk in keys else max(
                    (difflib.SequenceMatcher(None, k, fk).ratio() for k in keys), default=0.0)
                for fn in fns:
                    if fn.lower().endswith(_DISC_EXT):
                        cands.append((r, item, fn))
    # 0.60 was far too permissive for a WHOLE-DISC download. "Worms 3D" scores ~0.77 against a
    # bare "Worms" and ~0.73 against "Worms 3D PL DVD", so a PS2 search happily pulled PC ISOs,
    # a Polish DVD and an asset archive (AndysTestCube.xom out of Worms3D_PS2_CSA.ZIP) — twelve
    # downloads, several hundred MB each, none of them a PS2 disc. Match the 0.82 bar the
    # item-based disc matcher already uses, and cap the attempts: past a handful of near-misses
    # this is guessing, and every guess is a multi-hundred-MB transfer.
    seen_fn, emitted = set(), 0
    for r, item, fn in sorted(cands, key=lambda x: x[0], reverse=True):
        if r < 0.82 or emitted >= _SEARCH_MAX_CANDIDATES:
            break
        if fn in seen_fn:
            continue
        seen_fn.add(fn)
        emitted += 1
        yield (f"{ARCHIVE}/download/{quote(item)}/{quote(fn)}", Path(fn).name, fn)


# ── Multi-disc acquisition ─────────────────────────────────────────────────────
# A multi-disc RA game has several hash entries named "(Disc 1)", "(Disc 2)", … Each disc is a
# separate content hash and must land as its own RomFile (RomFleet's convention: one file per
# disc, named after the Redump/RA hash name, e.g. "Thousand Arms (USA) (Disc 1).chd"). Sources
# expose the discs either as per-disc files (Arquivista "<Title> (Region) - CDn.chd", Redump
# "<Title>, The (Region) (Disc n).chd") or bundled in a .7z/.zip/.rar we extract on the pool.

def _disc_hashes(ra: dict) -> dict:
    """{md5 -> hash_name} for the game's numbered discs — only when ≥2 distinct disc numbers
    exist (so genuine single-disc games with alt-revision hashes are NOT treated as multi-disc)."""
    out, nums = {}, set()
    for h in ra.get("hashes", []):
        md5 = (h.get("md5") or "").lower()
        nm = h.get("name") or ""
        m = re.search(r"\((?:Disc|CD|Disk)\s*(\d+)", nm, re.I)
        if md5 and nm and m:
            out[md5] = nm
            nums.add(int(m.group(1)))
    return out if len(nums) >= 2 else {}


def _disc_source_files(sources: list[dict], ra: dict, cookies: str):
    """Yield (url, basename, display) for every disc-image/archive file in the registered disc
    sources whose disc-stripped title-key matches the game — i.e. all discs of the set."""
    keys = {_title_key(ra.get("title", ""))}
    for h in ra.get("hashes", []):
        if h.get("name"):
            keys.add(_title_key(h["name"]))
    keys.discard("")
    for src in sources:
        if src.get("kind") in ("cart", "mariocube"):
            continue
        item = src.get("item")
        if not item:
            continue
        idx = _item_index(item, cookies)
        for k in keys:
            for fn in idx.get(k, []):
                if fn.lower().endswith(_DISC_EXT):
                    yield (f"{ARCHIVE}/download/{quote(item)}/{quote(fn)}", Path(fn).name, fn)


_ARCHIVE_EXT = (".7z", ".zip", ".rar")
_IMG_EXT = (".chd", ".iso", ".rvz", ".gcm", ".nkit", ".cso", ".bin", ".cue", ".img",
            # Floppy formats. Home-computer multi-disk games are distributed as ONE zip
            # holding every disk, so _targets() must be able to see the members — with
            # only optical extensions here it extracted the archive, found nothing, and
            # fell through to hashing the .zip as a single unit. That yields one hash
            # (the first disk) and mismatches whenever a later disk is the one wanted:
            # La Femme (Apple II) shipped Disk 1 + Disk 2 + .m3u in one zip and was
            # unsourceable because only Disk 2 was missing (2026-08-15). Same shape
            # blocks pc8800 (.d88) and msx.
            ".dsk", ".d88", ".woz", ".do", ".po", ".2mg", ".nib",
            ".di1", ".di2", ".fdi", ".xdf", ".d64", ".adf", ".hdm")


_REL_RANK = (("(usa", 0), ("(world", 1), ("(europe", 2), ("(australia", 3), ("(japan", 4))


def _rel_rank(key: str) -> int:
    k = key.lower()
    for tag, r in _REL_RANK:
        if tag in k:
            return r
    return 8


def _target_discs(ra: dict, disc_map: dict, owned: set) -> dict:
    """{md5 -> RA hash name} for the discs still WANTED, restricted to a single release.

    disc_map is every disc of every region RA lists — 12 entries for Driver 2 (USA, Europe,
    France, Germany, Spain, Italy). Chasing all of them is neither 1G1V nor what the gate
    asks for: on 2026-08-13 it fetched Europe 1+2 AND USA 1+2 for one game, and re-bought
    Oddworld's disc 2 because nothing subtracted the discs already on disk.

    So: finish the release we have already started (most discs owned wins), and only when we
    hold none of them fall back to a region preference.
    """
    sets = _disc_sets(ra)
    if not sets:
        return {m5: n for m5, n in disc_map.items() if m5 not in owned}
    scored = [(sum(1 for m5 in d.values() if m5 in owned), k) for k, d in sets.items()]
    started = [(c, k) for c, k in scored if c]
    if started:
        started.sort(key=lambda t: (-t[0], _rel_rank(t[1]), t[1]))
        key = started[0][1]
    else:
        key = min(sets, key=lambda k: (_rel_rank(k), k))
    return {m5: disc_map[m5] for m5 in sets[key].values()
            if m5 in disc_map and m5 not in owned}


async def _acquire_multidisc(db, sysc, folder: str, g: dict, ra: dict, sources: list[dict],
                             cookies: str, canon: Path, hasher, disc_map: dict,
                             allow_search: bool) -> dict:
    """Download/extract candidate files on the POOL, hash each disc image, and onboard EVERY
    disc whose hash matches an as-yet-unowned RA disc hash — each as its own RomFile named after
    its RA hash name. Returns {md5 -> filename} for the discs onboarded."""
    from app.db.models import RomFile as _RF
    owned = {(r.computed_hash or "").lower()
             for r in db.query(_RF).filter(_RF.system_folder == folder).all()
             if r.computed_hash}
    remaining = _target_discs(ra, disc_map, owned)   # md5 -> RA hash name, still to acquire
    wanted_n = len(remaining)
    onboarded: dict = {}
    if not remaining:
        return onboarded, wanted_n
    stage = Path(settings.canonical_path) / "_staging" / "sourcing" / f"g{g['id']}"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)

    def _targets(tmp: Path, base: str) -> list[Path]:
        """Extract a downloaded .7z/.zip/.rar to its disc images, or pass a bare image through."""
        if base.lower().endswith(_ARCHIVE_EXT):
            exdir = stage / (base + "_x")
            exdir.mkdir(exist_ok=True)
            subprocess.run(["7z", "x", "-y", f"-o{exdir}", str(tmp)],
                           capture_output=True, timeout=3600)
            tmp.unlink(missing_ok=True)
            return [f for f in sorted(exdir.rglob("*"))
                    if f.is_file() and f.suffix.lower() in _IMG_EXT]
        return [tmp]

    async def _process(targets: list[Path], prov: dict | None = None):
        """Hash each candidate image; onboard any that matches a still-wanted disc hash.

        `prov` describes where these bytes came from — a multi-disc set is unpacked from ONE
        download but yields several RomFiles, so each disc records the same origin.
        """
        for tf in targets:
            if not remaining:
                break
            try:
                got = await hasher.hash_file(str(tf))
            except Exception:  # noqa
                got = None
            if got in remaining:
                name = remaining.pop(got)
                # RA hash names on computer systems already include the extension
                # ("Fangs (Disk 2).d88", "La Femme ... (Disk 2).dsk"), so appending the
                # suffix unconditionally produced "....dsk.dsk" (2026-08-15).
                ext = tf.suffix.lower()
                dest = canon / (name if name.lower().endswith(ext) else f"{name}{ext}")
                shutil.move(str(tf), str(dest))
                hit = {"ra_game_id": g["id"], "title": ra.get("title", g["title"]),
                       "num_achievements": ra.get("num_achievements", 0),
                       "points": ra.get("points", 0)}
                game = _get_or_create_game(db, hit, sysc.ra_console_id)
                _upsert_rom(db, dest, folder, sysc.hash_method, got, "matched",
                            name, game.id, {"ra": name}, None,
                            dict(prov or {}, member=tf.name) if prov else None)
                db.commit()
                onboarded[got] = f"{name}{tf.suffix.lower()}"

    def cands():
        yield from _disc_source_files(sources, ra, cookies)
        if allow_search:
            yield from _search_candidates(ra, cookies, sysc.ra_console_id)

    try:
        seen_url = set()
        for url, base, disp in cands():           # HTTP disc/search sources
            if not remaining:
                break
            if url in seen_url:
                continue
            seen_url.add(url)
            tmp = stage / base
            ok = False
            for _attempt in range(2):    # discs are large; retry once on a flaky transfer so a
                if await asyncio.to_thread(_download, url, cookies, tmp, timeout=2400):
                    ok = True
                    break
                tmp.unlink(missing_ok=True)
            if not ok:
                continue
            await _process(await asyncio.to_thread(_targets, tmp, base),
                           _prov("disc", url))
        for src in sources:                       # torrent disc sources (Redump-in-torrent)
            if not remaining or src.get("kind") != "torrent":
                continue
            for ref, fi, path in _torrent_candidates(src, ra, cookies):
                if not remaining:
                    break
                fetched = await asyncio.to_thread(_torrent_fetch, ref, fi, path, stage, cookies)
                if fetched:
                    await _process(await asyncio.to_thread(_targets, fetched, Path(path).name),
                                   _prov("torrent", src.get("torrent_url"),
                                         file_index=fi, member=str(path)))
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return onboarded, wanted_n


async def _ra_game(es, game_id: int) -> dict | None:
    r = await es.get(index=settings.es_index_games, id=str(game_id))
    return r.get("_source") if r else None


# Homebrew Hub databases (github repos feeding hh.gbdev.io) by RA console id:
# (repo, default branch, rom extensions). All share the entries/<slug>/<rom> layout, and
# their ROMs hash to the RA hash under that console's hasher (NES = iNES-strip, others raw).
_HB_DBS: dict[int, tuple] = {
    4: ("gbdev/database", "master", (".gb", ".gbc")),        # Game Boy
    6: ("gbdev/database", "master", (".gb", ".gbc")),        # Game Boy Color (same repo)
    5: ("gbadev-org/games", "master", (".gba",)),            # Game Boy Advance
    7: ("nesdev-org/homebrew-db", "master", (".nes",)),      # NES
}
_HB_INDEX: dict[str, dict] = {}


# Per-game GitHub repos. Arduboy homebrew is published as a repo per game rather than in any
# curated set: ArduLifts is in none of TOSEC - Arduboy, No-Intro - Arduboy Inc - Arduboy, or the
# [RA Sets] Arduboy collection (all three checked), but RyCuLe/Arduboy_ArduLifts holds
# ArduLifts.arduboy hashing to RA's md5 exactly. itch has no copy either. Searching GitHub by
# title is the only route that reaches these, and the RA hash gate still decides.
_GH_EXTS = {71: (".arduboy", ".hex")}          # RA console 71 = Arduboy


def _github_candidates(ra: dict, console_id: int):
    exts = _GH_EXTS.get(console_id)
    if not exts:
        return
    title = _clean_title(ra.get("title", ""))
    if not title:
        return
    seen: set = set()
    for q in (f"{title} arduboy", title):
        data = _get_json("https://api.github.com/search/repositories?q="
                         + quote(q) + "&per_page=5")
        for repo in (data or {}).get("items", [])[:5]:
            full = repo.get("full_name")
            if not full or full in seen:
                continue
            seen.add(full)
            tree = _get_json(f"https://api.github.com/repos/{full}/git/trees/HEAD?recursive=1")
            for t in (tree or {}).get("tree", []):
                path = t.get("path", "")
                if t.get("type") == "blob" and path.lower().endswith(exts):
                    yield (f"https://raw.githubusercontent.com/{full}/HEAD/{quote(path)}",
                           path.rsplit("/", 1)[-1], f"{full}/{path}")
        if seen:
            break


def _hb_index(repo: str, branch: str, exts: tuple) -> dict:
    """A Homebrew Hub repo -> {entry slug: [raw github rom urls]}, cached per repo. These are
    hash-verifiable homebrew ROMs downloadable straight from GitHub (no JS/bot wall)."""
    if repo not in _HB_INDEX:
        data = _get_json(f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1")
        idx: dict = {}
        for t in (data or {}).get("tree", []):
            p = t.get("path", "")
            if p.startswith("entries/") and p.lower().endswith(exts):
                idx.setdefault(p.split("/")[1], []).append(
                    f"https://raw.githubusercontent.com/{repo}/{branch}/{quote(p)}")
        _HB_INDEX[repo] = idx
    return _HB_INDEX[repo]


def _hbhub_candidates(ra: dict, console_id: int):
    """Homebrew Hub ROMs whose entry slug fuzzy-matches the RA title, best-first. The
    hash-verify in _try rejects any wrong match, so a loose threshold is safe."""
    db = _HB_DBS.get(console_id)
    if not db:
        return
    idx = _hb_index(*db)
    key = re.sub(r"[^a-z0-9]+", "-", ra.get("title", "").lower()).strip("-")
    for slug in sorted(idx, key=lambda s: difflib.SequenceMatcher(None, key, s).ratio(),
                       reverse=True)[:6]:
        if difflib.SequenceMatcher(None, key, slug).ratio() < 0.5:
            break
        for url in idx[slug]:
            name = unquote(url.split("/")[-1])
            yield url, name, Path(name).stem


# Planet Virtual Boy (virtual-boy.com) — VB homebrew (RA console 28). HTTP-scrapable:
# /homebrew/<slug>/downloads/ -> download page /downloads/<id>/ -> /downloads/<id>/get/<fileid>
# serves the raw headerless .vb (plain curl -L works; verified Virtual Picross 2026-07-18).
_PLANETVB = "https://www.virtual-boy.com"


def _planetvb_candidates(ra: dict):
    """Resolve an RA VB-homebrew title to Planet Virtual Boy download URLs, best-first.
    The hash-verify in _try rejects any wrong match, so a fuzzy slug is safe."""
    title = re.sub(r"^~[^~]+~\s*", "", ra.get("title", "")).strip()   # drop ~Homebrew~/~Hack~ tag
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if not slug:
        return
    names = [h.get("name") for h in ra.get("hashes", []) if h.get("name")]
    dest_name = names[0] if names else f"{title}.vb"
    disp = Path(dest_name).stem
    listing = _get_text(f"{_PLANETVB}/homebrew/{slug}/downloads/")
    if not listing:
        return
    for did in list(dict.fromkeys(re.findall(r"/downloads/(\d+)/", listing)))[:4]:
        page = _get_text(f"{_PLANETVB}/downloads/{did}/")
        if not page:
            continue
        m = re.search(r"/downloads/\d+/get/\d+", page)
        if m:
            yield f"{_PLANETVB}{m.group(0)}", dest_name, disp


def _reap_sourcing_stage(max_age_h: float = 2.0) -> int:
    """Delete staging dirs left behind by a killed run.

    Both the sourcing loop and patching wrap their staging in try/finally, but a finally never
    runs when the process is killed outright -- a timeout cap, a service restart, a SIGKILL --
    so a multi-GB candidate or patch base is simply abandoned. Nothing else ever collects them.
    Covers the sourcing per-game dirs and patching's rfpatch_* dirs.

    Only dirs untouched for max_age_h are removed, so this can never delete a live download out
    from under a concurrent run. Deliberate keeps (_mislabeled_backup and friends) are outside
    both patterns and never touched.
    """
    base = Path(settings.canonical_path) / "_staging"
    if not base.is_dir():
        return 0
    cutoff = time.time() - max_age_h * 3600
    victims = []
    src = base / "sourcing"
    if src.is_dir():
        victims += [d for d in src.iterdir() if d.is_dir()]
    victims += [d for d in base.glob("rfpatch_*") if d.is_dir()]
    n = 0
    for d in victims:
        try:
            if d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        except OSError:
            continue
    if n:
        log.info("reaped abandoned staging dirs", count=n)
    return n



# ── Multi-disc awareness ──────────────────────────────────────────────────────
# RA registers every disc of a multi-disc game as its own accepted hash, so the FIRST
# hash-matched file satisfied the gate and `_try` returned immediately -- leaving discs 2..N
# unsourced forever. That is how 65 owned games ended up unplayable while the library
# reported 100% coverage (audited 2026-08-12). Sourcing must complete the SET, not the gate.
_SRC_DISC_RE = re.compile(r"\((?:Disc|Disk|CD)\s*([0-9IVX]+)\)|(?:^|[ _\-])CD\s*([0-9]+)\b", re.I)
_SRC_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8}


def _src_disc_no(name: str):
    m = _SRC_DISC_RE.search(name or "")
    if not m:
        return None
    v = (m.group(1) or m.group(2) or "").upper()
    return _SRC_ROMAN.get(v, int(v) if v.isdigit() else None)


def _disc_sets(ra: dict) -> dict:
    """release-key -> {disc_no: md5} for every multi-disc release RA lists for this game."""
    out: dict = {}
    for h in ra.get("hashes", []) or []:
        nm = h.get("name") or ""
        d = _src_disc_no(nm)
        if d is None:
            continue
        key = _SRC_DISC_RE.sub("", nm).strip().lower()
        out.setdefault(key, {})[d] = (h.get("md5") or "").lower()
    return {k: v for k, v in out.items() if len(v) > 1}


async def source_missing(folder: str, limit: int | None = None, allow_search: bool = True,
                         titles: list[str] | None = None, ids: list[int] | None = None) -> dict:
    sysc = get_system_by_folder(folder)
    if not sysc:
        raise ValueError("unknown system")
    sources = _sources_for(folder)
    # No registered archive source is NOT fatal. The per-game loop still computes `wanted`
    # (so shared-console routing — e.g. RA console 27 arcade/neogeo/naomi/atomiswave — can
    # tell which sub-system a set actually belongs to) and can still reach the search / patch
    # / torrent / homebrew fallbacks; only the cart/disc source candidates are skipped.
    cookies = _cookies()
    if not cookies:
        return {"error": "archive.org not configured (run: ia configure)"}
    _reap_sourcing_stage()

    from app.core.elasticsearch import get_es, init_es, close_es
    from app.services.missing import missing_for_system
    from app.services.dat_source import dat_path, parse_dat, md5_index
    from app.db.session import SessionLocal

    ni_path = dat_path("no-intro", sysc.ra_console_id)
    ni_by_md5 = md5_index(parse_dat(ni_path)) if ni_path else {}
    m = await missing_for_system(folder)
    wanted = [g for g in m["missing"] if g.get("id")]
    if titles:
        wanted = [g for g in wanted if g["title"] in titles]
    if ids is not None:
        idset = set(ids)
        wanted = [g for g in wanted if g["id"] in idset]
    wanted = wanted[:(limit or 100000)]
    hasher = get_hasher(sysc.hash_method)
    canon = Path(settings.canonical_path) / "roms" / folder
    canon.mkdir(parents=True, exist_ok=True)

    results = {"system": folder, "wanted": len(wanted), "sourced": 0, "not_found": 0, "errors": 0,
               "via_cart": 0, "via_disc": 0, "via_search": 0, "via_patch": 0, "via_hbhub": 0,
               "via_planetvb": 0, "via_mariocube": 0, "via_torrent": 0, "via_itch": 0, "added": [],
               "failures": {}}   # ra game_id -> why it couldn't be produced (patch_outlook)
    await init_es()
    db = SessionLocal()
    try:
        for g in wanted:
            try:
                ra = await _ra_game(get_es(), g["id"])
                if not ra:
                    results["not_found"] += 1
                    continue
                md5set = {(h.get("md5") or "").lower() for h in ra.get("hashes", []) if h.get("md5")}
                # What actually happened this attempt, so a failure can say WHY rather than
                # leaving "sourceable but still missing" unexplained on the Missing page.
                attempt = {"candidates": 0, "downloaded": 0, "dl_failed": 0,
                           "hash_mismatch": 0, "hash_error": 0, "hash_reject": 0}

                # Download candidates into _staging, NOT straight into the library. Every
                # normal reject path below unlinks, but nothing survives the process dying
                # between "download finished" and "hash rejected it" — a 2h timeout cap, a
                # service restart, a kill. That left a 1.5 GB Xbox Series X build of
                # Psychonauts 2 and a Russian Spyro ISO sitting in /roms/ps2, which a later
                # ingest would happily file as unmatched ROMs in a strict-1G1R tree.
                # _staging lives beside roms/ rather than inside it, so _iter_rom_files never
                # walks it, and the final move is same-dataset (instant, not a copy).
                dl_stage = Path(settings.canonical_path) / "_staging" / "sourcing" / f"g{g['id']}"

                got_hashes: set = set()      # md5s obtained for THIS game in this run
                from app.db.models import RomFile as _RF   # local: module has no model imports
                owned_now = {(r.computed_hash or "").lower()
                             for r in db.query(_RF).filter(
                                 _RF.system_folder == folder).all()
                             if r.computed_hash}

                async def _try(cands):
                    # `accept` is a LOCAL copy of md5set. Assigning to md5set directly inside
                    # this function would make it local for the whole body and the read below
                    # would raise UnboundLocalError before assignment — which is exactly what
                    # broke every multi-disc game on 2026-08-13.
                    accept = set(md5set) - owned_now
                    # Seeding accept with EVERY hash RA lists re-buys discs we already hold:
                    # the first candidate offered for Oddworld: Abe's Exoddus was its CD2, we
                    # already had disc 2, it matched, onboarded, and only THEN did the loop
                    # work out that disc 1 was the gap (2026-08-13 — 335 MB duplicate).
                    #
                    # And when we already hold part of a release, finish THAT release. Without
                    # this a stray Europe disc 1 matches, opens a second release, and the
                    # completion loop faithfully pulls all four Europe discs alongside the USA
                    # disc 2 we started with. 1G1V picks a release, then takes all ITS discs.
                    _started = {k: d for k, d in _disc_sets(ra).items()
                                if any(m5 in owned_now for m5 in d.values())}
                    if _started:
                        accept = {m5 for d in _started.values()
                                  for m5 in d.values() if m5 not in owned_now}
                        log.info("completing the release we already hold",
                                 game=g.get("title"),
                                 releases=[k[:48] for k in _started],
                                 need_hashes=len(accept))
                    seen = set()
                    for url, dest_name, disp in cands:
                        if dest_name in seen:
                            continue
                        seen.add(dest_name)
                        attempt["candidates"] += 1
                        dl_stage.mkdir(parents=True, exist_ok=True)
                        dest = dl_stage / dest_name
                        # MUST go through a thread: _download shells out to curl via a blocking
                        # subprocess.run, and a multi-GB disc holds the event loop for the whole
                        # transfer. That starves /api/systems, which the romfleet-health watchdog
                        # polls every 2min — two strikes and it restarts the API, SIGTERMing curl
                        # mid-download (seen 2026-08-05: Onimusha killed at 3.3GB, curl_exit=-15).
                        # Cart ROMs finish too fast to trip it; PS2 discs tripped it nearly always,
                        # which is what made PS2's auto-source rate look so much worse than the rest.
                        if not await asyncio.to_thread(_download, url, cookies, dest):
                            attempt["dl_failed"] += 1
                            dest.unlink(missing_ok=True)
                            continue
                        attempt["downloaded"] += 1
                        try:
                            got = await hasher.hash_file(str(dest))
                        except Exception as e:  # noqa
                            # A hasher blow-up used to collapse into got=None, indistinguishable
                            # from an honest hash mismatch. But there are TWO kinds, and calling
                            # both a "toolchain problem" is its own wrong answer: rahash saying
                            # "failed to hash <file>" means it read the file and it is not a
                            # valid image for this console (a PC ISO or an asset archive that a
                            # loose search dragged in) — that is a WRONG FILE. A missing binary,
                            # timeout or crash is the actual toolchain fault.
                            got = None
                            msg = str(e)
                            rejected = "failed to hash" in msg.lower()
                            attempt["hash_reject" if rejected else "hash_error"] += 1
                            log.warning(
                                "hasher rejected file (not a valid image for this console)"
                                if rejected else "hash FAILED — hasher/toolchain error",
                                game=g.get("title"), file=dest.name,
                                size_mb=round(dest.stat().st_size / 1e6) if dest.exists() else 0,
                                error=msg[:200])
                        if got not in accept:
                            if got:
                                attempt["hash_mismatch"] += 1
                                log.info("hash mismatch — file is a different dump",
                                         game=g.get("title"), file=dest.name,
                                         got=got, wanted=sorted(md5set)[:3],
                                         size_mb=round(dest.stat().st_size / 1e6) if dest.exists() else 0)
                            dest.unlink(missing_ok=True)
                            continue
                        # Verified: promote into the library. Only a hash-matched file ever
                        # gets a path under roms/.
                        final = canon / dest_name
                        final.parent.mkdir(parents=True, exist_ok=True)
                        dest.replace(final)
                        hit = {"ra_game_id": g["id"], "title": ra.get("title", g["title"]),
                               "hash_name": disp, "num_achievements": ra.get("num_achievements", 0),
                               "points": ra.get("points", 0)}
                        game = _get_or_create_game(db, hit, sysc.ra_console_id)
                        _upsert_rom(db, final, folder, sysc.hash_method, got, "matched",
                                    disp, game.id, {"ra": disp}, None,
                                    _prov(src.get("kind", "http") if isinstance(src, dict) else "http",
                                          url, member=dest_name))
                        db.commit()
                        got_hashes.add(got)
                        # For a multi-disc release, one disc satisfies the GATE but not the
                        # GAME. Keep pulling candidates until every disc of a release we have
                        # started is present; only then call it done.
                        sets = _disc_sets(ra)
                        if sets:
                            # Complete exactly ONE release — the one this download belongs to.
                            # Chasing "any started release" cascades: a stray Italy disc makes
                            # Italy 'started but incomplete', which keeps the loop alive and
                            # drags in yet another release. That is how Metal Gear Solid ended
                            # up with Europe 1+2, Italy 1 AND USA Rev1 1+2 (2026-08-13).
                            target = next((k for k, d in sets.items() if got in d.values()), None)
                            if target is None:
                                return True                      # not part of a multi-disc set
                            discs = sets[target]
                            have = {d for d, m5 in discs.items()
                                    if m5 in got_hashes or m5 in owned_now}
                            need = sorted(set(discs) - have)
                            if not need:
                                return True                      # this release is complete
                            wanted_md5 = {m5 for d, m5 in discs.items() if d in need}
                            log.info("multi-disc release incomplete — continuing",
                                     game=g.get("title"), release=target[:48],
                                     have=sorted(have), need=need)
                            # Only accept the remaining discs OF THIS RELEASE from here on.
                            accept = wanted_md5
                            continue
                        return True
                    return False

                async def _try_torrent(cands):
                    """Torrent onboard: selective-download set-torrent files to a POOL staging
                    dir, extract .7z/.zip, hash-verify against the gate, and onboard — same
                    discipline as _try, different (aria2c) transport.

                    Completes the SET, not just the gate. This used to `return True` on the first
                    matching file, which is the identical bug fixed in _try and
                    _acquire_multidisc but never here: Princess Maker has all seven of its
                    wanted disks sitting in the [RA Sets] MSX torrent, and this onboarded Disk 1
                    and stopped, leaving the game reported as missing 2-7 (2026-08-16).
                    """
                    stage = Path(settings.canonical_path) / "_staging" / "torrent" / f"g{g['id']}"
                    seen: set = set()
                    # Target what we LACK, so re-running never re-buys a disk already on disk.
                    want = {m for m in md5set if m not in owned_now} or set(md5set)
                    onboarded_any = False
                    try:
                        for ref, fi, path in cands:
                            if fi in seen:
                                continue
                            seen.add(fi)
                            shutil.rmtree(stage, ignore_errors=True)
                            stage.mkdir(parents=True, exist_ok=True)
                            fetched = _torrent_fetch(ref, fi, path, stage, cookies)
                            if not fetched:
                                continue
                            if fetched.suffix.lower() in _ARCHIVE_EXT:
                                exdir = stage / "x"
                                exdir.mkdir(exist_ok=True)
                                subprocess.run(["7z", "x", "-y", f"-o{exdir}", str(fetched)],
                                               capture_output=True, timeout=1800)
                                roms = [f for f in sorted(exdir.rglob("*")) if f.is_file()]
                            else:
                                roms = [fetched]
                            for rf in roms:
                                try:
                                    got = await hasher.hash_file(str(rf))
                                except Exception:  # noqa
                                    got = None
                                if got not in want:
                                    continue
                                disp = rf.stem
                                dest = canon / rf.name
                                shutil.move(str(rf), str(dest))
                                hit = {"ra_game_id": g["id"], "title": ra.get("title", g["title"]),
                                       "hash_name": disp,
                                       "num_achievements": ra.get("num_achievements", 0),
                                       "points": ra.get("points", 0)}
                                game = _get_or_create_game(db, hit, sysc.ra_console_id)
                                _upsert_rom(db, dest, folder, sysc.hash_method, got, "matched",
                                            disp, game.id, {"ra": disp}, None,
                                            _prov("torrent", src.get("torrent_url"),
                                                  file_index=fi, member=str(path)))
                                db.commit()
                                got_hashes.add(got)
                                want.discard(got)
                                onboarded_any = True
                            if not want:
                                return True          # every wanted hash acquired
                        return onboarded_any
                    finally:
                        shutil.rmtree(stage, ignore_errors=True)

                hit_kind = None
                disc_map = _disc_hashes(ra)          # {md5 -> name} when this is a multi-disc set
                if disc_map:
                    onboarded, wanted_n = await _acquire_multidisc(
                        db, sysc, folder, g, ra, sources, cookies, canon, hasher,
                        disc_map, allow_search)
                    if onboarded:
                        hit_kind = "disc"
                        if len(onboarded) < wanted_n:
                            log.warning("multidisc partial", game=g.get("title"),
                                        got=len(onboarded), of=len(disc_map))
                if not hit_kind:
                    for src in sources:
                        if src.get("kind") == "torrent":     # torrent tried later (slower)
                            continue
                        if await _try(_candidates(src, ra, ni_by_md5, cookies)):
                            hit_kind = src["kind"]
                            break
                if not hit_kind and sysc.ra_console_id in (4, 6):   # GB/GBC → Homebrew Hub
                    if await _try(_hbhub_candidates(ra, sysc.ra_console_id)):
                        hit_kind = "hbhub"
                if not hit_kind and sysc.ra_console_id == 28:   # Virtual Boy → Planet Virtual Boy
                    if await _try(_planetvb_candidates(ra)):
                        hit_kind = "planetvb"
                if not hit_kind and allow_search:
                    if await _try(_search_candidates(ra, cookies, sysc.ra_console_id)):
                        hit_kind = "search"
                async def _try_cleancpc():
                    """Clean CPC DB — the database RA's Amstrad CPC hashes actually come from.

                    RA's hash page tags them "CLEAN CPC DB" (e.g. Donald's Alphabet Chase
                    (1988)(Disney)(M5)(Side A)[codes].dsk), which is why No-Intro and TOSEC
                    naming never matched: those sets carry a different dump entirely (the
                    Westwood Associates variant). Layout is one zip per initial letter, each
                    holding <Letter>/<Game folder>/<file>.dsk — so the whole letter archive is
                    fetched once and every member hashed, rather than guessing a filename.
                    """
                    title = (ra.get("title") or g.get("title") or "").strip()
                    if not title:
                        return False
                    ch = title[0].upper()
                    letter = ch if ch.isalpha() else "#"
                    zname = f"clean-cpc-db {letter} [v-1.3].zip"
                    url = "https://archive.org/download/amstrad-cpc-clean-db/" + quote(zname)
                    stage = Path(settings.canonical_path) / "_staging" / "cleancpc" / f"g{g['id']}"
                    shutil.rmtree(stage, ignore_errors=True)
                    stage.mkdir(parents=True, exist_ok=True)
                    want = {m for m in md5set if m not in owned_now} or set(md5set)
                    onboarded_any = False
                    try:
                        zf = stage / zname
                        if not await asyncio.to_thread(_download, url, cookies, zf, timeout=900):
                            return False
                        ex = stage / "x"
                        ex.mkdir(exist_ok=True)
                        await asyncio.to_thread(
                            lambda: subprocess.run(["7z", "x", "-y", f"-o{ex}", str(zf)],
                                                   capture_output=True, timeout=1800))
                        for rf in sorted(ex.rglob("*")):
                            if not rf.is_file() or not want:
                                continue
                            try:
                                got = await hasher.hash_file(str(rf))
                            except Exception:  # noqa
                                continue
                            if got not in want:
                                continue
                            disp = rf.stem
                            dest = canon / rf.name
                            shutil.move(str(rf), str(dest))
                            hit = {"ra_game_id": g["id"], "title": ra.get("title", g["title"]),
                                   "hash_name": disp,
                                   "num_achievements": ra.get("num_achievements", 0),
                                   "points": ra.get("points", 0)}
                            game = _get_or_create_game(db, hit, sysc.ra_console_id)
                            _upsert_rom(db, dest, folder, sysc.hash_method, got, "matched",
                                        disp, game.id, {"ra": disp}, None,
                                        _prov("cleancpc", url, member=rf.name))
                            db.commit()
                            got_hashes.add(got)
                            want.discard(got)
                            onboarded_any = True
                        return onboarded_any
                    finally:
                        shutil.rmtree(stage, ignore_errors=True)

                if not hit_kind and sysc.ra_console_id == 37:   # Amstrad CPC → Clean CPC DB
                    try:
                        if await _try_cleancpc():
                            hit_kind = "cleancpc"
                    except Exception as e:  # noqa
                        log.warning("clean-cpc source failed", game=g.get("title"),
                                    err=str(e)[:140])
                if not hit_kind:   # torrent set-source (slowest transport) after HTTP sources
                    for src in sources:
                        if src.get("kind") != "torrent":
                            continue
                        if await _try_torrent(_torrent_candidates(src, ra, cookies)):
                            hit_kind = "torrent"
                            break
                if not hit_kind and sysc.ra_console_id in _GH_EXTS:   # per-game GitHub repos
                    try:
                        if await _try(_github_candidates(ra, sysc.ra_console_id)):
                            hit_kind = "github"
                    except Exception as e:  # noqa
                        log.warning("github source failed", game=g.get("title"), err=str(e)[:140])
                if not hit_kind:   # itch.io — where modern homebrew actually lives
                    try:
                        from app.services.itch_source import rom_candidates
                        if await _try(rom_candidates(_clean_title(ra.get("title", g["title"])),
                                                     sysc.ra_console_id)):
                            hit_kind = "itch"
                    except Exception as e:  # noqa
                        log.warning("itch source failed", game=g.get("title"), err=str(e)[:140])
                if not hit_kind:   # last resort: it's a hack — patch an owned base disc
                    from app.services.patching import patch_game
                    # Threaded for the same reason as the download above, only more so: patching a
                    # DVD title downloads a multi-GB base disc, chdman-extracts it and runs xdelta,
                    # so in-loop it can hold the event loop for tens of minutes. `db` is safe here
                    # because the awaiting coroutine touches it again only after this returns —
                    # one thread at a time, never concurrent access to the session.
                    if await asyncio.to_thread(patch_game, db, sysc, folder, g, ra):
                        hit_kind = "patch"
                shutil.rmtree(dl_stage, ignore_errors=True)
                if hit_kind:
                    # If this game once posted a red "sourcing failed" card, go back and turn
                    # it green — otherwise Discord keeps insisting we do not have a game that
                    # is now sitting in the library.
                    try:
                        from app.services import discord_notify as _dn
                        await _dn.notify_late_success(
                            db, g["id"], ra.get("title", g["title"]),
                            sysc.ra_name or folder, (ra.get("image_boxart") or None))
                    except Exception as e:  # noqa
                        log.warning("late-success discord update failed",
                                    game=g.get("title"), err=str(e)[:140])
                    results["sourced"] += 1
                    results["via_" + hit_kind] += 1
                    results["added"].append(g["title"])
                    from app.core.events import log_event
                    log_event(db, "rf_added", f"Sourced {g['title']}", detail="from archive",
                              system=folder, data={"game_id": g["id"]})
                else:
                    results["not_found"] += 1
                    # Record WHY, so callers (the RA monitor's Discord alert) can say
                    # "Hack — no patch published" instead of the misleading generic
                    # "no hash-matching source found". None => an ordinary missing dump.
                    from app.services.patching import patch_outlook
                    why = patch_outlook(sysc, g, ra) or _attempt_reason(attempt)
                    results["failures"][str(g["id"])] = why
                    # Persist it: the Missing page shows the last attempt's outcome, which is
                    # the only way to tell "we never tried" from "we tried and it failed" —
                    # and a game the status column calls SOURCEABLE that keeps failing is a
                    # bug signal, not a fact of life.
                    from app.core.events import log_event
                    log_event(db, "source_attempt",
                              f"Could not source {g['title']} — {why}",
                              system=folder,
                              data={"game_id": g["id"], "reason": why, **attempt})
            except Exception as e:  # noqa
                results["errors"] += 1
                log.warning("source failed", game=g.get("title"), error=str(e)[:160])
        return results
    finally:
        db.close()
        await close_es()
