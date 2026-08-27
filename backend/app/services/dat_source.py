"""DAT-based membership + selection (No-Intro / Redump / MAME — any Logiqx DAT).

Generalizes membership beyond RetroAchievements: a file is a library game if its hash
is in the chosen DAT. Selection is either 1G1R (group a game's versions and let the
priority scorer pick the primary) or full-set (every version is its own entry).

Grouping: parent/clone if the DAT has it, else normalized base title (strip region/rev/
language tags) — the same signals the scorer parses.
"""
from __future__ import annotations
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from app.core.config import settings
from app.core.hash_priority import parse_and_score
from app.core.scoring import score_parsed, DEFAULT_SCORING


DATS_ROOT = Path("/mnt/romfleet/dats")


def dat_path(source: str, ra_console_id: int) -> str | None:
    """Find the DAT for a source+console.

    Files named `console-<ra_console_id>.dat` win outright. The old behaviour — newest file in
    the directory regardless of console — was only ever safe while each source held exactly one
    system; the moment the Redump mirror's per-system DATs landed (11 of them) it would have
    handed PS2 code whichever DAT happened to sort last.
    """
    base = DATS_ROOT / source
    if not base.is_dir():
        return None
    exact = base / f"console-{ra_console_id}.dat"
    if exact.is_file():
        return str(exact)
    # No console-specific DAT: return NOTHING rather than an unrelated system's file. The old
    # "newest in the directory" fallback meant the single Sega Mega Drive No-Intro DAT was
    # handed to NES, SNES, GB and every other console — their lookups then silently found no
    # match, so No-Intro enrichment appeared to work while only ever functioning for Genesis.
    # A wrong DAT is worse than none: it looks like an answer.
    return None


# category tags No-Intro/Redump put in the name -> RomFleet category
_CAT = [("aftermarket", "Unlicensed"), ("unl", "Unlicensed"), ("pirate", "Unlicensed"),
        ("proto", "Prototype"), ("beta", "Prototype"), ("demo", "Demo"),
        ("sample", "Demo"), ("program", "Application")]


def display_meta(name: str) -> tuple[str, str | None, str | None]:
    """From a DAT entry name -> (clean_title, region, category).
    'Sonic the Hedgehog (USA, Europe)' -> ('Sonic the Hedgehog', 'USA, Europe', None)."""
    from app.core.hash_priority import parse_and_score
    title = re.sub(r"\s+", " ", _STRIP.sub("", name or "")).strip()
    ph = parse_and_score(md5="", name=name or "", labels=[], patch_url=None)
    region = ", ".join(ph.regions) if getattr(ph, "regions", None) else None
    low = (name or "").lower()
    category = next((c for tag, c in _CAT if f"({tag}" in low or f"{tag})" in low), None)
    return (title or name or "").strip(), region, category


def parse_dat(path: str) -> list[dict]:
    """[{md5, name, cloneof}] per <game> in a Logiqx DAT — one entry for the game's DISC image.

    Redump lists every track of a CD game separately, so a cue/bin title has a `.cue` rom of
    ~85 bytes followed by the multi-hundred-MB `.bin`. Taking `find("rom")` — the first — meant
    a CD game was represented by its cue sheet's md5, which matches nothing anyone hosts. Pick
    the LARGEST rom instead, which is the disc image for both cue/bin and single-track .iso.
    """
    out = []
    for _, el in ET.iterparse(path, events=("end",)):
        if el.tag == "game":
            name = el.get("name")
            best, best_sz = None, -1
            for rom in el.findall("rom"):
                if not (rom.get("md5") or ""):
                    continue
                try:
                    sz = int(rom.get("size") or 0)
                except ValueError:
                    sz = 0
                if sz > best_sz:
                    best, best_sz = rom, sz
            if best is not None:
                out.append({"md5": (best.get("md5") or "").lower(), "name": name,
                            "cloneof": el.get("cloneof"), "size": best_sz,
                            "rom_name": best.get("name")})
            el.clear()
    return out


_DAT_MD5_CACHE: dict = {}


def dat_md5s(source: str, ra_console_id: int) -> set:
    """Every disc-image md5 a DAT knows for a console.

    Used to judge an archive.org candidate from its PUBLISHED md5, before downloading it. RA's
    own hash cannot do this for disc systems — it hashes the boot executable inside the image,
    not the file — so a Redump DAT is the only way to tell a genuine dump from a re-rip without
    pulling multiple gigabytes first.
    """
    key = (source, ra_console_id)
    if key in _DAT_MD5_CACHE:
        return _DAT_MD5_CACHE[key]
    p = dat_path(source, ra_console_id)
    out = {e["md5"] for e in parse_dat(p)} if p else set()
    _DAT_MD5_CACHE[key] = out
    return out


_STRIP = re.compile(r"\s*\([^)]*\)|\s*\[[^\]]*\]")


def base_title(name: str) -> str:
    return re.sub(r"\s+", " ", _STRIP.sub("", name or "")).strip().lower()


def md5_index(entries: list[dict]) -> dict[str, str]:
    idx = {}
    for e in entries:
        idx.setdefault(e["md5"], e["name"])
    return idx


def group_1g1r(entries: list[dict], cfg: dict | None = None) -> list[dict]:
    """Group versions into games (cloneof if present, else base title) and pick the
    scorer-preferred primary per group."""
    cfg = cfg or DEFAULT_SCORING
    has_clones = any(e.get("cloneof") for e in entries)
    groups: dict[str, list[dict]] = {}
    if has_clones:
        by_name = {e["name"]: e for e in entries}
        for e in entries:
            parent = e.get("cloneof") or e["name"]
            groups.setdefault(parent, []).append(e)
    else:
        for e in entries:
            groups.setdefault(base_title(e["name"]), []).append(e)

    result = []
    for key, items in groups.items():
        scored = []
        for i in items:
            s = score_parsed(parse_and_score(md5=i["md5"], name=i["name"], labels=[], patch_url=None), cfg)
            if s is not None:
                scored.append((s, i))
        if not scored:
            scored = [(0, items[0])]
        scored.sort(key=lambda x: x[0])
        result.append({"key": key, "primary": scored[0][1],
                       "versions": [i for _, i in scored], "n": len(scored)})
    return result
