"""Activity feed + coverage tracking for the Home page.

Events: ra_added (RA catalog grew), rf_added (ROMs entered RomFleet), coverage (a system's
or the overall RA-coverage % changed). Coverage % = owned RA games / RA catalog (subset-
excluded, from the missing report). Deltas are detected against a stored snapshot.
"""
from __future__ import annotations
from sqlalchemy.orm.attributes import flag_modified
from app.db.models import Event, System, Setting

_SNAP = "coverage_snap"


def log_event(db, kind: str, title: str, detail: str | None = None,
              system: str | None = None, data: dict | None = None):
    db.add(Event(kind=kind, title=title, detail=detail, system_folder=system, data=data or {}))
    db.commit()


async def system_coverage(folder: str) -> dict:
    from app.services.missing import missing_for_system
    m = await missing_for_system(folder)
    cat, own = m.get("catalog", 0), m.get("owned", 0)
    return {"catalog": cat, "owned": own, "pct": round(own / cat * 100, 1) if cat else 100.0}


def _get_snap(db) -> dict:
    r = db.get(Setting, _SNAP)
    return dict(r.value) if r and r.value else {}


def _save_snap(db, snap: dict):
    r = db.get(Setting, _SNAP)
    if not r:
        r = Setting(key=_SNAP, value={})
        db.add(r)
    r.value = snap
    flag_modified(r, "value")
    db.commit()


async def refresh_coverage(db) -> dict:
    """Recompute per-system + overall RA coverage; log an event for any % that changed."""
    from app.core.console_meta import display_name
    snap = _get_snap(db)
    new, t_own, t_cat = {}, 0, 0
    for s in db.query(System).all():
        c = await system_coverage(s.folder)
        new[s.folder] = c
        t_own += c["owned"]
        t_cat += c["catalog"]
        old = snap.get(s.folder)
        if old and old.get("pct") != c["pct"]:
            nm = display_name(s.ra_console_id, s.ra_name)
            arrow = "▲" if c["pct"] > old["pct"] else "▼"
            log_event(db, "coverage", f"{nm} coverage {old['pct']}% {arrow} {c['pct']}%",
                      f"{c['owned']}/{c['catalog']} owned", s.folder,
                      {"from": old["pct"], "to": c["pct"]})
    overall = round(t_own / t_cat * 100, 1) if t_cat else 100.0
    oo = (snap.get("_overall") or {}).get("pct")
    if oo is not None and oo != overall:
        arrow = "▲" if overall > oo else "▼"
        log_event(db, "coverage", f"RetroAchievements coverage {oo}% {arrow} {overall}%",
                  f"{t_own}/{t_cat} across all systems", None,
                  {"from": oo, "to": overall, "overall": True})
    new["_overall"] = {"pct": overall, "owned": t_own, "catalog": t_cat}
    _save_snap(db, new)
    return new
