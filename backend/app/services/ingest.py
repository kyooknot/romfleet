"""Phase-1 ingest: scan a system's source ROMs, RA-hash each file, gate against
the RA hash index in Elasticsearch, and persist the result to Postgres.

RA is the gate: a file whose computed RA hash matches a known RA hash is a
library game; anything else is recorded 'unmatched' (for-info), never released.
"""
from __future__ import annotations
import asyncio
from pathlib import Path
from elasticsearch import AsyncElasticsearch
from sqlalchemy.orm import Session
from app.core.config import settings
from app.core.systems import get_system_by_folder, gate_list
from app.hashers import get_hasher
from app.services.ra_client import ra_client
import datetime as _dt
from app.services.sync import _upsert_game
from app.services.dat import lla_index
from app.services.dat_source import dat_path, parse_dat, md5_index, base_title, display_meta
from app.db.session import SessionLocal
from app.db.models import System, Game, RomFile, MediaAsset
import structlog

log = structlog.get_logger()

_SKIP_EXTS = {'.txt', '.nfo', '.jpg', '.png', '.gif', '.xml', '.dat',
              '.sav', '.srm', '.state', '.cht', '.m3u', '.db'}

# RA decorates category titles like "~Homebrew~ Foo". Move the tag to a suffix: "Foo (Homebrew)".
import re as _re
_RA_TAG = _re.compile(r'^~([^~]+)~\s*(.+)$')


def clean_ra_title(t: str) -> tuple[str, str | None]:
    """Split '~Homebrew~ Foo' (or already-cleaned 'Foo (Homebrew)') -> ('Foo', 'Homebrew')."""
    t = t or ""
    m = _RA_TAG.match(t)
    if m:
        return m.group(2).strip(), m.group(1).strip()
    m2 = _RA_SUFFIX_TAG.match(t)
    if m2:
        return m2.group(1).strip(), m2.group(2).strip()
    return t, None


_RA_SUFFIX_TAG = _re.compile(r'^(.*)\s*\((Homebrew|Hack|Unlicensed|Prototype|Demo|Test Kit|Subset|Bonus|Multi)\)\s*$', _re.I)


async def sync_console_to_es(es: AsyncElasticsearch, console_id: int) -> dict:
    """Pull one RA console's games+hashes into the ra_games index (the gate source).
    One GetGameList call; `changed_ids` are the games whose hash set changed (created or
    updated) — the only ones that need the per-game enrich call."""
    games = await ra_client.get_game_list(console_id)
    created = updated = 0
    changed_ids: list[int] = []
    for g in games:
        gid = g.get("ID") or g.get("GameID")
        r = await _upsert_game(es, g, console_id)
        if r == "created":
            created += 1
            if gid:
                changed_ids.append(gid)
        elif r == "updated_hashes":
            updated += 1
            if gid:
                changed_ids.append(gid)
    # ---- DELISTING DETECTION ----------------------------------------------------------
    # Everything above is upsert-only, so a set RA REMOVES (demoted to Unofficial, or
    # deleted) lingers in the gate forever. Nothing anywhere scanned for that: the hourly
    # claims poll only ever adds, and this reconcile only ever upserts.
    # Consequences, both silent: a game we own stays "matched" against a set that no longer
    # exists, and a game we DON'T own haunts the wanted list forever as unobtainable,
    # because our copy outlives RA's listing.
    # Observed 2026-08-09: RA 17308 "Sex" (3DO) was sourced and demoted hours later --
    # NumAchievements dropped to 0 and it left GetGameList (41 entries, no 17308).
    # Mark rather than delete: a demotion is often reversed, and the doc carries the hashes
    # our owned ROM is matched against. `delisted` is what downstream should filter on.
    delisted = 0
    try:
        live = {g.get("ID") or g.get("GameID") for g in games}
        live.discard(None)
        resp = await es.search(index=settings.es_index_games, size=10000,
                               query={"term": {"console_id": console_id}},
                               source=["game_id", "title", "delisted"])
        for hit in resp["hits"]["hits"]:
            src = hit.get("_source") or {}
            gid = src.get("game_id")
            if gid in live or not gid:
                continue
            if not src.get("delisted"):
                await es.update(index=settings.es_index_games, id=str(gid),
                                doc={"delisted": True, "delisted_at": _dt.datetime.now(
                                    _dt.timezone.utc).isoformat()})
                delisted += 1
                log.warning("RA set delisted — no longer in GetGameList",
                            game_id=gid, title=src.get("title"), console_id=console_id)
        # …and un-mark anything RA brought back, so a reversed demotion self-heals.
        if live:
            back = await es.search(index=settings.es_index_games, size=10000,
                                   query={"bool": {"must": [{"term": {"console_id": console_id}},
                                                            {"term": {"delisted": True}}]}},
                                   source=["game_id", "title"])
            for hit in back["hits"]["hits"]:
                gid = (hit.get("_source") or {}).get("game_id")
                if gid in live:
                    await es.update(index=settings.es_index_games, id=str(gid),
                                    doc={"delisted": False, "delisted_at": None})
                    log.info("RA set relisted", game_id=gid, console_id=console_id)
    except Exception as e:  # noqa — never let this break the reconcile
        log.warning("delisting check failed", console_id=console_id, error=str(e)[:150])
    return {"console_id": console_id, "games": len(games), "created": created,
            "updated": updated, "changed_ids": changed_ids, "delisted": delisted}


