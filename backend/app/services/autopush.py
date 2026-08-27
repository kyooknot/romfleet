"""Debounced auto-push: a web edit marks its system(s) dirty; after a short quiet
window the changed system is pushed to every endpoint. Metadata/favorite/curation edits
take the XML-only fast path (~instant); media edits do a full media push.

Deferral: if an endpoint has LaunchBox/BigBox open, a push would have to kill it (LB
rewrites its Platform XML on exit) — so we DON'T interrupt the user; we keep the system
dirty and retry until the endpoint is idle. Runs on thread timers, so it works from both
sync and async FastAPI handlers. In-memory only — a restart drops pending edits (push
manually if needed); durability is a later refinement.
"""
from __future__ import annotations

import threading

import structlog

from app.db.session import SessionLocal
from app.db.models import RomFile

log = structlog.get_logger()

_DEBOUNCE = 3.0        # seconds of quiet after the last edit before pushing
_BUSY_RETRY = 90.0     # endpoint busy (LB open) -> retry this often
_ENABLED = True

_lock = threading.Lock()
_pending: dict[str, dict] = {}                 # system -> {"media": bool, "timer": Timer}
_push_locks: dict[str, threading.Lock] = {}    # serialize pushes per system


def set_enabled(on: bool) -> None:
    global _ENABLED
    _ENABLED = on


def mark_game_dirty(game_id: int, *, media: bool = False) -> None:
    """Resolve a game's system folder(s) and mark them dirty for auto-push."""
    if not _ENABLED:
        return
    db = SessionLocal()
    try:
        folders = [r[0] for r in db.query(RomFile.system_folder).filter(
            RomFile.game_id == game_id, RomFile.match_status == "matched",
            RomFile.system_folder.isnot(None)).distinct().all()]
    finally:
        db.close()
    for f in folders:
        mark_dirty(f, media=media)


def mark_dirty(system: str, *, media: bool = False) -> None:
    if not _ENABLED:
        return
    with _lock:
        st = _pending.get(system) or {"media": False, "timer": None}
        st["media"] = st["media"] or media
        if st["timer"]:
            st["timer"].cancel()
        t = threading.Timer(_DEBOUNCE, _fire, args=(system,))
        t.daemon = True
        st["timer"] = t
        _pending[system] = st
        t.start()
    log.info("autopush.mark_dirty", system=system, media=media)


def _reschedule(system: str, delay: float) -> None:
    with _lock:
        st = _pending.get(system)
        if not st:
            return
        if st["timer"]:
            st["timer"].cancel()
        t = threading.Timer(delay, _fire, args=(system,))
        t.daemon = True
        st["timer"] = t
        t.start()


def _fire(system: str) -> None:
    from app.services.push_launchbox import push_system, load_endpoints, endpoint_busy
    endpoints = list(load_endpoints())
    if not endpoints:
        with _lock:
            _pending.pop(system, None)
        return
    busy = [e for e in endpoints if endpoint_busy(e)]
    if busy:
        log.info("autopush.deferred_busy", system=system, busy=busy)
        _reschedule(system, _BUSY_RETRY)          # keep dirty, retry when idle
        return
    with _lock:
        st = _pending.pop(system, {})
    media = bool(st.get("media", False))
    lock = _push_locks.setdefault(system, threading.Lock())
    with lock:
        for ep in endpoints:
            try:
                r = push_system(system, ep, xml_only=not media)
                log.info("autopush.pushed", system=system, endpoint=ep,
                         xml_only=r.get("xml_only"), games=r.get("games"))
            except Exception as e:
                log.warning("autopush.failed", system=system, endpoint=ep, err=str(e))
