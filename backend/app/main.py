"""RomFleet API + web UI (Phase 1).

  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations
from pathlib import Path
import shutil
from fastapi import FastAPI, Query, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from app.core.config import settings
from app.db.session import SessionLocal
from app.db.models import System, Game, RomFile, MediaAsset
from app.services import autopush

# completeness is configurable per game category (settings key 'completeness').
_COMPLETENESS = None


def _completeness_cfg() -> dict:
    global _COMPLETENESS
    if _COMPLETENESS is None:
        from app.core.completeness import get_completeness
        with SessionLocal() as db:
            _COMPLETENESS = get_completeness(db)
    return _COMPLETENESS


def _coverage(g: Game | None) -> dict | None:
    if g is None:
        return None
    from app.core.completeness import profile_for
    prof = profile_for(_completeness_cfg(), g.category)
    req_media = prof.get("media", [])
    req_meta = prof.get("meta", [])
    present = {m.media_type for m in g.media}
    missing_media = [t for t in req_media if t not in present]
    meta_missing = [f for f in req_meta if not getattr(g, f, None)]
    return {"media_total": len(g.media), "core_have": len(req_media) - len(missing_media),
            "core_total": len(req_media), "missing_media": missing_media,
            "metadata_missing": meta_missing, "complete": not missing_media and not meta_missing}

import structlog

# Module-level logger. main.py had NONE — the only `log` names were locals inside two nested
# functions, so every other `log.` call in this file was a NameError waiting for its branch to
# execute. The dat-tick pile-up guard hit it immediately; worse, the platform-collision guard
# would have raised it at exactly the moment a collision was detected, turning the check that
# exists to prevent silent data loss into a 500.
log = structlog.get_logger()


app = FastAPI(title="RomFleet", version="0.2.0")
WEB_DIR = Path(__file__).parent / "web"
MEDIA_STORE = Path(settings.canonical_path) / "media"
MEDIA_STORE.mkdir(parents=True, exist_ok=True)

MEDIA_TYPES = ["boxfront", "boxback", "titlescreen", "screenshot", "logo", "background",
               "marquee", "video", "cart", "disc", "manual"]


def _media_dict(m: MediaAsset) -> dict:
    return {"id": m.id, "media_type": m.media_type, "source": m.source,
            "is_primary": m.is_primary, "is_locked": m.is_locked,
            "url": (f"/media/{m.path}" if m.path else m.url)}


def _game_dict(g: Game, full: bool = False) -> dict:
    d = {"id": g.id, "ra_game_id": g.ra_game_id, "is_manual": g.is_manual,
         "console_id": g.console_id, "title": g.title, "num_achievements": g.num_achievements,
         "points": g.points, "metadata_source": g.metadata_source, "is_favorite": g.is_favorite,
         "star_rating": g.star_rating, "community_rating": g.community_rating,
         "hidden_on": g.hidden_on or [],
         # play state (bidirectionally synced with every surface; RomFleet DB is source of truth)
         "play_count": g.play_count or 0,
         "last_played": g.last_played.isoformat() if g.last_played else None,
         "playtime_minutes": g.playtime_minutes}
    if full:
        from app.core.console_meta import display_name
        from app.core.systems import get_system_by_folder
        folder = g.roms[0].system_folder if g.roms else None
        sysc = get_system_by_folder(folder) if folder else None
        d["system_folder"] = folder
        d["system_name"] = display_name(g.console_id, sysc.ra_name) if sysc else (folder or None)
        for f in Game.EDITABLE:
            d[f] = getattr(g, f)
        d["locked_fields"] = g.locked_fields or []
        d["play_sync"] = g.play_sync or {}   # per-surface last-observed play_count (delta accum)
        d["source_ids"] = g.source_ids or {}
        d["source_names"] = g.source_names or {}
        d["media"] = [_media_dict(m) for m in sorted(g.media, key=lambda m: (m.media_type, not m.is_primary))]
        d["coverage"] = _coverage(g)
        d["roms"] = [{"id": r.id, "filename": r.filename, "status": r.match_status,
                      "ra_hash_name": r.ra_hash_name, "hash": r.computed_hash, "size": r.size_bytes,
                      "dat_matches": r.dat_matches or {}}
                     for r in g.roms]
    return d


# ---------- stats / systems / roms ----------
@app.get("/api/stats")
def stats():
    with SessionLocal() as db:
        base = {k: db.scalar(q) for k, q in {
            "systems": select(func.count()).select_from(System),
            "roms": select(func.count()).select_from(RomFile),
            "matched": select(func.count()).where(RomFile.match_status == "matched"),
            "unmatched": select(func.count()).where(RomFile.match_status == "unmatched"),
        }.items()}
        # count only LIVE games (linked to a ROM) — orphans from gate switches don't count
        live = {r[0] for r in db.query(RomFile.game_id).filter(RomFile.game_id.isnot(None)).distinct()}
        games = db.scalars(select(Game).where(Game.id.in_(live)).options(
            selectinload(Game.media))).all() if live else []
        base["games"] = len(games)
        base["incomplete"] = sum(1 for g in games if not (_coverage(g) or {}).get("complete"))
        # convenience percentages for dashboard tiles (Homepage customapi etc.)
        base["match_pct"] = round(100 * base["matched"] / base["roms"], 1) if base["roms"] else 0
        base["complete_pct"] = round(100 * (base["games"] - base["incomplete"]) / base["games"], 1) if base["games"] else 0
        return base


@app.get("/api/home")
async def home(limit: int = 40):
    """Home feed: live per-system + overall RA coverage, plus the recent activity timeline."""
    from app.core.events import refresh_coverage
    from app.core.console_meta import display_name, ensure_icon, ensure_icon_for
    from app.db.models import Event
    with SessionLocal() as db:
        cov = await refresh_coverage(db)
        systems = []
        for s in db.scalars(select(System)).all():
            c = cov.get(s.folder)
            if c:
                systems.append({"folder": s.folder, "name": display_name(s.ra_console_id, s.ra_name),
                                "icon": ensure_icon_for(s.folder, s.ra_console_id), **c})
        systems.sort(key=lambda x: -x["pct"])
        evs = db.query(Event).order_by(Event.created_at.desc()).limit(limit).all()
        # resolve per-game box art (RA icon) for events that name a specific game
        from app.core.elasticsearch import get_es
        from app.core.config import settings as _st
        gids = list({(e.data or {}).get("game_id") for e in evs if (e.data or {}).get("game_id")})
        img_by_gid: dict = {}
        if gids:
            try:
                resp = await get_es().mget(index=_st.es_index_games,
                                           ids=[str(g) for g in gids], source=["image_icon"])
                for doc in resp["docs"]:
                    icon = doc.get("_source", {}).get("image_icon") if doc.get("found") else None
                    if icon:
                        img_by_gid[int(doc["_id"])] = f"{_st.ra_media_base}{icon}"
            except Exception:  # noqa
                pass
        return {"overall": cov.get("_overall", {}), "systems": systems,
                "events": [{"kind": e.kind, "title": e.title, "detail": e.detail,
                            "system": e.system_folder, "at": e.created_at.isoformat(),
                            "image": img_by_gid.get((e.data or {}).get("game_id"))} for e in evs]}


@app.get("/api/metrics")
def metrics():
    """Per-system coverage/media metrics — surfaces where the gaps are: unmatched roms,
    incomplete games, and per-media-type coverage (box art, snaps, video, …) by system."""
    from app.core.console_meta import display_name, ensure_icon, ensure_icon_for
    from sqlalchemy.orm import selectinload
    TYPES = ["boxfront", "titlescreen", "screenshot", "logo", "background", "video"]
    with SessionLocal() as db:
        romrows = db.execute(select(
            RomFile.system_folder,
            func.count().label("roms"),
            func.count().filter(RomFile.match_status == "unmatched").label("unmatched"),
        ).group_by(RomFile.system_folder)).all()
        romstats = {r.system_folder: r for r in romrows}
        out, tot = [], {"games": 0, "incomplete": 0, "unmatched": 0, "missing_meta": 0,
                        "media": {t: 0 for t in TYPES}}
        for s in db.scalars(select(System)).all():
            gids = [r[0] for r in db.query(RomFile.game_id).filter(
                RomFile.system_folder == s.folder, RomFile.game_id.isnot(None)).distinct()]
            games = db.scalars(select(Game).where(Game.id.in_(gids)).options(
                selectinload(Game.media))).all() if gids else []
            complete = incomplete = miss_meta = 0
            media = {t: 0 for t in TYPES}
            for g in games:
                cov = _coverage(g)
                if cov and cov["complete"]:
                    complete += 1
                else:
                    incomplete += 1
                if cov and cov["metadata_missing"]:
                    miss_meta += 1
                present = {m.media_type for m in g.media}
                for t in TYPES:
                    if t in present:
                        media[t] += 1
            rs = romstats.get(s.folder)
            row = {"folder": s.folder, "name": display_name(s.ra_console_id, s.ra_name),
                   "icon": ensure_icon_for(s.folder, s.ra_console_id), "games": len(games),
                   "roms": rs.roms if rs else 0, "unmatched": rs.unmatched if rs else 0,
                   "complete": complete, "incomplete": incomplete, "missing_meta": miss_meta,
                   "media": media}
            out.append(row)
            tot["games"] += len(games); tot["incomplete"] += incomplete
            tot["unmatched"] += row["unmatched"]; tot["missing_meta"] += miss_meta
            for t in TYPES:
                tot["media"][t] += media[t]
        out.sort(key=lambda x: (-x["incomplete"], -x["unmatched"], x["name"]))
        return {"media_types": TYPES, "systems": out, "totals": tot}


@app.get("/api/systems")
def systems():
    with SessionLocal() as db:
        rows = db.execute(select(
            RomFile.system_folder, func.count().label("roms"),
            func.count().filter(RomFile.match_status == "matched").label("matched"),
            func.count().filter(RomFile.match_status == "unmatched").label("unmatched"),
            func.count().filter(RomFile.match_status == "error").label("errors"),
        ).group_by(RomFile.system_folder).order_by(RomFile.system_folder)).all()
        meta = {s.folder: s for s in db.scalars(select(System)).all()}
        from app.core.systems import get_system_by_folder, gate_list
        from app.core.console_meta import display_name, ensure_icon, ensure_icon_for
        out = []
        for r in rows:
            m = meta.get(r.system_folder)
            sysc = get_system_by_folder(r.system_folder)
            cid = m.ra_console_id if m else (sysc.ra_console_id if sysc else 0)
            ra_name = m.ra_name if m else ((sysc.ra_name if sysc else None) or r.system_folder)
            out.append({"folder": r.system_folder,
                        "ra_name": ra_name, "name": display_name(cid, ra_name),
                        "icon": ensure_icon_for(r.system_folder, cid),
                        "roms": r.roms, "matched": r.matched, "unmatched": r.unmatched, "errors": r.errors,
                        "gates": gate_list(m.gate if m else None, sysc.gate if sysc else "ra"),
                        "selection": (m.selection if m else None) or "1g1r"})
        out.sort(key=lambda s: s["name"].lower())   # sidebar A→Z by display name
        return out


def _available_gates(console_id: int) -> list[dict]:
    """Which membership sources are usable for a console: RA always; a DAT source if its
    DAT is present; LLA if the console has LLA DATs."""
    from app.services.dat_source import dat_path
    from app.services.dat import lla_index
    out = [{"id": "ra", "label": "RetroAchievements", "available": True}]
    for src, label in (("no-intro", "No-Intro"), ("redump", "Redump"), ("mame", "MAME")):
        out.append({"id": src, "label": label, "available": bool(dat_path(src, console_id))})
    out.append({"id": "lla", "label": "Lost Level Archive", "available": bool(lla_index(console_id))})
    return out


def _system_config(db, folder: str) -> dict:
    from app.core.systems import get_system_by_folder, gate_list
    sysc = get_system_by_folder(folder)
    if not sysc:
        raise HTTPException(404, "unknown system")
    row = db.query(System).filter_by(folder=folder).one_or_none()
    return {"folder": folder, "console_id": sysc.ra_console_id,
            "gates": gate_list(row.gate if row else None, sysc.gate),
            "selection": (row.selection if row else None) or "1g1r",
            "compress": (row.compress if row else None) or "none",
            "default_gate": gate_list(None, sysc.gate),
            "available_gates": _available_gates(sysc.ra_console_id)}


@app.get("/api/systems/{folder}/config")
def get_system_config(folder: str):
    with SessionLocal() as db:
        return _system_config(db, folder)


class SystemConfigPatch(BaseModel):
    gate: str | list[str] | None = None   # a single provider or a union of providers
    selection: str | None = None
    compress: str | None = None           # none | rvz


@app.put("/api/systems/{folder}/config")
def put_system_config(folder: str, patch: SystemConfigPatch):
    from app.core.systems import get_system_by_folder, gate_list
    sysc = get_system_by_folder(folder)
    if not sysc:
        raise HTTPException(404, "unknown system")
    with SessionLocal() as db:
        row = db.query(System).filter_by(folder=folder).one_or_none()
        if not row:
            row = System(folder=folder, ra_console_id=sysc.ra_console_id,
                         ra_name=sysc.ra_name, hash_method=sysc.hash_method)
            db.add(row)
        if patch.gate is not None:
            gates = gate_list(patch.gate, sysc.gate)
            avail = {g["id"] for g in _available_gates(sysc.ra_console_id) if g["available"]}
            bad = [g for g in gates if g not in avail]
            if bad:
                raise HTTPException(400, f"gate(s) not available for this system: {bad}")
            row.gate = ",".join(gates)   # stored as a comma-joined union
        if patch.selection is not None:
            if patch.selection not in ("1g1r", "fullset"):
                raise HTTPException(400, "selection must be 1g1r or fullset")
            row.selection = patch.selection
        if patch.compress is not None:
            if patch.compress not in ("none", "rvz"):
                raise HTTPException(400, "compress must be none or rvz")
            row.compress = patch.compress
        db.commit()
        return _system_config(db, folder)


@app.post("/api/systems/{folder}/reingest")
async def reingest_system(folder: str):
    """Re-derive the library for a system against its currently-configured gate/selection.
    Non-destructive: re-links ROMs and creates games; games orphaned by a gate switch keep
    their scraped metadata/media (switching back restores them)."""
    from app.core.elasticsearch import get_es, init_es, close_es
    from app.services.ingest import rederive_system
    await init_es()
    try:
        return await rederive_system(get_es(), folder)
    finally:
        await close_es()


# ---------- DAT / membership sources ----------
@app.get("/api/settings/dat-sources")
def get_dat_sources():
    from app.core import dat_sources
    with SessionLocal() as db:
        return dat_sources.view(db)


@app.put("/api/settings/dat-sources")
def put_dat_sources(values: dict):
    """Save per-source auto-update cadence (manual|daily|weekly|monthly)."""
    from app.core import dat_sources
    with SessionLocal() as db:
        dat_sources.save_config(db, values)
        return dat_sources.view(db)


@app.post("/api/dats/{source}/update")
async def dats_update(source: str):
    """Pull a source now (ra: incremental, only changed games; lla: refresh from GitHub)."""
    from app.core import dat_sources
    if source not in dat_sources.PULLABLE:
        raise HTTPException(400, f"{source} is upload-only — use /upload")
    from app.services.dat_update import run_source
    with SessionLocal() as db:
        return await run_source(db, source)


# ONE long-lived background event loop for inline-heavy jobs. The first version used
# asyncio.run() per tick, which creates AND CLOSES a loop each time — and app.services.ra_client
# is a module-level singleton holding an httpx.AsyncClient, which binds to the loop that first
# uses it. So the hour after a tick ran, the next one died with "Event loop is closed"
# (observed 2026-08-04 00:00). A single loop that never closes keeps that singleton valid while
# still keeping the work off uvicorn's loop.
_BG_LOOP = None
_BG_LOCK = __import__("threading").Lock()


def _bg_loop():
    global _BG_LOOP
    import asyncio as _a
    import threading as _t
    with _BG_LOCK:
        if _BG_LOOP is None or _BG_LOOP.is_closed():
            loop = _a.new_event_loop()
            _t.Thread(target=loop.run_forever, daemon=True, name="romfleet-bg").start()
            _BG_LOOP = loop
    return _BG_LOOP


# Set while a tick is in flight. The hourly timer will otherwise stack runs on top of each
# other: a tick that auto-sources a PS2 game can run for hours, and the next hour's tick would
# start a second attempt at the same game.
_TICK_RUNNING = {"at": None}


@app.post("/api/dats/tick")
async def dats_tick(wait: bool = False):
    """Run every pullable source whose cadence has elapsed. Driven by the hourly systemd timer.

    Runs in a thread with its OWN event loop. It used to be awaited inline, and its auto-source
    step shells out synchronously (multi-GB downloads), which blocked the main event loop and
    took the whole API down for the duration. That turned into an hourly loop once the liveness
    watchdog existed: tick blocks API -> watchdog sees it dead and restarts -> the tick's curl
    dies with "Empty reply from server" -> the attempt is never recorded -> the same game is
    re-detected next hour and posts another "Sourcing..." to Discord. Madagascar: Escape 2
    Africa did exactly that five hours running on 2026-08-02.

    Pass wait=true to run it inline and get the stats back (useful by hand; never from the timer).
    """
    from app.services.dat_update import run_due
    if wait:
        with SessionLocal() as db:
            return await run_due(db)
    if _TICK_RUNNING["at"]:
        log.warning("dat tick still running, skipping this one", started=_TICK_RUNNING["at"])
        return {"ok": True, "status": "previous tick still running",
                "started": _TICK_RUNNING["at"]}
    import asyncio as _aio
    from datetime import datetime, timezone
    _TICK_RUNNING["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    async def _go():
        with SessionLocal() as db:
            return await run_due(db)

    def _done(fut):
        try:
            log.info("dat tick finished", result=fut.result())
        except Exception as e:  # noqa
            log.warning("dat tick failed", err=str(e)[:300])
        finally:
            _TICK_RUNNING["at"] = None

    _aio.run_coroutine_threadsafe(_go(), _bg_loop()).add_done_callback(_done)
    return {"ok": True, "status": "running"}


@app.post("/api/settings/discord/test")
async def discord_test():
    """Post a test embed to the configured Discord webhook so the user can confirm it works."""
    from app.services import discord_notify
    with SessionLocal() as db:
        ok = await discord_notify.send_test(db)
    if not ok:
        raise HTTPException(400, "no webhook set, or Discord rejected the post")
    return {"ok": True}


@app.post("/api/dats/{source}/upload")
async def dats_upload(source: str, file: UploadFile = File(...)):
    """Upload a No-Intro / Redump / MAME DAT (they have no open API)."""
    if source not in ("no-intro", "redump", "mame"):
        raise HTTPException(400, "upload is for no-intro / redump / mame")
    from app.services.dat_source import DATS_ROOT
    from app.core import dat_sources
    dest_dir = DATS_ROOT / source
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = Path(file.filename or "upload.dat").name
    if not name.lower().endswith((".dat", ".xml")):
        raise HTTPException(400, "expected a .dat or .xml Logiqx DAT")
    (dest_dir / name).write_bytes(await file.read())
    with SessionLocal() as db:
        dat_sources.record_update(db, source, {"uploaded": name})
    return {"source": source, "saved": name, "dir": str(dest_dir)}


# ---------- organize / clean to the chosen archive (dry-run first, rollback-able) ----------
@app.get("/api/systems/{folder}/organize/plan")
def organize_plan(folder: str):
    """DRY-RUN. Compute what Organize would do — renames + quarantines. Changes nothing."""
    from app.services.organize import build_plan
    with SessionLocal() as db:
        return build_plan(db, folder)


@app.post("/api/systems/{folder}/organize/apply")
def organize_apply(folder: str, confirm: bool = Query(False)):
    """Execute the plan: rename members to canonical names, MOVE non-members/extras to
    _quarantine (never deleted). Writes a rollback manifest first. Requires confirm=true."""
    if not confirm:
        raise HTTPException(400, "organize is destructive-ish (moves files) — pass confirm=true")
    from app.services.organize import apply_plan
    with SessionLocal() as db:
        return apply_plan(db, folder)


@app.get("/api/systems/{folder}/publish/plan")
def publish_plan(folder: str):
    """DRY-RUN. What Publish would move into the canonical library. Changes nothing."""
    from app.services.organize import build_publish_plan
    with SessionLocal() as db:
        return build_publish_plan(db, folder)


@app.post("/api/systems/{folder}/publish/apply")
def publish_apply(folder: str, confirm: bool = Query(False)):
    """Move the gated + highest-priority keepers into romfleet/roms/<folder>/. Manifest is
    written first (rollback via /organize/rollback with the publish manifest). confirm=true."""
    if not confirm:
        raise HTTPException(400, "publish moves files into the canonical library — pass confirm=true")
    from app.services.organize import apply_publish
    with SessionLocal() as db:
        return apply_publish(db, folder)


@app.get("/api/systems/{folder}/organize/quarantine")
def organize_quarantine(folder: str):
    from app.services.organize import quarantine_status
    return quarantine_status(folder)


@app.post("/api/systems/{folder}/organize/purge")
def organize_purge(folder: str, confirm: bool = Query(False)):
    """Reclaim space: permanently delete the reviewed _quarantine contents. A purge log
    is written first. Irreversible — requires confirm=true."""
    if not confirm:
        raise HTTPException(400, "purge permanently deletes quarantined files — pass confirm=true")
    from app.services.organize import purge_quarantine
    with SessionLocal() as db:
        return purge_quarantine(db, folder)


@app.get("/api/systems/{folder}/organize/manifests")
def organize_manifests(folder: str):
    from app.services.organize import list_manifests
    return list_manifests(folder)


@app.post("/api/systems/{folder}/organize/rollback")
def organize_rollback(folder: str, manifest: str | None = None):
    """Reverse an organize run (latest manifest if none named): move every file back."""
    from app.services.organize import rollback
    with SessionLocal() as db:
        return rollback(db, folder, manifest)


@app.get("/api/systems/{folder}/missing")
async def system_missing(folder: str, source: str | None = None):
    """The wanted list — catalog games (RA / No-Intro / LLA) not represented by any owned ROM."""
    from app.services.missing import missing_for_system
    return await missing_for_system(folder, source)


@app.post("/api/systems/{folder}/source-missing")
async def source_missing_ep(folder: str, limit: int | None = None,
                            ids: str | None = None, allow_search: bool = True):
    """Auto-source the missing games from archive.org (each download hash-verified vs RA
    before it's kept). Cartridge systems via the No-Intro ni-roms item.

    `ids` (comma-separated RA game ids) retries just those games, and `allow_search=false`
    skips the archive.org title search. Together they make "re-attempt this one game and tell
    me why it failed" cheap — the patch verdicts live in memory in THIS process, so an
    out-of-process run can never update what the Missing page shows.
    """
    from app.services.sourcing import source_missing
    id_list = None
    if ids:
        try:
            id_list = [int(x) for x in ids.replace(" ", "").split(",") if x]
        except ValueError:
            raise HTTPException(status_code=400, detail="ids must be comma-separated integers")
    return await source_missing(folder, limit, allow_search=allow_search, ids=id_list)


@app.get("/api/endpoints")
def api_endpoints():
    """Deploy targets (agents) for the Deploy button."""
    from app.services.push_launchbox import load_endpoints
    return {"endpoints": [{"name": n, "host": e.host, "label": f"{n} (LaunchBox)"}
                          for n, e in load_endpoints().items()]}


@app.post("/api/systems/{folder}/push")
def system_push(folder: str, endpoint: str = Query(settings.default_endpoint), xml_only: bool = Query(False)):
    """Deploy one system to an agent (endpoint). Runs in the background; watch the Agents
    page (or the deployment record) for the result."""
    from app.services.push_launchbox import push_system, get_endpoint
    try:
        get_endpoint(endpoint)
    except ValueError as e:
        raise HTTPException(400, str(e))
    import threading

    def _run():
        try:
            push_system(folder, endpoint, xml_only=xml_only)
        except Exception as e:  # noqa
            import structlog
            structlog.get_logger().warning("deploy failed", system=folder, endpoint=endpoint, err=str(e))
    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "status": "deploying", "system": folder, "endpoint": endpoint}


@app.get("/api/agents")
def api_agents():
    """Per-agent status: which systems each agent has deployed + game/hidden counts."""
    from app.services.push_launchbox import load_endpoints
    from app.db.models import Deployment
    out = []
    with SessionLocal() as db:
        for name, ep in load_endpoints().items():
            deps = db.query(Deployment).filter_by(endpoint=name).order_by(Deployment.system_folder).all()
            systems = [{"system": d.system_folder, "games": d.games, "hidden": d.hidden,
                        "additional_apps": d.additional_apps,
                        "pushed_at": d.pushed_at.isoformat() if d.pushed_at else None} for d in deps]
            out.append({"name": name, "host": ep.host, "lb_path": ep.lb_path, "kind": "LaunchBox",
                        "systems": systems,
                        "total_games": sum(s["games"] for s in systems),
                        "total_hidden": sum(s["hidden"] for s in systems)})
    return {"agents": out}


@app.post("/api/agents/{endpoint}/refresh")
def agent_refresh(endpoint: str, xml_only: bool = Query(False)):
    """Re-deploy EVERY system this agent has (from its deployment records), to push the
    latest metadata/media/favorites/hidden in one go. Runs in the background, sequentially
    (concurrent pushes to one agent would collide on its Platform/Emulators XML)."""
    from app.services.push_launchbox import push_system, get_endpoint
    from app.db.models import Deployment
    try:
        get_endpoint(endpoint)
    except ValueError as e:
        raise HTTPException(400, str(e))
    with SessionLocal() as db:
        systems = [d.system_folder for d in
                   db.query(Deployment).filter_by(endpoint=endpoint).order_by(Deployment.system_folder).all()]
    if not systems:
        return {"ok": True, "status": "nothing deployed", "systems": []}
    import threading

    def _run():
        import structlog
        log = structlog.get_logger()
        for s in systems:
            try:
                push_system(s, endpoint, xml_only=xml_only)
            except Exception as e:  # noqa
                log.warning("refresh push failed", system=s, endpoint=endpoint, err=str(e))
    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "status": "refreshing", "endpoint": endpoint,
            "systems": systems, "count": len(systems)}


class DeploySystems(BaseModel):
    systems: list[str]


@app.post("/api/agents/{endpoint}/deploy")
def agent_deploy(endpoint: str, body: DeploySystems, xml_only: bool = Query(False)):
    """Deploy MULTIPLE systems to one agent in a single action (checkbox picker on the Agents
    page). Runs in the background, sequentially — concurrent pushes to one agent collide on its
    Platform/Emulators XML (same reason `refresh` is sequential)."""
    from app.services.push_launchbox import push_system, get_endpoint
    try:
        get_endpoint(endpoint)
    except ValueError as e:
        raise HTTPException(400, str(e))
    systems = [s for s in dict.fromkeys(body.systems) if s]   # dedupe, preserve order
    if not systems:
        return {"ok": True, "status": "nothing selected", "systems": [], "count": 0}
    # Two folders that resolve to one LaunchBox platform will silently destroy each other:
    # rendering is per-folder and each push writes <Platform>.xml wholesale, so the later one
    # wins and the earlier one's games vanish with every log line still reporting success.
    # Report it in the response instead of discovering it by counting games on the target.
    # Fail fast if the agent is not there. Rendering a platform takes ~30-60s, so a deploy at
    # an offline machine spent an HOUR rendering 66 platforms and failing every scp, while the
    # UI showed the 200 from this endpoint and nothing else. One probe up front turns that into
    # an immediate, readable error.
    import socket
    ep = get_endpoint(endpoint)
    try:
        with socket.create_connection((ep.host, 22), timeout=6):
            pass
    except OSError as e:
        raise HTTPException(503, f"agent '{endpoint}' unreachable at {ep.host}:22 ({e}) — "
                                 f"is it powered on and on the network?")

    from app.services.render_launchbox import platform_collisions
    collisions = platform_collisions(systems)
    if collisions:
        log.error("platform collision — these folders share one LaunchBox platform and will "
                  "overwrite each other", collisions=collisions, endpoint=endpoint)
    import threading

    def _run():
        import structlog
        log = structlog.get_logger()
        for s in systems:
            try:
                push_system(s, endpoint, xml_only=xml_only)
            except Exception as e:  # noqa
                log.warning("multi-deploy push failed", system=s, endpoint=endpoint, err=str(e))
    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "status": "deploying", "endpoint": endpoint,
            "systems": systems, "count": len(systems),
            "platform_collisions": collisions or None}


# ---------- BIOS + Saves (assets) ----------
@app.get("/api/assets")
def api_assets():
    """Catalog the canonical BIOS (RetroArch system/) tree + save data, with agent list."""
    from app.services.assets import inventory
    return inventory()


@app.post("/api/agents/{endpoint}/deploy-bios")
def agent_deploy_bios(endpoint: str):
    """Mirror the whole BIOS/system tree to the agent's RetroArch system dir (background)."""
    from app.services.assets import push_bios
    from app.services.push_launchbox import get_endpoint
    try:
        get_endpoint(endpoint)
    except ValueError as e:
        raise HTTPException(400, str(e))
    import threading

    def _run():
        import structlog
        try:
            push_bios(endpoint)
        except Exception as e:  # noqa
            structlog.get_logger().warning("bios deploy failed", endpoint=endpoint, err=str(e))
    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "status": "deploying-bios", "endpoint": endpoint}


