"""Hourly RA new-release monitor.

One API call (GetClaims completed=1) yields the newest set claims across every console. We
diff against a stored watermark (newest DoneTime already processed), ingest each new game's
hashes into the ES gate, auto-source brand-new sets on tracked systems, and report to Discord.

Cost per hour: 1 call + a couple of GetGameExtended/GetGameHashes per genuinely-new set — vs
the old 12-consoles-per-tick sweep. The heavy all-console reconcile moved to the nightly job.
"""
from __future__ import annotations
from app.core import dat_sources
from app.core.config import settings
from app.db.models import System
from app.services.missing import _SUBSET
import structlog

log = structlog.get_logger()


def _clean(title: str) -> str:
    from app.services.ingest import clean_ra_title
    return clean_ra_title(title)[0]


def _is_subset(raw_title: str) -> bool:
    """A subset/bonus claim is achievements layered onto an EXISTING base ROM — RA ships no
    distinct dump for it, so there is nothing to source and an attempt can only ever report
    failure.

    RA writes these two ways and we accept both: the bracket form '[Subset - Multiplayer]'
    (which survives _clean(), and is all missing.py's _SUBSET matches), and the category-tag
    form '~Subset~ Foo' that clean_ra_title() lifts into a category. Bracket-only matching
    would let a tag-form subset through to the sourcer."""
    from app.services.ingest import clean_ra_title
    if _SUBSET.search(raw_title or ""):
        return True
    _, cat = clean_ra_title(raw_title or "")
    return (cat or "").strip().lower() in ("subset", "bonus")


async def _revision_still_owned(db, game_id) -> bool | None:
    """After a revision, does a ROM we already hold still match RA's (re-ingested) hash list?

    This is the whole point of surfacing a revision: RA can drop or replace accepted hashes, so
    a game that was green yesterday can quietly stop matching. Compare what we physically own
    against the hash set just written to the gate, not against our own stale match_status.
    Returns None when we cannot tell (game not in the catalog, or gate lookup failed).
    """
    from app.core.elasticsearch import get_es, init_es, close_es
    from app.services.sourcing import _ra_game
    from app.db.models import Game, RomFile
    try:
        g = db.query(Game).filter(Game.ra_game_id == game_id).first()
        if not g:
            return None
        owned = {(r.computed_hash or "").lower()
                 for r in db.query(RomFile).filter(RomFile.game_id == g.id).all()
                 if r.computed_hash}
        if not owned:
            return False
        await init_es()
        try:
            ra = await _ra_game(get_es(), game_id)
        finally:
            await close_es()
        md5s = {(h.get("md5") or "").lower() for h in (ra or {}).get("hashes", [])}
        return bool(owned & md5s) if md5s else None
    except Exception as e:  # noqa
        log.warning("revision ownership check failed", game_id=game_id, error=str(e)[:150])
        return None


