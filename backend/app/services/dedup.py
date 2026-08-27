"""Same-hash ROM de-duplication (1G1V cleanup).

When a system's library was sourced from two archive sets, one RA game can end up with
multiple physical files that hash IDENTICALLY — e.g. Amstrad's TOSEC `.dsk.zip` set plus
the CleanCpcDb `.zip` set both unzip to the same disk image. Same computed_hash => same
content => same RA identity, so we keep one file and drop the rest.

Keeper preference (first wins):
  1. filename (minus a trailing ".zip") == RA's canonical hash name  -> the RA-canonical file
  2. filename contains the RA hash stem                              -> close match
  3. shortest filename, then alphabetical                            -> deterministic

Only files that share a computed_hash *within one (system, game)* are touched. Different-hash
multi-file games (multi-disc, Side A/B, genuine version variants) are left completely alone.
"""
from __future__ import annotations
import re
from pathlib import Path
from collections import defaultdict
from app.db.models import RomFile
import structlog

log = structlog.get_logger()

# ── cross-naming ("fuzzy") core-title key ─────────────────────────────────────────────
# The same hack can appear under two totally different naming conventions with DIFFERENT
# hashes (a re-dump): TOSEC-style "02 - Hackerman 64 (falcobuster)" vs RA-style
# "SM64 - Hackerman 64 (Hack)". Same-hash and version-slot dedup both miss these. The core
# key drops a single leading "PREFIX - " (series or number), strips author/hack parens, and
# collapses whitespace — but KEEPS the distinguishing disc/part/vol/side/difficulty tokens so
# genuinely-distinct entries never merge.
_ROM_EXT = re.compile(r"\.(?:zip|dsk|nes|sfc|smc|z64|n64|v64|md|gbc?|gba|bin|a78|col|int)$", re.I)
_SERIES_PREFIX = re.compile(r"^[^-]{1,24}\s-\s")            # leading "SM64 - " / "LoZMM - " / "02 - "
_VOLNUM = re.compile(r"\b(vol|part|disc|disk|episode|week|map|dai)\.?\s*[-]?\s*0*(\d+)", re.I)
# keep parens that carry a distinguishing token; strip the rest (authors, (Hack), (Unl)…)
_KEEP_PAREN = re.compile(r"vol\d|part\d|disc|disk|episode|side|map|week|dai|hard|easy|normal|lite|"
                         r"beta|proto|demo", re.I)


# Parens that are catalog TAGS, not authors — everything else in parens is treated as an
# author/credit, and two files crediting DIFFERENT authors are distinct works, never dupes.
_TAG_PAREN = re.compile(
    r"^\s*(?:u|e|j|w|usa|europe|japan|world|asia|korea|en|fr|de|es|it|ja|nl|pt|sv|zh|ko|ru|"
    r"hack|unl|unlicensed|proto|prototype|demo|beta|aftermarket|sample|fix|standalone.*|"
    r"rev\s*[\w]+|v[\d.]+\w*|ver\s*[\d.]+|msu-?1?|soundlink|np|ntsc|pal|"
    r"vol\.?\s*\d+|part\s*\d+|disc\s*\d+|disk\s*\d+|episode\s*\d+|side\s*[ab]|"
    r"hard|easy|normal|lite|map\s*\d*|week\s*\d*|dai-?\d*[-\w]*|[\d\s\-!.\[\]+]+)\s*$", re.I)


def _authors(fn: str) -> set[str]:
    """Author/credit tokens from parens (excluding catalog tags). Differing author sets => the
    two files are distinct works, so the cross-naming collapse must skip them."""
    out = set()
    for m in re.finditer(r"\(([^)]*)\)", fn or ""):
        c = m.group(1).strip()
        if c and not _TAG_PAREN.match(c):
            tok = re.sub(r"[^a-z0-9]", "", c.lower())
            if tok:
                out.add(tok)
    return out