@app.post("/api/agents/{endpoint}/deploy-saves")
def agent_deploy_saves(endpoint: str):
    """Deploy the saves tree to the agent (background)."""
    from app.services.assets import push_saves
    from app.services.push_launchbox import get_endpoint
    try:
        get_endpoint(endpoint)
    except ValueError as e:
        raise HTTPException(400, str(e))
    import threading

    def _run():
        import structlog
        try:
            push_saves(endpoint)
        except Exception as e:  # noqa
            structlog.get_logger().warning("saves deploy failed", endpoint=endpoint, err=str(e))
    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "status": "deploying-saves", "endpoint": endpoint}


@app.post("/api/agents/{endpoint}/reconcile")
def agent_reconcile(endpoint: str):
    """Rebuild an agent's deployment records from what's ACTUALLY on it (reads its
    Data\\Platforms\\*.xml game/hide counts). Backfills / re-syncs the Agents page."""
    from app.services.push_launchbox import get_endpoint, _ssh, _ps_encoded
    from app.services.render_launchbox import platform_for
    from app.core.systems import load_systems
    from app.db.models import Deployment
    ep = get_endpoint(endpoint)
    lb = ep.lb_path.rstrip("\\")
    script = (f"Get-ChildItem '{lb}\\Data\\Platforms\\*.xml' -EA SilentlyContinue | ForEach-Object {{"
              "$c=(Select-String $_.FullName -Pattern '<Game>' -AllMatches -EA SilentlyContinue).Matches.Count;"
              "$h=(Select-String $_.FullName -Pattern '<Hide>true' -AllMatches -EA SilentlyContinue).Matches.Count;"
              "\"$($_.BaseName)`t$c`t$h\" }")
    r = _ssh(ep, _ps_encoded(script))
    plat_to_folder: dict = {}
    for s in load_systems():
        p = platform_for(s.folder)
        if p:
            plat_to_folder.setdefault(p, s.folder)
    n = 0
    with SessionLocal() as db:
        db.query(Deployment).filter_by(endpoint=endpoint).delete()
        for line in (r.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            plat, games, hidden = (p.strip() for p in parts)
            folder = plat_to_folder.get(plat)
            if folder and games.isdigit():
                db.add(Deployment(endpoint=endpoint, system_folder=folder, games=int(games),
                                  hidden=int(hidden) if hidden.isdigit() else 0))
                n += 1
        db.commit()
    return {"ok": True, "endpoint": endpoint, "systems": n}


@app.get("/api/missing")
async def api_missing_all():
    """Fleet-wide wanted list with a sourceability status per game, so you don't hunt
    per-system: sourceable (retail/No-Intro), patchable (hack/translation with a RAPatches
    patch), no-patch (hack, no patch on RAPatches), homebrew (aftermarket — needs drop-off)."""
    from app.services.missing import missing_for_system
    from app.core.systems import load_systems
    from app.db.models import Event
    from app.db.session import SessionLocal

    # Last recorded sourcing attempt per RA game id. Without this the page can only say what
    # a game SHOULD be (its status), never what happened when we tried — so a "sourceable"
    # game sitting in the list forever looks like a fact rather than the bug signal it is.
    last_attempt: dict[int, dict] = {}
    _db = SessionLocal()
    try:
        for ev in (_db.query(Event).filter(Event.kind == "source_attempt")
                   .order_by(Event.created_at.desc()).limit(4000).all()):
            gid = (ev.data or {}).get("game_id")
            if gid is not None and gid not in last_attempt:
                last_attempt[gid] = {"reason": (ev.data or {}).get("reason"),
                                     "at": ev.created_at.isoformat() if ev.created_at else None}
    except Exception:  # noqa — the page must render even if the events table is unhappy
        last_attempt = {}
    finally:
        _db.close()

    rows, by_sys = [], []
    for sysc in load_systems():
        try:
            r = await missing_for_system(sysc.folder)
        except Exception:
            continue
        if not r.get("owned"):        # skip systems not yet onboarded — those are "not started", not gaps
            continue
        miss = r.get("missing", [])
        if miss:
            by_sys.append({"system": sysc.folder, "missing": len(miss)})
        ra_gate = r.get("gate") == "ra"
        for m in miss:
            cat = (m.get("category") or "").lower()
            if not ra_gate:
                status = "sourceable"
            elif any(w in cat for w in ("homebrew", "aftermarket", "unlicensed", "prototype", "demo")):
                status = "homebrew"
            elif m.get("has_patch"):
                # Patch availability is checked BEFORE category on purpose. RA's Fix /
                # RACompatibility entries carry no ~Hack~ tag, so they read as ordinary
                # "standard" games — but RA only recognises the PATCHED image, so calling them
                # "sourceable" is wrong and sends you hunting for a download that cannot exist.
                # Hansel and Gretel, Cinderella and Snow White surfaced exactly this way.
                status = "patchable"
            elif any(w in cat for w in ("hack", "translation")):
                status = "no-patch"
            else:
                status = "sourceable"
            att = last_attempt.get(m.get("id"))
            # An incomplete multi-disc set is a DIFFERENT kind of gap: we hold the game but
            # not all of it. Its own reason must win over the sourcing-attempt text, which
            # would otherwise say "not attempted yet" for a game sitting in the library.
            gap = m.get("incomplete_discs")
            row = {"system": sysc.folder, "title": m["title"], "category": m.get("category"),
                   "achievements": m.get("achievements", 0), "points": m.get("points", 0),
                   "status": "incomplete-discs" if gap else status, "url": m.get("url"),
                   "incomplete_discs": gap,
                   "reason": (m.get("reason") if gap
                              else (att or {}).get("reason") or "not attempted yet"),
                   "attempted_at": (att or {}).get("at"),
                   # A game we classify as obtainable that has been TRIED and still isn't here
                   # is a defect somewhere in the pipeline, not an expected gap. Surface it.
                   "anomaly": bool(att) and status in ("sourceable", "patchable")}
            if status == "homebrew":
                from urllib.parse import quote_plus
                row["itch"] = "https://itch.io/search?q=" + quote_plus(m["title"])
            rows.append(row)
    # Anomalies first — a game we call obtainable that we've already tried and failed to get
    # is the actionable row on this page; everything else is an expected gap.
    order = {"incomplete-discs": 0, "sourceable": 1, "patchable": 2, "no-patch": 3, "homebrew": 4}
    rows.sort(key=lambda x: (not x["anomaly"], order.get(x["status"], 9),
                             -x["achievements"], x["title"].lower()))
    counts: dict = {}
    for x in rows:
        counts[x["status"]] = counts.get(x["status"], 0) + 1
    return {"total": len(rows), "counts": counts, "by_system": by_sys,
            "anomalies": sum(1 for x in rows if x["anomaly"]), "missing": rows}


@app.get("/api/extras")
def api_extras():
    """Files in the canonical store that aren't tracked matched ROMs — untracked strays,
    unmatched files, quarantine leftovers, macOS sidecars, unexpected extensions."""
    from pathlib import Path
    from app.core.systems import load_systems
    root = Path(settings.canonical_path) / "roms"
    extras = []
    with SessionLocal() as db:
        for sysc in load_systems():
            d = root / sysc.folder
            if not d.is_dir():
                continue
            matched = {r[0] for r in db.query(RomFile.filename).filter(
                RomFile.system_folder == sysc.folder, RomFile.match_status == "matched").all()}
            for p in sorted(d.rglob("*")):
                if p.is_dir() or p.name.startswith(".romfleet"):
                    continue
                name = p.name
                if name.startswith("._") or name == ".DS_Store":
                    reason = "macOS sidecar"
                elif "_quarantine" in p.parts:
                    reason = "quarantine leftover"
                elif len(p.relative_to(d).parts) > 1:
                    continue        # component of a multi-file (disc) ROM folder — not an extra
                elif name in matched:
                    continue        # a tracked, matched ROM
                else:
                    reason = "untracked / not matched"
                extras.append({"system": sysc.folder, "file": str(p.relative_to(root)),
                               "reason": reason, "bytes": p.stat().st_size})
    counts: dict = {}
    for x in extras:
        counts[x["reason"]] = counts.get(x["reason"], 0) + 1
    return {"total": len(extras), "bytes": sum(x["bytes"] for x in extras),
            "counts": counts, "extras": extras}


class ExtraDel(BaseModel):
    file: str   # path relative to <canonical>/roms


@app.post("/api/extras/delete")
def api_extras_delete(payload: ExtraDel, confirm: bool = Query(False)):
    """Delete one extra file (path relative to <canonical>/roms); requires confirm=true."""
    from pathlib import Path
    if not confirm:
        raise HTTPException(400, "deletes a file — pass confirm=true")
    root = (Path(settings.canonical_path) / "roms").resolve()
    target = (root / payload.file).resolve()
    if not str(target).startswith(str(root) + "/") or not target.is_file():
        raise HTTPException(400, "invalid path")
    sz = target.stat().st_size
    target.unlink()
    with SessionLocal() as db:
        n = db.query(RomFile).filter(RomFile.filename == target.name,
                                     RomFile.system_folder == target.parent.name
                                     ).delete(synchronize_session=False)
        db.commit()
    return {"ok": True, "deleted": payload.file, "bytes": sz, "rows_removed": n}


_DEDUP_MODES = {"hash", "slot", "core"}


@app.get("/api/dedup")
def dedup_plan_all(folder: str | None = Query(None),
                   mode: str = Query("hash", pattern="^(hash|slot|core)$")):
    """Dry-run duplicate detection. mode=hash: byte-identical files under one RA game.
    mode=slot: 1G1R version-dupes (region/lang/rev/translation, English wins) — safe.
    mode=core: cross-naming redumps (same hack, TOSEC vs RA name) — review before applying.
    ?folder= scopes to one system; omit for the whole fleet."""
    from app.services.dedup import plan_dedup, plan_version_dedup
    with SessionLocal() as db:
        if mode == "hash":
            return plan_dedup(db, folder)
        return plan_version_dedup(db, folder, key=mode)


@app.post("/api/dedup")
def dedup_apply_all(folder: str | None = Query(None), confirm: bool = Query(False),
                    mode: str = Query("hash", pattern="^(hash|slot|core)$")):
    """Delete the non-keeper file(s) in every dup group (disk + DB) for the chosen mode.
    Requires confirm=true; without it, returns the dry-run plan."""
    from app.services.dedup import plan_dedup, apply_dedup, plan_version_dedup, apply_version_dedup
    with SessionLocal() as db:
        if not confirm:
            plan = plan_dedup(db, folder) if mode == "hash" else plan_version_dedup(db, folder, key=mode)
            return {"dry_run": True, "mode": mode, **plan}
        return apply_dedup(db, folder) if mode == "hash" else apply_version_dedup(db, folder, key=mode)


@app.get("/api/systems/{folder}/roms")
def system_roms(folder: str, status: str | None = None, q: str | None = None,
                missing: str | None = None,   # "media" | "metadata" | "any"
                category: str | None = None,  # game type; "__none__" = uncategorized
                limit: int = Query(2000, le=8000), offset: int = 0):
    with SessionLocal() as db:
        stmt = select(RomFile).where(RomFile.system_folder == folder)
        if status:
            stmt = stmt.where(RomFile.match_status == status)
        if q:
            stmt = stmt.where(RomFile.filename.ilike(f"%{q}%"))
        rows = db.scalars(stmt).all()
        gids = {r.game_id for r in rows if r.game_id}
        games = {g.id: g for g in db.scalars(
            select(Game).where(Game.id.in_(gids)).options(selectinload(Game.media))).all()} if gids else {}

        out = []
        for r in rows:
            g = games.get(r.game_id)
            if category:
                gcat = g.category if g else None
                if category == "__none__":
                    if gcat:
                        continue
                elif gcat != category:
                    continue
            cov = _coverage(g)
            if missing == "media" and not (cov and cov["missing_media"]):
                continue
            if missing == "metadata" and not (cov and cov["metadata_missing"]):
                continue
            if missing == "any" and not (cov and not cov["complete"]):
                continue
            if missing in ("boxfront", "titlescreen", "screenshot", "logo", "background", "video"):
                present = {m.media_type for m in g.media} if g else set()
                if g is None or missing in present:
                    continue
            out.append({
                "id": r.id, "game_id": r.game_id,
                "game": (g.title if g else None), "category": (g.category if g else None),
                "filename": r.filename, "status": r.match_status,
                "matched_dbs": list((r.dat_matches or {}).keys()),
                "coverage": cov,
            })
        out.sort(key=lambda x: ((x["game"] or x["filename"] or "").lower()))
        return {"total": len(out), "roms": out[offset:offset + limit]}


@app.get("/api/systems/{folder}/categories")
def system_categories(folder: str):
    """Distinct game types present in a system, with counts, for the type filter."""
    with SessionLocal() as db:
        gids = [r[0] for r in db.query(RomFile.game_id).filter(
            RomFile.system_folder == folder, RomFile.game_id.isnot(None)).distinct().all()]
        cats: dict[str, int] = {}
        none_n = 0
        for (cat,) in db.query(Game.category).filter(Game.id.in_(gids)).all() if gids else []:
            if cat:
                cats[cat] = cats.get(cat, 0) + 1
            else:
                none_n += 1
        out = [{"category": c, "count": n} for c, n in sorted(cats.items())]
        if none_n:
            out.append({"category": "__none__", "label": "(none)", "count": none_n})
        return out


# ---------- game detail / edit ----------
@app.get("/api/games/{game_id}")
def game_detail(game_id: int):
    with SessionLocal() as db:
        g = db.get(Game, game_id)
        if not g:
            raise HTTPException(404, "game not found")
        return _game_dict(g, full=True)


class GamePatch(BaseModel):
    title: str | None = None
    sort_title: str | None = None
    region: str | None = None
    players: str | None = None
    genre: str | None = None
    developer: str | None = None
    publisher: str | None = None
    release_date: str | None = None
    description: str | None = None
    rating: str | None = None


@app.patch("/api/games/{game_id}")
def game_edit(game_id: int, patch: GamePatch):
    with SessionLocal() as db:
        g = db.get(Game, game_id)
        if not g:
            raise HTTPException(404, "game not found")
        locked = set(g.locked_fields or [])
        for f, v in patch.model_dump(exclude_unset=True).items():
            setattr(g, f, v)
            locked.add(f)          # only fields the client actually sent get locked
        g.locked_fields = sorted(locked)
        db.commit()
        db.refresh(g)
        autopush.mark_game_dirty(game_id, media=False)   # metadata edit -> XML-only push
        return _game_dict(g, full=True)


@app.post("/api/games/{game_id}/favorite")
def toggle_favorite(game_id: int, value: bool | None = Query(None, description="explicit set; omit to toggle")):
    """Mark/unmark a game a Favorite centrally — autopush renders <Favorite> to every endpoint."""
    with SessionLocal() as db:
        g = db.get(Game, game_id)
        if not g:
            raise HTTPException(404, "game not found")
        g.is_favorite = (not g.is_favorite) if value is None else bool(value)
        db.commit()
        autopush.mark_game_dirty(game_id, media=False)   # favorite change -> XML-only push
        return {"ok": True, "is_favorite": g.is_favorite}


@app.post("/api/games/{game_id}/rating")
def set_rating(game_id: int, value: float = Query(..., ge=0, le=5, description="0-5 personal grade; 0 clears it (falls back to community)")):
    """Set a personal 0-5 star grade centrally — autopush renders it to every endpoint."""
    with SessionLocal() as db:
        g = db.get(Game, game_id)
        if not g:
            raise HTTPException(404, "game not found")
        g.star_rating = None if value <= 0 else round(value, 2)
        db.commit()
        autopush.mark_game_dirty(game_id, media=False)   # rating change -> XML-only push
        return {"ok": True, "star_rating": g.star_rating}


class HiddenPatch(BaseModel):
    hidden_on: list[str]   # ["*"] = all agents; endpoint names; [] = visible everywhere


@app.post("/api/games/{game_id}/hidden")
def set_hidden(game_id: int, payload: HiddenPatch):
    """Hide a game on specific agents (endpoint names) or all agents ('*'). Renders
    <Hide>true> for those agents; autopush propagates the change."""
    with SessionLocal() as db:
        g = db.get(Game, game_id)
        if not g:
            raise HTTPException(404, "game not found")
        g.hidden_on = [h for h in payload.hidden_on if h]
        db.commit()
        autopush.mark_game_dirty(game_id, media=False)   # hide change -> XML-only re-push
        return {"ok": True, "hidden_on": g.hidden_on}


@app.post("/api/games/{game_id}/lock/{field}")
def lock_field(game_id: int, field: str):
    """Lock a field so scrapers/fills won't change it (independent of editing)."""
    with SessionLocal() as db:
        g = db.get(Game, game_id)
        if not g:
            raise HTTPException(404, "game not found")
        locked = set(g.locked_fields or [])
        locked.add(field)
        g.locked_fields = sorted(locked)
        db.commit()
        return {"ok": True, "locked_fields": g.locked_fields}


@app.delete("/api/games/{game_id}/lock/{field}")
def unlock_field(game_id: int, field: str):
    """Unlock a field so scrapers/fills may set it again."""
    with SessionLocal() as db:
        g = db.get(Game, game_id)
        if not g:
            raise HTTPException(404, "game not found")
        g.locked_fields = [f for f in (g.locked_fields or []) if f != field]
        db.commit()
        return {"ok": True, "locked_fields": g.locked_fields}


# ---------- per-source metadata/media fill ----------
def _game_folder(db, g: Game) -> str | None:
    """The game's system folder (for per-folder platform overrides on shared-console
    sub-systems). Prefers a folder that HAS an override so a WonderSwan Color / 64DD / etc.
    game resolves to its own platform, not the shared base."""
    from app.services.scraper_lbdb import LBDB_PLATFORM_BY_FOLDER
    folders = [r[0] for r in db.query(RomFile.system_folder).filter(
        RomFile.game_id == g.id).distinct().all()]
    for f in folders:
        if f in LBDB_PLATFORM_BY_FOLDER:
            return f
    return folders[0] if folders else None


def _scrape_one(db, g: Game, source: str, only_missing: bool = True) -> dict:
    """Dispatch a single-source scrape for one game. All scrapers try source_ids[source]
    before any fuzzy name-match. only_missing controls LBDB overwrite (True = 'fill' button,
    False = a pinned exact id)."""
    result: dict = {"source": source}
    if source == "ra":
        from app.services.scraper_ra import scrape_ra
        result.update(scrape_ra(db, g, MEDIA_STORE))
    elif source == "igdb":
        from app.services.scraper_igdb import IgdbScraper, scrape_igdb, igdb_platform_for
        igdb = IgdbScraper()
        if not igdb.configured:
            raise HTTPException(400, "IGDB not set (IGDB_CLIENT_ID/IGDB_CLIENT_SECRET in .env)")
        try:
            pid = igdb_platform_for(_game_folder(db, g), g.console_id)
            result.update(scrape_igdb(db, g, igdb, pid, MEDIA_STORE))
        finally:
            igdb.close()
    elif source == "ss":
        from app.services.scraper_ss import SsScraper, scrape_ss
        ss = SsScraper()
        if not ss.configured:
            raise HTTPException(400, "ScreenScraper not set (SS_DEV_ID/SS_DEV_PASSWORD in .env)")
        try:
            result.update(scrape_ss(db, g, ss, MEDIA_STORE, folder=_game_folder(db, g)))
        finally:
            ss.close()
    elif source == "lbdb":
        import httpx
        from app.services.scraper_lbdb import LbdbScraper, scrape_game, lbdb_platform_for
        lbdb = LbdbScraper()
        if not (g.source_ids or {}).get("lbdb"):        # index only needed for name-match
            platform = lbdb_platform_for(_game_folder(db, g), g.console_id)
            if not platform:
                raise HTTPException(400, f"no LBDB platform mapping for console {g.console_id}")
            lbdb.load_platform(platform)
        http = httpx.Client(headers={"User-Agent": "RomFleet/0.1"}, follow_redirects=True)
        try:
            result.update(scrape_game(db, g, lbdb, MEDIA_STORE, http, only_missing=only_missing))
        finally:
            http.close(); lbdb.close()
    elif source == "tgdb":
        from app.services.scraper_tgdb import TgdbScraper, scrape_tgdb, TGDB_PLATFORM
        tgdb = TgdbScraper()
        if not tgdb.configured:
            raise HTTPException(400, "TheGamesDB API key not set (TGDB_API_KEY in .env)")
        try:
            result.update(scrape_tgdb(db, g, tgdb, TGDB_PLATFORM.get(g.console_id), MEDIA_STORE))
        finally:
            tgdb.close()
    elif source == "emumovies":
        from app.services.scraper_emumovies import EmuMovies, scrape_video
        emu = EmuMovies()
        if not emu.configured:
            raise HTTPException(400, "EmuMovies creds not set (EMU_USER/EMU_PASS in .env)")
        if not emu.login():
            emu.close(); raise HTTPException(502, "EmuMovies login failed")
        try:
            result.update(scrape_video(db, g, emu, MEDIA_STORE, folder=_game_folder(db, g)))
        finally:
            emu.close()
    elif source == "arcadedb":
        from app.services.scraper_arcadedb import ArcadeDB, scrape_arcadedb
        adb = ArcadeDB()
        try:
            result.update(scrape_arcadedb(db, g, adb, MEDIA_STORE, folder=_game_folder(db, g)))
        finally:
            adb.close()
    return result


@app.post("/api/games/{game_id}/scrape")
def scrape_source(game_id: int, source: str = Query(..., pattern="^(ra|lbdb|tgdb|emumovies|igdb|ss|arcadedb)$")):
    """Fill unlocked/blank fields (and media) for one game from a specific source."""
    with SessionLocal() as db:
        g = db.get(Game, game_id)
        if not g:
            raise HTTPException(404, "game not found")
        result = _scrape_one(db, g, source, only_missing=True)
        db.commit(); db.refresh(g)
        autopush.mark_game_dirty(game_id, media=True)    # scrape may pull media -> full push
        return {**_game_dict(g, full=True), "_scrape": result}


# ---------- manual source-id override (bypass fuzzy matching) ----------
@app.post("/api/games/{game_id}/source-id")
def set_source_id(game_id: int, source: str = Query(..., pattern="^(ra|lbdb|tgdb|emumovies|igdb|ss|arcadedb)$"),
                  value: str = Query(...)):
    """Pin a game to an exact source id (or exact title for EmuMovies), then scrape by it."""
    with SessionLocal() as db:
        g = db.get(Game, game_id)
        if not g:
            raise HTTPException(404, "game not found")
        sid = dict(g.source_ids or {})
        sid[source] = int(value) if value.isdigit() else value
        g.source_ids = sid
        result = _scrape_one(db, g, source, only_missing=False)
        db.commit(); db.refresh(g)
        autopush.mark_game_dirty(game_id, media=True)
        return {**_game_dict(g, full=True), "_scrape": result}


# ---------- LBDB name search (to pick the exact game id) ----------
@app.get("/api/lbdb/search")
def lbdb_search(console_id: int, q: str, limit: int = Query(15, le=50)):
    from app.services.scraper_lbdb import LbdbScraper, LBDB_PLATFORM
    platform = LBDB_PLATFORM.get(console_id)
    if not platform:
        return []
    s = LbdbScraper()
    try:
        return s.search_by_name(platform, q, limit)
    finally:
        s.close()


# ---------- bulk scrape (background) ----------
_JOBS: dict[str, dict] = {}


def _system_games(db, folder: str):
    gids = [r[0] for r in db.query(RomFile.game_id).filter(
        RomFile.system_folder == folder, RomFile.game_id.isnot(None)).distinct().all()]
    return db.scalars(select(Game).where(Game.id.in_(gids)).options(selectinload(Game.media))).all()


def _bulk_scrape(folder: str, source: str):
    """Bulk-scrape only games that still need it (incomplete for lbdb/tgdb; no locked
    video for emumovies). Auto-lock means completed/locked games are naturally skipped."""
    _JOBS[folder] = {"running": True, "source": source, "done": 0, "total": 0, "matched": 0, "images": 0}
    from app.core.systems import get_system_by_folder
    sysc = get_system_by_folder(folder)
    try:
        with SessionLocal() as db:
            all_games = _system_games(db, folder)
            done = matched = images = 0

            if source in ("lbdb", "tgdb"):
                targets = [g for g in all_games if not (_coverage(g) or {}).get("complete")]
                total = len(targets)
                _JOBS[folder]["total"] = total
                if source == "lbdb":
                    import httpx
                    from app.services.scraper_lbdb import LbdbScraper, scrape_game, lbdb_platform_for
                    sc = LbdbScraper(); sc.load_platform(lbdb_platform_for(folder, sysc.ra_console_id))
                    http = httpx.Client(headers={"User-Agent": "RomFleet/0.1"}, follow_redirects=True)
                    for g in targets:
                        r = scrape_game(db, g, sc, MEDIA_STORE, http)
                        matched += 1 if r.get("matched") else 0; images += r.get("images", 0)
                        done += 1; _JOBS[folder].update(done=done, matched=matched, images=images)
                    http.close(); sc.close()
                else:
                    from app.services.scraper_tgdb import TgdbScraper, scrape_tgdb, TGDB_PLATFORM
                    sc = TgdbScraper()
                    if not sc.configured:
                        raise RuntimeError("TheGamesDB API key not set")
                    pid = TGDB_PLATFORM.get(sysc.ra_console_id)
                    for g in targets:
                        r = scrape_tgdb(db, g, sc, pid, MEDIA_STORE)
                        matched += 1 if r.get("matched") else 0; images += r.get("images", 0)
                        done += 1; _JOBS[folder].update(done=done, matched=matched, images=images)
                    sc.close()

            elif source == "igdb":
                from app.services.scraper_igdb import IgdbScraper, scrape_igdb, igdb_platform_for
                sc = IgdbScraper()
                if not sc.configured:
                    raise RuntimeError("IGDB not configured (IGDB_CLIENT_ID/IGDB_CLIENT_SECRET in .env)")
                targets = [g for g in all_games if not (_coverage(g) or {}).get("complete")]
                total = len(targets); _JOBS[folder]["total"] = total
                pid = igdb_platform_for(folder, sysc.ra_console_id)
                try:
                    for g in targets:
                        r = scrape_igdb(db, g, sc, pid, MEDIA_STORE)
                        matched += 1 if r.get("matched") else 0; images += r.get("images", 0)
                        done += 1; _JOBS[folder].update(done=done, matched=matched, images=images)
                finally:
                    sc.close()

            elif source == "ra":
                # RA box art + title/ingame screenshots (often the only art for hacks/homebrew).
                import time
                from app.services.scraper_ra import scrape_ra
                targets = [g for g in all_games if not (_coverage(g) or {}).get("complete")]
                total = len(targets); _JOBS[folder]["total"] = total
                for g in targets:
                    r = scrape_ra(db, g, MEDIA_STORE)
                    matched += 1 if r.get("matched") else 0; images += r.get("images", 0)
                    done += 1; _JOBS[folder].update(done=done, matched=matched, images=images)
                    time.sleep(0.1)   # courtesy to the RA API on bulk runs

            elif source == "ss":
                import time
                from app.services.scraper_ss import SsScraper, scrape_ss
                sc = SsScraper()
                if not sc.configured:
                    raise RuntimeError("ScreenScraper not configured (SS_DEV_ID/SS_DEV_PASSWORD)")
                targets = [g for g in all_games if not (_coverage(g) or {}).get("complete")]
                total = len(targets); _JOBS[folder]["total"] = total
                try:
                    for g in targets:
                        r = scrape_ss(db, g, sc, MEDIA_STORE, folder=folder)
                        matched += 1 if r.get("matched") else 0; images += r.get("images", 0)
                        done += 1; _JOBS[folder].update(done=done, matched=matched, images=images)
                        time.sleep(0.1)   # stay under SS thread/rate limits
                finally:
                    sc.close()

            elif source == "emumovies":
                from app.services.scraper_emumovies import EmuMovies, scrape_video
                emu = EmuMovies()
                if not emu.configured or not emu.login():
                    raise RuntimeError("EmuMovies not configured / login failed")
                targets = [g for g in all_games
                           if not any(m.media_type == "video" and m.is_locked for m in g.media)]
                total = len(targets); _JOBS[folder]["total"] = total
                for g in targets:
                    if scrape_video(db, g, emu, MEDIA_STORE, folder=folder).get("video") == "downloaded":
                        matched += 1
                    done += 1; _JOBS[folder].update(done=done, matched=matched)
                emu.close()
            elif source == "arcadedb":
                import time
                from app.services.scraper_arcadedb import ArcadeDB, scrape_arcadedb
                adb = ArcadeDB()
                targets = [g for g in all_games if not (_coverage(g) or {}).get("complete")]
                total = len(targets); _JOBS[folder]["total"] = total
                try:
                    for g in targets:
                        r = scrape_arcadedb(db, g, adb, MEDIA_STORE, folder=folder)
                        matched += 1 if r.get("matched") else 0; images += r.get("images", 0)
                        done += 1; _JOBS[folder].update(done=done, matched=matched, images=images)
                        time.sleep(0.15)   # ArcadeDB: single connection per IP — stay polite
                finally:
                    adb.close()
            else:
                raise RuntimeError("bulk supports ra / lbdb / tgdb / igdb / ss / emumovies / arcadedb")

            db.commit()
        _JOBS[folder] = {"running": False, "source": source, "done": done,
                         "total": total, "matched": matched, "images": images}
    except Exception as e:
        _JOBS[folder] = {"running": False, "source": source, "error": str(e)[:200]}


@app.post("/api/discord/weekly-roundup")
async def discord_weekly_roundup(days: int = Query(7, ge=1, le=31),
                                 dry_run: bool = Query(False),
                                 force: bool = Query(False)):
    """The Saturday digest. dry_run returns the embeds without posting, so the format can be
    checked without spending a message in the channel."""
    from app.services import discord_notify
    with SessionLocal() as db:
        if dry_run:
            return {"dry_run": True,
                    "embeds": await discord_notify.build_weekly_roundup(db, days=days)}
        return await discord_notify.post_weekly_roundup(db, days=days, force=force)


@app.post("/api/systems/{folder}/scrape-all")
def scrape_all(folder: str, source: str = Query("lbdb", pattern="^(ra|lbdb|tgdb|igdb|ss|emumovies|arcadedb)$"),
               bg: BackgroundTasks = None):
    if _JOBS.get(folder, {}).get("running"):
        raise HTTPException(409, "a scrape is already running for this system")
    bg.add_task(_bulk_scrape, folder, source)
    return {"started": True, "system": folder, "source": source}


@app.get("/api/systems/{folder}/scrape-status")
def scrape_status(folder: str):
    return _JOBS.get(folder, {"running": False})


# ---------- manual entry for an unmatched rom ----------
class ManualGame(BaseModel):
    title: str


@app.post("/api/roms/{rom_id}/manual")
def create_manual(rom_id: int, body: ManualGame):
    with SessionLocal() as db:
        r = db.get(RomFile, rom_id)
        if not r:
            raise HTTPException(404, "rom not found")
        sysrow = db.scalar(select(System).where(System.folder == r.system_folder))
        g = Game(is_manual=True, title=body.title,
                 console_id=(sysrow.ra_console_id if sysrow else 0),
                 metadata_source="manual", locked_fields=["title"])
        db.add(g)
        db.flush()
        r.game_id = g.id
        r.match_status = "manual"
        db.commit()
        db.refresh(g)
        return _game_dict(g, full=True)


# ---------- media management ----------
@app.post("/api/games/{game_id}/media")
async def upload_media(game_id: int, media_type: str = Form(...), file: UploadFile = File(...)):
    if media_type not in MEDIA_TYPES:
        raise HTTPException(400, f"bad media_type; one of {MEDIA_TYPES}")
    with SessionLocal() as db:
        g = db.get(Game, game_id)
        if not g:
            raise HTTPException(404, "game not found")
        ext = Path(file.filename or "").suffix.lower() or ".bin"
        rel_dir = Path(str(g.console_id)) / str(g.id)
        (MEDIA_STORE / rel_dir).mkdir(parents=True, exist_ok=True)
        existing = [m for m in g.media if m.media_type == media_type]
        rel_path = rel_dir / f"{media_type}-{len(existing) + 1}{ext}"
        with open(MEDIA_STORE / rel_path, "wb") as out:
            shutil.copyfileobj(file.file, out)
        m = MediaAsset(game_id=g.id, media_type=media_type, source="upload",
                       path=str(rel_path), is_primary=(len(existing) == 0), is_locked=True)
        db.add(m)
        db.commit()
        db.refresh(g)
        autopush.mark_game_dirty(game_id, media=True)    # new media -> full push
        return _game_dict(g, full=True)


@app.patch("/api/media/{media_id}/primary")
def set_primary(media_id: int):
    with SessionLocal() as db:
        m = db.get(MediaAsset, media_id)
        if not m:
            raise HTTPException(404, "media not found")
        for sib in db.scalars(select(MediaAsset).where(
                MediaAsset.game_id == m.game_id, MediaAsset.media_type == m.media_type)).all():
            sib.is_primary = (sib.id == media_id)
        gid = m.game_id
        db.commit()
        autopush.mark_game_dirty(gid, media=True)        # primary image changed -> full push
        return {"ok": True}


@app.patch("/api/media/{media_id}/lock")
def toggle_media_lock(media_id: int):
    with SessionLocal() as db:
        m = db.get(MediaAsset, media_id)
        if not m:
            raise HTTPException(404, "media not found")
        m.is_locked = not m.is_locked
        db.commit()
        return {"ok": True, "is_locked": m.is_locked}


@app.delete("/api/media/{media_id}")
def delete_media(media_id: int):
    with SessionLocal() as db:
        m = db.get(MediaAsset, media_id)
        if not m:
            raise HTTPException(404, "media not found")
        if m.is_locked:
            raise HTTPException(409, "media is locked — unlock it first")
        if m.path:
            (MEDIA_STORE / m.path).unlink(missing_ok=True)
        gid = m.game_id
        db.delete(m)
        db.commit()
        autopush.mark_game_dirty(gid, media=True)        # media removed -> full push
        return {"ok": True}


# ---------- static ----------
app.mount("/media", StaticFiles(directory=str(MEDIA_STORE)), name="media")
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


# ---------- settings: priority scoring profile ----------
@app.get("/api/settings/scoring")
def get_scoring_cfg():
    from app.core.scoring import get_scoring, DEFAULT_SCORING
    with SessionLocal() as db:
        return {"config": get_scoring(db), "all_regions": DEFAULT_SCORING["region_order"]}


@app.put("/api/settings/scoring")
def put_scoring_cfg(cfg: dict):
    from app.core.scoring import save_scoring
    with SessionLocal() as db:
        return {"config": save_scoring(db, cfg)}


@app.post("/api/settings/scoring/preview")
def preview_scoring(cfg: dict):
    """Score sample hash names against a (possibly-unsaved) profile — ranked best-first."""
    from app.core.hash_priority import parse_and_score
    from app.core.scoring import score_parsed, DEFAULT_SCORING
    samples = cfg.pop("_names", None) or [
        "Sonic The Hedgehog (USA, Europe) (Rev 2).md",
        "Sonic The Hedgehog (USA, Europe).md",
        "Sonic The Hedgehog (Japan).md",
        "Final Fantasy III (Japan) (En translation v1.1).sfc",
        "Final Fantasy III (Japan) (Ru).sfc",
        "Chrono Trigger (USA) (MSU-1).sfc",
    ]
    cfg = {**DEFAULT_SCORING, **(cfg or {})}
    out = []
    for n in samples:
        ph = parse_and_score(md5="", name=n, labels=[], patch_url=None)
        s = score_parsed(ph, cfg)
        out.append({"name": n, "score": s, "excluded": s is None,
                    "regions": ph.regions, "rev": ph.revision})
    out.sort(key=lambda x: (x["excluded"], x["score"] if x["score"] is not None else 9e9))
    return out


# ---------- settings: media completeness profile ----------
@app.get("/api/settings/completeness")
def get_completeness_cfg():
    from app.core.completeness import get_completeness, ALL_MEDIA, ALL_META, DEFAULT_COMPLETENESS
    with SessionLocal() as db:
        return {"config": get_completeness(db), "all_media": ALL_MEDIA, "all_meta": ALL_META,
                "known_categories": ["Released"] + list(DEFAULT_COMPLETENESS["categories"].keys())
                                    + ["Unreleased", "DLC", "Early Access"]}


@app.put("/api/settings/completeness")
def put_completeness_cfg(cfg: dict):
    global _COMPLETENESS
    from app.core.completeness import save_completeness
    with SessionLocal() as db:
        saved = save_completeness(db, cfg)
    _COMPLETENESS = None   # invalidate cache so the audit recomputes
    return {"config": saved}


# ---------- settings: scraper credentials ----------
@app.get("/api/settings/credentials")
def get_credentials():
    from app.core.credentials import CRED_FIELDS, masked
    with SessionLocal() as db:
        return {"schema": CRED_FIELDS, "values": masked(db)}


@app.put("/api/settings/credentials")
def put_credentials(values: dict):
    from app.core.credentials import save_creds, masked
    with SessionLocal() as db:
        save_creds(db, values)
        return {"values": masked(db)}


@app.post("/api/settings/credentials/test")
def test_credentials(source: str = Query(..., pattern="^(ra|tgdb|igdb|ss|emumovies)$")):
    import httpx
    from app.core.credentials import cred
    try:
        if source == "ra":
            r = httpx.get(f"{settings.ra_api_base}/API_GetConsoleIDs.php",
                          params={"y": cred("ra_api_key"), "z": cred("ra_username")}, timeout=15)
            ok = r.status_code == 200 and isinstance(r.json(), list) and r.json()
            return {"ok": bool(ok), "detail": (f"{len(r.json())} consoles reachable" if ok else "auth failed")}
        if source == "tgdb":
            from app.services.scraper_tgdb import TgdbScraper
            t = TgdbScraper()
            if not t.configured:
                return {"ok": False, "detail": "no API key set"}
            hit = t.search("Sonic the Hedgehog", 18); t.close()
            return {"ok": bool(hit), "detail": ("API key valid" if hit else "no result / key rejected")}
        if source == "igdb":
            from app.services.scraper_igdb import IgdbScraper
            ig = IgdbScraper()
            if not ig.configured:
                return {"ok": False, "detail": "client id/secret not set"}
            ok = ig._auth(); ig.close()
            return {"ok": ok, "detail": ("Twitch token obtained" if ok else "auth failed")}
        if source == "ss":
            from app.services.scraper_ss import SsScraper
            s = SsScraper()
            if not s.configured:
                return {"ok": False, "detail": "dev creds not set"}
            # Validate DEV-level scrape capability (what actually matters), not the member
            # login: ssuserInfos.php strictly checks ssid/sspassword and 403s if the member
            # password differs from the dev password, even when scraping works fine.
            jeu = s.lookup(1, gameid=3)   # Sonic 1 (Megadrive) — a known game
            s.close()
            return {"ok": bool(jeu), "detail": ("dev credentials accepted (scrape OK)"
                                                if jeu else "rejected — check dev id/password")}
        if source == "emumovies":
            from app.services.scraper_emumovies import EmuMovies
            e = EmuMovies()
            if not e.configured:
                return {"ok": False, "detail": "user/pass not set"}
            ok = e.login(); e.close()
            return {"ok": ok, "detail": ("login OK" if ok else "login failed")}
    except Exception as ex:
        return {"ok": False, "detail": str(ex)[:150]}
    return {"ok": False, "detail": "unknown source"}


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")