async def update_ra_console(es: AsyncElasticsearch, console_id: int) -> dict:
    """Incremental RA refresh: sync (1 API call) then enrich ONLY the changed games — so a
    no-change run costs a single request and never overwhelms the RA API rate limit."""
    from app.services.sync import enrich_game_hashes
    s = await sync_console_to_es(es, console_id)
    enriched = errors = 0
    for gid in s["changed_ids"]:
        try:
            await enrich_game_hashes(es, gid)
            enriched += 1
        except Exception as e:  # noqa
            errors += 1
            log.warning("ra enrich failed", game_id=gid, error=str(e)[:120])
        await asyncio.sleep(0.12)   # RA API courtesy
    return {"console_id": console_id, "games": s["games"], "created": s["created"],
            "updated": s["updated"], "enriched": enriched, "errors": errors,
            "delisted": s.get("delisted", 0)}


async def enrich_console(es: AsyncElasticsearch, console_id: int, limit: int | None = None) -> dict:
    """Fill in hash names/labels/priority for a console's games via API_GetGameHashes.
    The bulk sync only stores bare MD5s; this adds the region/revision names the
    priority scorer needs. One-time-ish; RA-rate-limited."""
    from app.services.sync import enrich_game_hashes
    resp = await es.search(index=settings.es_index_games, size=10000,
                           query={"term": {"console_id": console_id}}, source=["game_id"])
    ids = [h["_source"]["game_id"] for h in resp["hits"]["hits"]]
    if limit:
        ids = ids[:limit]
    enriched = errors = 0
    for gid in ids:
        try:
            await enrich_game_hashes(es, gid)
            enriched += 1
        except Exception as e:
            errors += 1
            log.warning("enrich failed", game_id=gid, error=str(e)[:120])
        await asyncio.sleep(0.12)   # RA API courtesy
    return {"console_id": console_id, "games": len(ids), "enriched": enriched, "errors": errors}


async def gate_lookup(es: AsyncElasticsearch, md5: str) -> dict | None:
    """Return {ra_game_id,title,hash_name} if md5 matches any RA hash, else None."""
    resp = await es.search(
        index=settings.es_index_games, size=1,
        query={"nested": {
            "path": "hashes",
            "query": {"term": {"hashes.md5": md5.lower()}},
            "inner_hits": {"size": 1, "_source": ["hashes.md5", "hashes.name"]},
        }},
    )
    hits = resp["hits"]["hits"]
    if not hits:
        return None
    src = hits[0]["_source"]
    inner = hits[0].get("inner_hits", {}).get("hashes", {}).get("hits", {}).get("hits", [])
    hash_name = inner[0]["_source"].get("name") if inner else None
    return {"ra_game_id": src["game_id"], "title": src.get("title", ""), "hash_name": hash_name,
            "num_achievements": src.get("num_achievements", 0), "points": src.get("points", 0)}