def _core_key(fn: str) -> str:
    n = _ROM_EXT.sub("", _ROM_EXT.sub("", fn or ""))           # strip .dsk.zip -> ""
    n = _VOLNUM.sub(lambda m: f" {m.group(1).lower()}{int(m.group(2))} ", n)  # Vol.05 -> vol5
    n = _SERIES_PREFIX.sub("", n)                              # one leading "PREFIX - "
    n = re.sub(r"\s*\(([^)]*)\)", lambda m: f"({m.group(1)})" if _KEEP_PAREN.search(m.group(1)) else " ", n)
    n = re.sub(r"\bhack\b", " ", n, flags=re.I)
    return re.sub(r"[^a-z0-9]+", "", n.lower())                # drop ALL separators (spacing differs)


def _keeper_key(r: RomFile):
    """Lower sorts first = preferred keeper."""
    fn = r.filename or ""
    stem = fn[:-4] if fn.lower().endswith(".zip") else fn
    ra = r.ra_hash_name or ""
    ra_stem = ra.rsplit(".", 1)[0] if "." in ra else ra
    exact = bool(ra and stem == ra)
    contains = bool(ra_stem and ra_stem in fn)
    return (0 if exact else 1, 0 if contains else 1, len(fn), fn)


def plan_dedup(db, folder: str | None = None) -> dict:
    """Group matched files by (system, game, computed_hash); any group >1 is a dup set."""
    q = db.query(RomFile).filter(RomFile.game_id.isnot(None), RomFile.computed_hash.isnot(None))
    if folder:
        q = q.filter(RomFile.system_folder == folder)
    groups: dict = defaultdict(list)
    for r in q.all():
        groups[(r.system_folder, r.game_id, r.computed_hash)].append(r)

    plan = []
    for (sysf, gid, _h), rows in groups.items():
        if len(rows) < 2:
            continue
        rows.sort(key=_keeper_key)
        keep, drop = rows[0], rows[1:]
        plan.append({"system": sysf, "game_id": gid, "keep": keep.filename, "keep_id": keep.id,
                     "drop": [{"id": d.id, "file": d.filename, "path": d.path} for d in drop]})
    plan.sort(key=lambda x: (x["system"], x["keep"]))
    per_system: dict = defaultdict(int)
    for p in plan:
        per_system[p["system"]] += len(p["drop"])
    return {"folder": folder, "groups": len(plan),
            "files_to_remove": sum(len(p["drop"]) for p in plan),
            "per_system": dict(per_system), "plan": plan}


def _version_plan_for_folder(db, folder: str, key: str) -> list[dict]:
    """Within-game version-dupe groups for one 1G1R system. key='slot' uses organize's
    _version_slot (region/lang/rev/translation only — safe); key='core' uses the cross-naming
    core title (catches TOSEC-vs-RA re-dumps — review before applying). Keeper = English wins,
    then best score (identical to organize's 1G1R primary selection)."""
    from sqlalchemy.orm import selectinload
    from app.services.organize import (_effective, _version_slot, _is_english, _score,
                                        _KEEP_ALL_1G1R, _KEEP_ALL_NAME)
    from app.core.scoring import get_scoring

    sysc, gate, selection = _effective(db, folder)
    if selection != "1g1r" or folder in _KEEP_ALL_1G1R:
        return []                     # fullset / deliberate keep-all systems: never collapse
    cfg = get_scoring(db)
    roms = db.query(RomFile).options(selectinload(RomFile.game)).filter_by(
        system_folder=folder, match_status="matched").all()
    by_game: dict = defaultdict(list)
    for r in roms:
        if r.game_id is not None:
            by_game[r.game_id].append(r)

    keyfn = _version_slot if key == "slot" else _core_key
    out = []
    for gid, group in by_game.items():
        if any(k in (r.filename or "").lower() for r in group for k in _KEEP_ALL_NAME):
            continue                  # MSU-1 / GBA-Video: every file is a distinct keeper
        by_slot: dict = defaultdict(list)
        for r in group:
            by_slot[keyfn(r.filename or "")].append(r)
        for slot, sg in by_slot.items():
            if len(sg) < 2 or not slot:
                continue
            keep = max(sg, key=lambda r: (_is_english(r, gate), -_score(r, gate, cfg)))
            keep_auth = _authors(keep.filename or "") if key == "core" else set()
            drop = []
            for r in sg:
                if r.id == keep.id:
                    continue
                # cross-naming guard: same base title but a DIFFERENT credited author == a
                # distinct work (two people's hacks of one theme), never a redump — skip it.
                if key == "core":
                    a = _authors(r.filename or "")
                    if keep_auth and a and keep_auth.isdisjoint(a):
                        continue
                drop.append(r)
            if not drop:
                continue
            out.append({"system": folder, "game_id": gid, "keep": keep.filename, "keep_id": keep.id,
                        "keep_english": _is_english(keep, gate), "slot": slot,
                        "drop": [{"id": d.id, "file": d.filename, "path": d.path,
                                  "english": _is_english(d, gate)} for d in drop]})
    return out