async def poll(db) -> dict:
    from app.services.ra_client import ra_client
    from app.services.sync import upsert_ra_game
    from app.services.sourcing import source_missing
    from app.services import discord_notify
    from app.core.elasticsearch import get_es, init_es, close_es
    from app.core.console_meta import display_name
    from app.core.events import log_event

    claims = await ra_client.get_completed_claims()
    if not claims:
        return {"error": "no claims returned"}

    newest = max((c.get("DoneTime") or "") for c in claims)
    watermark = dat_sources.get_ra_claims_watermark(db)
    if not watermark:
        # First run: adopt the current newest as the baseline; do NOT source the 1000-item
        # backlog (that's the nightly full pull's job).
        dat_sources.set_ra_claims_watermark(db, newest)
        return {"initialized": True, "watermark": newest, "backlog": len(claims)}

    new = sorted([c for c in claims if (c.get("DoneTime") or "") > watermark],
                 key=lambda c: (c.get("DoneTime") or ""))
    # console id -> ALL systems on it (shared consoles: n64+n64dd, snes+satellaview+sufami,
    # pcengine+supergrafx, ngp+ngpc, wonderswan+wonderswancolor all share one RA console)
    # Build from the systems.yaml config (NOT db.query(System)): a configured-but-not-yet-
    # ingested system has its System DB row created lazily on first ingest, so relying on DB
    # rows falsely flags real systems (e.g. pcenginecd) as "untracked" until then.
    from app.core.systems import load_systems
    tracked: dict = {}
    for s in load_systems():
        tracked.setdefault(s.ra_console_id, []).append(s)

    summary = {"checked": len(claims), "new": len(new), "new_games": 0, "revisions": 0,
               "untracked": 0, "subsets": 0, "ingested": 0, "sourced": 0, "failed": 0,
               "revisions_resourced": 0}
    if not new:
        return summary

    # Partition: untracked new sets (FYI only) vs tracked (ingest), split into revisions/
    # subsets/new sets. NB SetType alone is not enough to decide "go source this": a subset
    # claim is SetType 0 (it IS a new set), it just has no dump of its own — hence the
    # separate title check, or every subset release reports a phantom sourcing failure.
    untracked_new, to_ingest, new_sets, subsets, revisions = [], [], [], [], []
    for c in new:
        srows = tracked.get(c.get("ConsoleID"))
        if not srows:
            if c.get("SetType", 0) == 0:
                untracked_new.append(c)
            continue
        to_ingest.append((c, srows[0]))
        if c.get("SetType", 0) != 0:
            summary["revisions"] += 1
            revisions.append((c, srows[0]))
        elif _is_subset(c.get("GameTitle") or ""):
            subsets.append((c, srows[0]))
            summary["subsets"] += 1
        else:
            new_sets.append((c, srows))

    # ── Phase 1: ingest every tracked game's hashes into the gate (one ES session) ──
    box_by_gid: dict[int, str] = {}
    await init_es()
    try:
        for c, _ in to_ingest:
            gid = c.get("GameID")
            try:
                info = await upsert_ra_game(get_es(), gid)
                if info:
                    summary["ingested"] += 1
                    box_by_gid[gid] = info.get("box_art") or ""
            except Exception as e:  # noqa
                log.warning("monitor ingest failed", game_id=gid, error=str(e)[:150])
        # make the just-indexed games searchable before source_missing queries them
        await get_es().indices.refresh(index=settings.es_index_games)
    finally:
        await close_es()

    # ── Phase 2: FYI for untracked new sets ──
    for c in untracked_new:
        summary["untracked"] += 1
        await discord_notify.notify_untracked(db, _clean(c.get("GameTitle") or ""),
                                              c.get("ConsoleName") or "",
                                              game_id=c.get("GameID"))

    # ── Phase 2b: FYI for subsets — ingested above (their hashes are the base ROM's), but
    # never sourced. Whether the base ROM is owned is the wanted list's job, not the monitor's.
    for c, srow in subsets:
        gid = c.get("GameID")
        cid = c.get("ConsoleID")
        if dat_sources.subset_announced(db, gid):
            continue          # already posted on an earlier tick; see mark_subset_announced
        title = _clean(c.get("GameTitle") or f"game {gid}")
        nm = display_name(cid, srow.ra_name)
        log_event(db, "ra_subset", f"New RA subset — {title} ({nm})",
                  system=srow.folder, data={"console": cid, "game_id": gid})
        await discord_notify.notify_subset(db, title, nm, box_by_gid.get(gid) or None,
                                           game_id=gid, console_id=cid)
        dat_sources.mark_subset_announced(db, gid)

    # ── Phase 2c: FYI for REVISIONS of sets we already track ──
    # A revision was previously counted in the summary and then discarded, so a change to an
    # existing set never reached the channel. It matters because a revision can change which
    # dumps RA accepts — a game we own can silently stop matching — so report whether a ROM we
    # hold still satisfies the (re-ingested) hash list.
    for c, srow in revisions:
        gid = c.get("GameID")
        cid = c.get("ConsoleID")
        title = _clean(c.get("GameTitle") or f"game {gid}")
        nm = display_name(cid, srow.ra_name)
        still = await _revision_still_owned(db, gid)
        box = box_by_gid.get(gid) or None
        log_event(db, "ra_revision", f"RA set revised — {title} ({nm})",
                  system=srow.folder, data={"console": cid, "game_id": gid, "owned": still})
        msg = await discord_notify.notify_revision(db, title, nm, box,
                                                   game_id=gid, still_owned=still)
        # A revision that invalidates the dump we hold is the one case where the library is
        # now WRONG rather than merely incomplete, so re-source it the same way a brand-new
        # set is sourced. Gated on the same _auto_source switch, because a revised disc title
        # can pull multiple GB.
        if still is not False or not dat_sources.get_auto_source(db):
            continue
        summary["revisions_resourced"] += 1
        ok, reason, routed = False, "", None
        try:
            for srow2 in (tracked.get(cid) or [srow]):      # shared console -> try each member
                r = await source_missing(srow2.folder, allow_search=True, ids=[gid])
                if r.get("sourced"):
                    ok, routed = True, srow2
                    break
                why = (r.get("failures") or {}).get(str(gid))
                if why:
                    reason, routed = why, srow2
                elif r.get("error") and not reason:
                    reason = r["error"]
        except Exception as e:  # noqa
            reason = f"re-source error: {str(e)[:120]}"
        summary["sourced" if ok else "failed"] += 1
        rnm = display_name(cid, (routed or srow).ra_name)
        await discord_notify.notify_revision_result(db, msg, title, rnm, box, ok, reason,
                                                    game_id=gid)
        if ok:
            dat_sources.forget_failed_card(db, gid)
        elif msg:
            dat_sources.remember_failed_card(db, gid, msg)

    # ── Phase 3: detect → auto-source → notify for each tracked NEW set ──
    for c, srows in new_sets:
        gid = c.get("GameID")
        cid = c.get("ConsoleID")
        summary["new_games"] += 1
        clean = _clean(c.get("GameTitle") or f"game {gid}")
        srow0 = srows[0]
        nm = display_name(cid, srow0.ra_name)
        box = box_by_gid.get(gid) or None
        log_event(db, "ra_added", f"New set added to RA — {clean} ({nm})",
                  system=srow0.folder, data={"console": cid, "game_id": gid})
        msg = await discord_notify.notify_new_set_detected(db, clean, nm, box, game_id=gid)
        ok, reason, hit_folder, wanted_any, routed = False, "", None, False, None
        try:
            # a shared-console set may belong to ANY sub-system — try each; the hash-library
            # split routes it to the right folder (e.g. a 64DD set -> n64dd not n64, and an
            # arcade set -> arcade not the first-listed neogeo).
            for srow in srows:
                r = await source_missing(srow.folder, allow_search=True, ids=[gid])
                if r.get("sourced"):
                    ok, hit_folder, routed = True, srow.folder, srow
                    break
                if r.get("wanted"):            # THIS sub-system's gate actually owns the set
                    # A hack/translation can never be sourced (RA hashes the PATCHED output),
                    # so prefer the patch-availability reason over the generic source miss.
                    why = (r.get("failures") or {}).get(str(gid))
                    wanted_any, routed = True, srow
                    reason = why or "no hash-matching source found"
                elif r.get("error") and not reason:
                    reason = r["error"]
            if not ok and not wanted_any and not reason:
                ok, reason = True, "already owned"   # not wanted by any sub-system's gate
        except Exception as e:  # noqa
            reason = f"sourcing error: {str(e)[:120]}"
        # Label with the sub-system the set actually ROUTED to (via the hash-library split),
        # not srows[0] — otherwise every RA-console-27 set reads as the first-listed system.
        disp = routed or srow0
        rnm = display_name(cid, disp.ra_name)
        summary["sourced" if ok else "failed"] += 1
        await discord_notify.notify_source_result(db, msg, clean, rnm, box, ok, reason,
                                                  game_id=gid)
        # Keep the message id of a FAILED card so a later success can turn it green. Games
        # that miss on the hour they are detected often turn up days later.
        from app.core import dat_sources as _ds
        if ok:
            _ds.forget_failed_card(db, gid)
        elif msg:
            _ds.remember_failed_card(db, gid, msg)
        log_event(db, "sourced" if ok else "source_failed",
                  f"{'Sourced' if ok else 'Could not source'} {clean} ({rnm})"
                  + (f" — {reason}" if reason and not ok else ""),
                  system=(hit_folder or disp.folder), data={"game_id": gid, "reason": reason})

    dat_sources.set_ra_claims_watermark(db, newest)
    return summary
