"""Gap analysis — the inverse of ingest. Ingest asks "what on disk is in the catalog?";
this asks "what's in the catalog that we DON'T own?" — the wanted list. Compares the gate
source's full catalog (RA games in ES, or a DAT's game groups) against owned games.
"""
from __future__ import annotations
import re
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.core.config import settings
from app.core.systems import get_system_by_folder, gate_list
from app.db.session import SessionLocal
from app.db.models import System, Game, RomFile


_SUBSET = re.compile(r"\[(Subset|Bonus)", re.I)


def _owned_games(db, folder: str):
    gids = [r[0] for r in db.query(RomFile.game_id).filter(
        RomFile.system_folder == folder, RomFile.game_id.isnot(None)).distinct().all()]
    return db.scalars(select(Game).where(Game.id.in_(gids)).options(
        selectinload(Game.roms))).all() if gids else []


async def missing_for_system(folder: str, source: str | None = None) -> dict:
    """`source` overrides which catalog to diff against (defaults to the system's gate)."""
    sysc = get_system_by_folder(folder)
    if not sysc:
        raise ValueError("unknown system")
    db = SessionLocal()
    try:
        srow = db.query(System).filter_by(folder=folder).one_or_none()
        primary = source or gate_list(srow.gate if srow else None, sysc.gate)[0]
        owned = _owned_games(db, folder)
        owned_hashes = {(r.computed_hash or "").lower() for g in owned for r in g.roms
                        if r.computed_hash}
        if primary == "ra":
            from app.core.systems import load_library, get_system_by_console_id
            inc_set = frozenset().union(*[load_library(n) for n in sysc.library_include]) \
                if sysc.library_include else frozenset()
            exc_set = frozenset().union(*[load_library(n) for n in sysc.library_exclude]) \
                if sysc.library_exclude else frozenset()
            # Residue (hacks/MSU-1 in no clean DAT) is split by where its file was placed:
            # a game owned in a sibling sub-folder is excluded from this system's catalog.
            sibling_hashes: set = set()
            if sysc.library_exclude:
                for sib in get_system_by_console_id(sysc.ra_console_id):
                    if sib.folder != folder and set(sib.library_include) & set(sysc.library_exclude):
                        for g in _owned_games(db, sib.folder):
                            sibling_hashes |= {(r.computed_hash or "").lower()
                                               for r in g.roms if r.computed_hash}
            return await _missing_ra(sysc.ra_console_id, owned, owned_hashes,
                                     sysc.name_include, sysc.name_exclude,
                                     inc_set, exc_set, sibling_hashes)
        if primary in ("no-intro", "lla"):
            return _missing_dat(sysc.ra_console_id, primary, owned)
        return {"gate": primary, "catalog": 0, "owned": len(owned), "missing": [],
                "note": f"no browsable catalog for gate '{primary}'"}
    finally:
        db.close()



# ── Multi-disc completeness ───────────────────────────────────────────────────
# RA registers EVERY disc of a multi-disc game as its own accepted hash, so owning any one
# of them made `owned_here` true and the game vanished from the wanted list. That is how 69
# of 95 multi-disc games came to be silently unplayable while the library reported 100%
# coverage (audited 2026-08-12): strict 1G1R treated "Disc 1" and "Disc 2" as competing
# VERSIONS and kept exactly one. The gate cannot catch this on its own — holding disc 3 of
# Koudelka is, to the gate, indistinguishable from holding the whole game.
_DISC_RE = re.compile(r"\((?:Disc|Disk|CD)\s*([0-9IVX]+)\)|(?:^|[ _\-])CD\s*([0-9]+)\b", re.I)
_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8}


def _disc_no(name: str):
    m = _DISC_RE.search(name or "")
    if not m:
        return None
    v = (m.group(1) or m.group(2) or "").upper()
    return _ROMAN.get(v, int(v) if v.isdigit() else None)


def _version_key(name: str) -> str:
    """The dump name with its disc marker removed — identifies one RELEASE of a game."""
    return _DISC_RE.sub("", name or "").strip().lower()


def _missing_discs(hashes: list, owned_hashes: set) -> list:
    """Disc numbers we are missing for a release we already partly own ([] if complete).

    Grouped per release, so holding the USA discs is not 'completed' by a stray Japan disc.
    """
    by_ver: dict = {}
    singles: set = set()
    for hh in hashes or []:
        nm = hh.get("name") or ""
        md5 = (hh.get("md5") or "").lower()
        d = _disc_no(nm)
        if d is None:
            singles.add(md5)          # a release with no disc numbering — one file IS the game
            continue
        by_ver.setdefault(_version_key(nm), {})[d] = md5

    # A game is complete if ANY release is complete. This used to only look at partially-owned
    # multi-disc releases and report their gap, so owning a WHOLE alternative release still read
    # as missing: Aleste 2's "(ROM Conversion) (v8.0) (Ricbit)" is a single 2 MB .rom that RA
    # accepts on its own, but `if d is None: continue` discarded it and the partially-owned
    # 3-disk Woomb translation kept reporting "missing disc(s) 2, 3" (2026-08-16). The sourcer
    # then "SOURCED" the game repeatedly without ever changing the verdict.
    if singles & owned_hashes:
        return []
    for _ver, discs in by_ver.items():
        if len(discs) >= 2 and all(m5 in owned_hashes for m5 in discs.values()):
            return []

    for _ver, discs in by_ver.items():
        if len(discs) < 2:
            continue
        owned_d = {d for d, m5 in discs.items() if m5 in owned_hashes}
        if owned_d:
            gap = sorted(set(discs) - owned_d)
            if gap:
                return gap
    return []


