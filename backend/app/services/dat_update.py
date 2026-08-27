"""Run a pullable DAT source's update and record its timestamp/result."""
from __future__ import annotations
from app.db.models import System
from app.core import dat_sources


async def run_source(db, source: str) -> dict:
    if source == "ra":
        from app.services import ra_monitor
        result = await ra_monitor.poll(db)   # hourly: 1-call claims monitor (new releases)
    elif source == "lla":
        from app.services.lla_fetch import update_lla_dats
        r = update_lla_dats()
        result = {"downloaded": r["downloaded"], "removed": r["removed"], "errors": r["errors"]}
    else:
        raise ValueError(f"{source} is upload-only (no open API)")
    dat_sources.record_update(db, source, result)
    return result


async def _ra_full(db) -> dict:
    """Nightly reconcile: full RA refresh of ALL ingested consoles. Catches new hashes added to
    EXISTING games and confirms game counts — the things the hourly claims monitor can't see
    (claims only surface newly-published sets). Runs once/day in the overnight window, so the
    50+ GetGameList calls land off-peak. Each console's incremental sync is a single call unless
    something actually changed."""
    import asyncio
    from app.core.elasticsearch import get_es, init_es, close_es
    from app.services.ingest import update_ra_console
    from app.services.sync import enrich_missing_hash_names
    from app.core.console_meta import display_name
    from app.core.events import log_event
    consoles = sorted({s.ra_console_id for s in db.query(System).all()})
    await init_es()
    hn: dict = {}
    try:
        per = []
        for i, cid in enumerate(consoles):
            if i:
                await asyncio.sleep(1.0)   # no burst — space the GetGameList calls out
            r = await update_ra_console(get_es(), cid)
            per.append(r)
            dat_sources.mark_ra_checked(db, cid)
            if r.get("delisted"):
                srow = db.query(System).filter_by(ra_console_id=cid).first()
                nm = display_name(cid, srow.ra_name) if srow else f"console {cid}"
                log_event(db, "ra_delisted",
                          f"{r['delisted']} set(s) DELISTED by RA — {nm}",
                          system=(srow.folder if srow else None),
                          data={"count": r["delisted"], "console": cid})
                try:
                    from app.services import discord_notify
                    await discord_notify.notify_delisted(db, r["delisted"], nm)
                except Exception as e:  # noqa
                    log.warning("delist notify failed", error=str(e)[:120])
            if r.get("created"):
                srow = db.query(System).filter_by(ra_console_id=cid).first()
                nm = display_name(cid, srow.ra_name) if srow else f"console {cid}"
                log_event(db, "ra_added", f"{r['created']} new game(s) added to RA — {nm}",
                          system=(srow.folder if srow else None),
                          data={"count": r["created"], "console": cid})
        # Backfill any game left with nameless hashes. update_ra_console only enriches games it
        # saw CHANGE, so anything ingested by another path keeps name="" forever — and sourcing
        # then has only the RA display title to match filenames against, which silently loses
        # every game whose dump is titled differently (see enrich_missing_hash_names).
        try:
            hn = await enrich_missing_hash_names(get_es())
            if hn.get("enriched"):
                log_event(db, "ra_enriched",
                          f"Backfilled hash names for {hn['enriched']} game(s)",
                          data=hn)
        except Exception as e:  # noqa
            hn = {"error": str(e)[:200]}
    finally:
        await close_es()
    out = {"consoles": len(consoles),
           "created": sum(p["created"] for p in per),
           "updated": sum(p["updated"] for p in per),
           "enriched": sum(p["enriched"] for p in per),
           "hash_names": hn}
    # RA is authoritative for per-game playtime + last-played — refresh the recently-played
    # library games (bounded set) as part of the nightly reconcile cadence.
    try:
        from app.services.ra_playtime import sync_ra_playtime
        out["playtime"] = await sync_ra_playtime(db)
    except Exception as e:  # noqa
        out["playtime"] = {"error": str(e)[:200]}
    return out


async def run_due(db) -> dict:
    """Called by the hourly tick. Hourly: the RA claims monitor (1 call) + any other due source.
    Once/day overnight: the full all-console RA reconcile + a cart auto-source sweep."""
    ran = {}
    for src in dat_sources.due_sources(db):     # ra=hourly (monitor), lla=weekly
        try:
            ran[src] = await run_source(db, src)
        except Exception as e:  # noqa
            ran[src] = {"error": str(e)[:200]}
    # Nightly full reconcile + cart auto-source sweep (once/day, overnight window)
    if dat_sources.ra_full_due(db):
        try:
            ran["ra_full"] = await _ra_full(db)
            dat_sources.mark_ra_full(db)
            dat_sources.record_update(db, "ra", ran["ra_full"])
        except Exception as e:  # noqa
            ran["ra_full"] = {"error": str(e)[:200]}
        if dat_sources.get_auto_source(db):
            try:
                ran["auto_source"] = await _auto_source(db)
            except Exception as e:  # noqa
                ran["auto_source"] = {"error": str(e)[:200]}
    try:
        from app.core.events import refresh_coverage
        await refresh_coverage(db)     # log coverage-% deltas from this tick's changes
    except Exception as e:  # noqa
        pass
    return {"ran": ran}


async def _auto_source(db) -> dict:
    """Auto-source missing games on the tick — CARTRIDGE systems only (small, safe). Disc
    systems have a registered source too, but their multi-GB downloads should be deliberate
    (the manual 'Auto-source' button in the Missing panel), not fired every hour."""
    from app.services.sourcing import source_missing, _registry
    from app.core.systems import get_system_by_folder, gate_list
    carts = _registry().get("cart", {}).get("systems", {})
    out = {}
    for s in db.query(System).all():
        sysc = get_system_by_folder(s.folder)
        if not sysc or not carts.get(s.folder):        # cart-only: skip disc systems
            continue
        if gate_list(s.gate, sysc.gate)[0] != "ra":
            continue
        r = await source_missing(s.folder, allow_search=False)   # no broad search on the tick
        if r.get("wanted"):
            out[s.folder] = {k: r.get(k, 0) for k in ("wanted", "sourced", "not_found")}
    return out