def _iter_rom_files(root: Path, exts: list[str]):
    ext_set = {e.lower() for e in exts}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if "_quarantine" in p.parts or "multidisc" in p.parts or p.name.startswith(".romfleet-"):
            continue  # organize's quarantine bin + rollback manifests, and the pre-compression
            # multidisc/ staging folder (uncompressed .iso originals already kept as .rvz)
        suf = p.suffix.lower()
        if suf in _SKIP_EXTS:
            continue
        if suf in ext_set or suf == ".zip":   # RawHasher transparently unzips
            yield p


def _get_or_create_game(db: Session, hit: dict, console_id: int) -> Game:
    from app.core.categories import normalize_category
    clean, category = clean_ra_title(hit["title"])
    category = normalize_category(category)
    g = db.query(Game).filter_by(ra_game_id=hit["ra_game_id"]).one_or_none()
    if not g:
        g = Game(ra_game_id=hit["ra_game_id"], title=clean, category=category, console_id=console_id,
                 num_achievements=hit.get("num_achievements", 0), points=hit.get("points", 0),
                 metadata_source="ra")
        db.add(g)
        db.flush()
    else:
        locked = g.locked_fields or []
        if "title" not in locked:
            g.title = clean       # backfill clean title (no category suffix) on re-ingest
        if "category" not in locked and category:
            g.category = category
    return g


def _get_or_create_dat_game(db: Session, name: str, console_id: int, source: str,
                            selection: str) -> Game:
    """Anchor a game on a DAT entry (No-Intro/Redump/MAME/LLA). 1G1R groups a game's
    versions under one entry (key = normalized base title); full-set keeps every version
    as its own entry (key = the exact version name)."""
    from app.core.categories import normalize_category
    title, region, category = display_meta(name)
    category = normalize_category(category)
    key = base_title(name) if selection == "1g1r" else name
    g = (db.query(Game)
           .filter_by(console_id=console_id, gate_source=source, group_key=key)
           .one_or_none())
    if not g:
        g = Game(console_id=console_id, gate_source=source, group_key=key,
                 title=title, region=region, category=category, metadata_source=source)
        db.add(g)
        db.flush()
    else:
        locked = g.locked_fields or []
        if "title" not in locked:
            g.title = title
        if "region" not in locked and region:
            g.region = region
        if "category" not in locked and category:
            g.category = category
    return g


def _upsert_rom(db: Session, p: Path, folder: str, method: str, md5: str | None,
                status: str, hash_name: str | None, game_id: int | None,
                dat_matches: dict, error: str | None, provenance: dict | None = None):
    r = db.query(RomFile).filter_by(path=str(p)).one_or_none()
    if not r:
        r = RomFile(path=str(p))
        db.add(r)
    r.filename = p.name
    r.system_folder = folder
    r.size_bytes = p.stat().st_size
    r.hash_method = method
    r.computed_hash = md5
    r.match_status = status
    r.ra_hash_name = hash_name
    r.dat_matches = dat_matches
    r.error = error
    r.game_id = game_id
    # Only WRITE provenance when the caller knows it. ingest's disk rescan calls this for every
    # file with provenance=None; clobbering there would erase the acquisition record on the next
    # scan — the record would exist only until the nightly ingest ran.
    if provenance:
        r.provenance = provenance
        r.source_url = provenance.get("url") or r.source_url


