"""Populate Game.playtime_minutes (and last_played) from RetroAchievements — the
AUTHORITATIVE source for time played. The agents (LaunchBox/Batocera) also count playtime,
but per the sync design their value is IGNORED and OVERWRITTEN by RA's figure.

RA exposes per-game minutes only via API_GetGameInfoAndUserProgress -> `UserTotalPlaytime`
(verified live against the real account: e.g. 1942 -> 660 min, Arkanoid -> 2040 min). There is
NO bulk playtime endpoint, so refreshing every owned game would be one call per game. Instead we
refresh only the games the user has RECENTLY PLAYED (API_GetUserRecentlyPlayedGames) — the only
ones whose playtime/last-played can have changed since the last run — which bounds the per-game
calls to a small set. Wired into the nightly RA reconcile (dat_update._ra_full).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog

from app.db.models import Game
from app.services.favorites_sync import apply_last_played

log = structlog.get_logger()


def _parse_ra_dt(text: str | None) -> datetime | None:
    """RA's LastPlayed is 'YYYY-MM-DD HH:MM:SS' (UTC). Blank/None -> None."""
    if not text:
        return None
    try:
        return datetime.strptime(str(text).strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


async def sync_ra_playtime(db, max_recent: int = 300) -> dict:
    """Refresh playtime_minutes + last_played for library games the user has recently played.

    Returns a summary. Best-effort per game — a single failed RA call is logged and skipped,
    never aborting the batch.
    """
    from app.services.ra_client import ra_client

    # 1. page recently-played (c max 50) -> {ra_game_id: LastPlayed}
    last_by_gid: dict[int, str] = {}
    offset = 0
    while len(last_by_gid) < max_recent:
        try:
            batch = await ra_client.get_recently_played(count=50, offset=offset)
        except Exception as e:  # noqa
            log.warning("ra recently-played fetch failed", offset=offset, err=str(e)[:150])
            break
        if not batch:
            break
        for r in batch:
            gid = r.get("GameID")
            if gid:
                last_by_gid[int(gid)] = r.get("LastPlayed")
        if len(batch) < 50:
            break
        offset += 50
        await asyncio.sleep(0.3)

    if not last_by_gid:
        return {"recent": 0, "library_hits": 0, "playtime_updated": 0, "last_played_updated": 0}

    # 2. intersect with the owned library (games anchored on an RA id)
    games = db.query(Game).filter(Game.ra_game_id.in_(list(last_by_gid))).all()
    pt_updated = lp_updated = 0
    for g in games:
        try:
            prog = await ra_client.get_game_user_progress(g.ra_game_id)
        except Exception as e:  # noqa
            log.warning("ra progress fetch failed", ra_game_id=g.ra_game_id, err=str(e)[:150])
            continue
        secs = prog.get("UserTotalPlaytime")   # RA reports total playtime in SECONDS
        if secs is not None:
            try:
                mv = int(secs) // 60            # -> whole minutes for playtime_minutes
                if g.playtime_minutes != mv:
                    g.playtime_minutes = mv
                    pt_updated += 1
            except (TypeError, ValueError):
                pass
        if apply_last_played(g, _parse_ra_dt(last_by_gid.get(g.ra_game_id))):
            lp_updated += 1
        await asyncio.sleep(0.2)   # be polite to RA between per-game calls

    db.commit()
    result = {"recent": len(last_by_gid), "library_hits": len(games),
              "playtime_updated": pt_updated, "last_played_updated": lp_updated}
    log.info("sync_ra_playtime", **result)
    return result