def plan_version_dedup(db, folder: str | None = None, key: str = "slot") -> dict:
    """Version-dupe plan across one or all 1G1R systems (see _version_plan_for_folder)."""
    from app.core.systems import load_systems
    folders = [folder] if folder else [s.folder for s in load_systems()]
    plan = []
    for f in folders:
        try:
            plan.extend(_version_plan_for_folder(db, f, key))
        except Exception as e:  # noqa
            log.warning("version dedup plan failed", folder=f, err=str(e)[:120])
    plan.sort(key=lambda x: (x["system"], x["keep"]))
    per_system: dict = defaultdict(int)
    for p in plan:
        per_system[p["system"]] += len(p["drop"])
    return {"folder": folder, "key": key, "groups": len(plan),
            "files_to_remove": sum(len(p["drop"]) for p in plan),
            "per_system": dict(per_system), "plan": plan}


def apply_version_dedup(db, folder: str | None = None, key: str = "slot") -> dict:
    """Delete non-keeper files for every version-dupe group (disk + DB)."""
    p = plan_version_dedup(db, folder, key)
    removed = freed = 0
    errors = []
    for grp in p["plan"]:
        for d in grp["drop"]:
            try:
                fp = Path(d["path"])
                if fp.is_file():
                    freed += fp.stat().st_size
                    fp.unlink()
                db.query(RomFile).filter(RomFile.id == d["id"]).delete(synchronize_session=False)
                removed += 1
            except Exception as e:  # noqa
                errors.append({"id": d["id"], "file": d["file"], "err": str(e)[:120]})
                log.warning("version dedup remove failed", file=d["file"], err=str(e)[:120])
    db.commit()
    log.info("version dedup applied", folder=folder or "*", key=key,
             removed=removed, freed_bytes=freed)
    return {"folder": folder, "key": key, "groups": p["groups"], "removed": removed,
            "freed_bytes": freed, "errors": errors, "per_system": p["per_system"]}


def apply_dedup(db, folder: str | None = None) -> dict:
    """Delete the non-keeper files (disk + DB row) for every dup group."""
    p = plan_dedup(db, folder)
    removed = freed = 0
    errors = []
    for grp in p["plan"]:
        for d in grp["drop"]:
            try:
                fp = Path(d["path"])
                if fp.is_file():
                    freed += fp.stat().st_size
                    fp.unlink()
                db.query(RomFile).filter(RomFile.id == d["id"]).delete(synchronize_session=False)
                removed += 1
            except Exception as e:  # noqa  (never abort the batch on one bad file)
                errors.append({"id": d["id"], "file": d["file"], "err": str(e)[:120]})
                log.warning("dedup remove failed", file=d["file"], err=str(e)[:120])
    db.commit()
    log.info("dedup applied", folder=folder or "*", groups=p["groups"],
             removed=removed, freed_bytes=freed)
    return {"folder": folder, "groups": p["groups"], "removed": removed,
            "freed_bytes": freed, "errors": errors, "per_system": p["per_system"]}