def prune_orphan_games(db: Session) -> int:
    """Delete games left with NO ROMs and NO media (cruft from gate switches). Orphans that
    still hold scraped media are kept, so switching a gate back restores that work."""
    live = {r[0] for r in db.query(RomFile.game_id).filter(RomFile.game_id.isnot(None)).distinct()}
    orphan_ids = [gid for (gid,) in db.query(Game.id).filter(Game.id.notin_(live)).all()] if live else []
    if not orphan_ids:
        return 0
    with_media = {m[0] for m in db.query(MediaAsset.game_id).filter(
        MediaAsset.game_id.in_(orphan_ids)).distinct()}
    to_del = [gid for gid in orphan_ids if gid not in with_media]
    for gid in to_del:
        db.query(Game).filter_by(id=gid).delete()
    db.commit()
    return len(to_del)


async def rederive_system(es: AsyncElasticsearch, folder: str) -> dict:
    """Re-evaluate membership + re-anchor games for a system from ALREADY-stored hashes —
    no file I/O, no re-hashing. This is what a gate/selection change triggers. Non-destructive:
    games orphaned by the switch keep their scraped media (switching back restores them)."""
    sysc = get_system_by_folder(folder)
    if not sysc:
        raise ValueError(f"Unknown system folder: {folder}")
    db = SessionLocal()
    try:
        srow = db.query(System).filter_by(folder=folder).one_or_none()
        gate = gate_list(srow.gate if srow else None, sysc.gate)
        selection = (srow.selection if srow else None) or "1g1r"

        lla_idx = lla_index(sysc.ra_console_id)
        ni_path = dat_path("no-intro", sysc.ra_console_id)
        ni_idx = md5_index(parse_dat(ni_path)) if ni_path else {}

        roms = db.query(RomFile).filter_by(system_folder=folder).all()
        matched = unmatched = skipped = 0
        for r in roms:
            md5 = (r.computed_hash or "").lower()
            if not md5:
                skipped += 1
                continue
            hit = await gate_lookup(es, md5)
            dat_matches: dict = {}
            if hit:
                dat_matches["ra"] = hit["hash_name"] or True
            ni_name = ni_idx.get(md5)
            if ni_name:
                dat_matches["no-intro"] = ni_name
            lla_name = lla_idx.get(md5)
            if lla_name:
                dat_matches["lla"] = lla_name
            member = any(dat_matches.get(gp) for gp in gate)

            game = None
            hash_name = None
            if "ra" in gate and hit:
                game = _get_or_create_game(db, hit, sysc.ra_console_id)
                hash_name = hit["hash_name"]
            elif "no-intro" in gate and ni_name:
                game = _get_or_create_dat_game(db, ni_name, sysc.ra_console_id, "no-intro", selection)
                hash_name = ni_name
            elif "lla" in gate and lla_name:
                game = _get_or_create_dat_game(db, lla_name, sysc.ra_console_id, "lla", selection)
                hash_name = lla_name

            r.dat_matches = dat_matches
            r.ra_hash_name = hash_name
            r.game_id = game.id if game else None
            if game is not None or member:
                r.match_status = "matched"
                matched += 1
            else:
                r.match_status = "unmatched"
                unmatched += 1
        db.commit()
        pruned = prune_orphan_games(db)
        return {"system": folder, "gate": gate, "selection": selection,
                "roms": len(roms), "matched": matched, "unmatched": unmatched,
                "skipped": skipped, "pruned": pruned}
    finally:
        db.close()