async def _missing_ra(cid: int, owned, owned_hashes: set,
                      name_include: list | None = None, name_exclude: list | None = None,
                      inc_set: frozenset | set = frozenset(),
                      exc_set: frozenset | set = frozenset(),
                      sibling_hashes: set = frozenset()) -> dict:
    from app.core.elasticsearch import get_es, init_es, close_es
    from app.services.ingest import clean_ra_title
    await init_es()
    try:
        # Exclude sets RA has DELISTED (demoted/removed). Without this a delisted game we do
        # not own sits in the wanted list forever as unobtainable, because our ES copy
        # outlives RA's listing. must_not+exists keeps docs written before the flag existed.
        resp = await get_es().search(index=settings.es_index_games, size=10000,
                                     query={"bool": {
                                         "must": [{"term": {"console_id": cid}}],
                                         "must_not": [{"term": {"delisted": True}}]}},
                                     source=["game_id", "title", "num_achievements",
                                             "points", "hashes.md5", "hashes.name",
                                             "hashes.patch_url"])
    finally:
        await close_es()
    inc = tuple(e.lower() for e in (name_include or []))
    exc = tuple(e.lower() for e in (name_exclude or []))
    missing, catalog, owned_ct, subsets = [], 0, 0, 0
    for h in resp["hits"]["hits"]:
        s = h["_source"]
        title = s.get("title", "")
        # Subset/Bonus games auto-apply to the base ROM at runtime — no separate ROM to own.
        if _SUBSET.search(title):
            subsets += 1
            continue
        game_md5s = {hh.get("md5", "").lower() for hh in s.get("hashes", [])}
        owned_here = bool(game_md5s & owned_hashes)   # we own a ROM this game recognizes
        # Hash-based shared-console split (Satellaview/N64DD): a game belongs to this sub-system
        # if its hash is in the library set, or (for hacks not in any DAT) we own it here.
        if inc_set and not (game_md5s & inc_set) and not owned_here:
            continue
        if (exc_set or sibling_hashes) and not owned_here \
                and (game_md5s & exc_set or game_md5s & sibling_hashes):
            continue
        # legacy name-extension split (NGP vs NGPC on console 14)
        if inc or exc:
            names = [(hh.get("name") or "").lower() for hh in s.get("hashes", [])]
            if inc and not any(n.endswith(inc) for n in names):
                continue
            if exc and any(n.endswith(exc) for n in names):
                continue
        catalog += 1
        if owned_here:
            gap = _missing_discs(s.get("hashes", []), owned_hashes)
            if not gap:
                owned_ct += 1
                continue
            # owned but INCOMPLETE: report it rather than counting it as a win
            t, cat = clean_ra_title(title)
            missing.append({"id": s["game_id"], "title": t, "category": cat,
                            "achievements": s.get("num_achievements", 0),
                            "points": s.get("points", 0), "no_hash": False,
                            "has_patch": False, "incomplete_discs": gap,
                            "reason": "multi-disc set incomplete — missing disc(s) %s"
                                      % ", ".join(str(d) for d in gap),
                            "url": f"https://retroachievements.org/game/{s['game_id']}"})
            continue
        t, cat = clean_ra_title(title)
        has_patch = any(hh.get("patch_url") for hh in s.get("hashes", []))
        missing.append({"id": s["game_id"], "title": t, "category": cat,
                        "achievements": s.get("num_achievements", 0), "points": s.get("points", 0),
                        "no_hash": not game_md5s, "has_patch": has_patch,
                        "url": f"https://retroachievements.org/game/{s['game_id']}"})
    missing.sort(key=lambda x: (-x["achievements"], x["title"].lower()))
    return {"gate": "ra", "catalog": catalog, "owned": owned_ct,
            "subsets_excluded": subsets, "missing": missing}


def _missing_dat(cid: int, source: str, owned) -> dict:
    from app.services.dat_source import dat_path, parse_dat, md5_index, base_title, display_meta
    from app.services.dat import lla_index
    if source == "no-intro":
        p = dat_path("no-intro", cid)
        idx = md5_index(parse_dat(p)) if p else {}
    else:
        idx = lla_index(cid)
    groups = {}
    for name in idx.values():
        groups.setdefault(base_title(name), name)   # one representative name per game group
    owned_bt = set()
    for g in owned:
        for r in g.roms:
            n = (r.dat_matches or {}).get(source)
            if isinstance(n, str):
                owned_bt.add(base_title(n))
    missing = []
    for bt, name in groups.items():
        if bt in owned_bt:
            continue
        t, region, category = display_meta(name)
        missing.append({"id": None, "title": t, "category": category, "region": region, "url": None})
    missing.sort(key=lambda x: x["title"].lower())
    return {"gate": source, "catalog": len(groups), "owned": len(owned), "missing": missing}