async def ingest_system(es: AsyncElasticsearch, folder: str, limit: int | None = None) -> dict:
    sysc = get_system_by_folder(folder)
    if not sysc:
        raise ValueError(f"Unknown system folder: {folder}")
    root = Path(settings.src_roms_path) / folder
    if not root.is_dir():
        raise ValueError(f"Source dir not found: {root}")
    hasher = get_hasher(sysc.hash_method)

    db = SessionLocal()
    try:
        srow = db.query(System).filter_by(folder=folder).one_or_none()
        if not srow:
            srow = System(folder=folder, ra_console_id=sysc.ra_console_id,
                          ra_name=sysc.ra_name, hash_method=sysc.hash_method)
            db.add(srow)
            db.commit()

        # effective gate/selection: DB per-system override wins over systems.yaml default
        gate = gate_list(srow.gate, sysc.gate)
        selection = srow.selection or "1g1r"

        # identity indexes (naming for the match column; membership when in `gate`)
        lla_idx = lla_index(sysc.ra_console_id)
        ni_path = dat_path("no-intro", sysc.ra_console_id)
        ni_idx = md5_index(parse_dat(ni_path)) if ni_path else {}

        files = list(_iter_rom_files(root, sysc.extensions))
        if limit:
            files = files[:limit]
        # resume: skip files already hashed at the same size — makes an interrupted disc
        # run continue where it left off (and avoids re-decompressing multi-GB images).
        prior = {r.path: r.size_bytes for r in db.query(RomFile).filter(
            RomFile.system_folder == folder, RomFile.match_status != "error",
            RomFile.computed_hash.isnot(None)).all()}
        matched = unmatched = errors = skipped = 0
        samples: list[str] = []
        total = len(files)
        for idx, p in enumerate(files, 1):
            try:
                if prior.get(str(p)) == p.stat().st_size:
                    skipped += 1
                    continue
            except OSError:
                pass
            try:
                md5 = await hasher.hash_file(str(p))
                hit = await gate_lookup(es, md5)
            except Exception as e:
                _upsert_rom(db, p, folder, sysc.hash_method, None, "error", None, None, {}, str(e)[:300])
                errors += 1
                db.commit()
                continue
            # identify against every available DB (enrichment, independent of the gate)
            dat_matches: dict = {}
            if hit:
                dat_matches["ra"] = hit["hash_name"] or True
            ni_name = ni_idx.get((md5 or "").lower())
            if ni_name:
                dat_matches["no-intro"] = ni_name
            lla_name = lla_idx.get(md5)
            if lla_name:
                dat_matches["lla"] = lla_name
            # membership per the configured gate (any listed provider matched)
            member = any(dat_matches.get(gp) for gp in gate)

            # anchor the library game on the gate provider that matched (gate order = priority)
            game = None
            hash_name = None
            if "ra" in gate and hit:
                game = _get_or_create_game(db, hit, sysc.ra_console_id)
                hash_name = hit["hash_name"]
            elif "no-intro" in gate and ni_name:
                game = _get_or_create_dat_game(db, ni_name, sysc.ra_console_id, "no-intro", selection)
                hash_name = ni_name
            elif "lla" in gate and lla_name:
                game = _get_or_create_dat_game(db, lla_name, sysc.ra_console_id, "lla", selection)
                hash_name = lla_name

            if game is not None:
                _upsert_rom(db, p, folder, sysc.hash_method, md5, "matched",
                            hash_name, game.id, dat_matches, None)
                matched += 1
                if len(samples) < 8:
                    extra = "".join(f" [+{k}]" for k in dat_matches if k not in (gate[0], "ra"))
                    samples.append(f"MATCH  {p.name}  ->  {game.title}{extra}")
            elif member:  # matched a gate provider we don't anchor games on (e.g. lla-only)
                _upsert_rom(db, p, folder, sysc.hash_method, md5, "matched",
                            hash_name, None, dat_matches, None)
                matched += 1
            else:
                _upsert_rom(db, p, folder, sysc.hash_method, md5, "unmatched",
                            None, None, dat_matches, None)
                unmatched += 1
                if len(samples) < 8:
                    samples.append(f"MISS   {p.name}")
            # commit + report progress periodically so long disc runs are crash-safe + observable
            if idx % 10 == 0 or idx == total:
                db.commit()
                print(f"  [{idx}/{total}] matched={matched} unmatched={unmatched} "
                      f"err={errors} skip={skipped}", flush=True)
        db.commit()
        if matched:
            from app.core.events import log_event
            log_event(db, "rf_added", f"Ingested {folder}: {matched} games", system=folder,
                      data={"matched": matched, "unmatched": unmatched})
        return {"system": folder, "scanned": len(files), "matched": matched,
                "unmatched": unmatched, "errors": errors, "skipped": skipped,
                "gate": gate, "selection": selection, "samples": samples}
    finally:
        db.close()
