"""Discord webhook notifications for the RA monitor.

The user pastes a channel webhook URL into settings. New-set alerts post a "detected — sourcing…"
embed immediately (so the channel isn't silent during multi-GB downloads), then EDIT that same
message in place to the ✅/❌ outcome. Everything degrades to a no-op when no webhook is set.
"""
from __future__ import annotations
import re
import httpx
from app.core import dat_sources
from app.core.config import settings
import structlog

log = structlog.get_logger()

BLURPLE = 0x5865F2
GREEN = 0x2ECC71
RED = 0xE74C3C
GREY = 0x95A5A6

# Webhook identity. A webhook has a name/avatar configured in Discord's UI, but username and
# avatar_url in the POST body override them per message — so this is ours to set in code, and
# the channel's webhook settings never need touching.
BOT_NAME = "RA Bot"
# NO per-message avatar_url. The avatar now lives on the WEBHOOK itself: app/web/ra-bot.png was
# PATCHed to the webhook as a base64 data URI, so Discord stores and serves it from its own CDN.
# Sending avatar_url here would override that good avatar on every post.
#
# Three attempts got here, and the failures are worth keeping:
#   ra-icon.webp      500x275 -- Discord centre-crops to a square before masking to a circle,
#                     so the wide lockup lost its outer thirds.
#   <our own /static/ra-bot.png>
#                     correctly padded, but that host is LAN-only. Discord fetches avatars
#                     from ITS servers, could not reach it, and fell back to the default
#                     Discord logo -- no error, just a silently wrong icon. Any self-hosted
#                     avatar URL has this problem unless the host is publicly reachable.
#   favicon.webp      public and square, but the R/A run edge-to-edge, so the circular mask
#                     clipped both letters.
# The padded 512x512 canvas (mark at ~66% width) is the only one that clears the circle, and
# uploading it to the webhook is what makes it reachable without hosting anything ourselves.
BOT_AVATAR = None
RA_GAME_URL = "https://retroachievements.org/game/{gid}"


def _boxart(path: str | None) -> str | None:
    if not path:
        return None
    if path.startswith("http"):
        return path
    return f"{settings.ra_media_base}{path}"


def _embed(title: str, desc: str, color: int, box: str | None = None,
           footer: str | None = None, url: str | None = None) -> dict:
    e: dict = {"title": title, "description": desc, "color": color}
    if url:                       # makes the embed title a link to the RA set page
        e["url"] = url
    thumb = _boxart(box)
    if thumb:
        e["thumbnail"] = {"url": thumb}
    if footer:
        e["footer"] = {"text": footer}
    return e


# RA's stand-in art. A subset entry has no art of its own, so RA serves these for its box /
# title / ingame slots -- which is what rendered as "No Screenshot Found" in the channel.
_RA_PLACEHOLDERS = ("/Images/000001.png", "/Images/000002.png")


async def _ra_images(game_id: int, fallback_game_id: int | None = None) -> str | None:
    """Best available RA art: box art, else title screen, else in-game shot, else icon.

    The monitor only fills box_by_gid for games it INGESTS, and subsets are deliberately never
    ingested (there is no distinct ROM to gate), so their alerts arrived with no image at all.
    Asking RA directly is only half the fix: a subset's own box/title/ingame are all the
    placeholder, so when they are we fall back to the BASE game's art, which is the picture
    the reader actually wants to see anyway.
    """
    from app.services.ra_client import ra_client
    exts = []
    for gid in [g for g in (game_id, fallback_game_id) if g]:
        try:
            exts.append(await ra_client.get_game_extended(gid))
        except Exception as e:  # noqa
            log.warning("ra image lookup failed", game_id=gid, error=str(e)[:150])
    # Kind-major, not game-major: the base game's BOX ART beats the subset's tiny ICON. Looping
    # game-major returned the subset icon (149143) and never reached the real cover art.
    for key in ("ImageBoxArt", "ImageTitle", "ImageIngame", "ImageIcon"):
        for ext in exts:
            v = (ext or {}).get(key)
            if v and str(v) not in _RA_PLACEHOLDERS:
                return v
    return None


_SUBSET_TAG = re.compile(r"\s*[\[(](?:Subset|Bonus)\s*-.*$", re.I)


def _base_title(title: str) -> str:
    """"Rune Factory: Tides of Destiny [Subset - Level 99 Skills]" -> the base game's title."""
    return _SUBSET_TAG.sub("", title or "").strip()


def _base_lookup(db, title: str, console_id: int) -> tuple[bool | None, int | None]:
    """(owned, base_ra_game_id) for the base game a subset layers onto.

    owned is True/False, or None when the base game isn't in the catalog at all — so the alert
    can say "unknown" rather than wrongly assert a missing ROM.
    """
    try:
        from app.db.models import Game, RomFile
        base = _base_title(title)
        g = (db.query(Game)
               .filter(Game.console_id == console_id, Game.title.ilike(base))
               .first())
        if not g:
            return None, None
        owned = db.query(RomFile).filter(RomFile.game_id == g.id,
                                         RomFile.match_status == "matched").count() > 0
        return owned, g.ra_game_id
    except Exception as e:  # noqa
        log.warning("base rom check failed", title=title, error=str(e)[:150])
        return None, None


async def _post(db, embed: dict, kind: str = "new_set") -> str | None:
    """Post an embed; return the Discord message id (needed to edit it later), or None.

    `kind` selects the route (see dat_sources.EVENT_KINDS). A disabled kind, or one whose
    webhook has no URL, returns None — the caller already treats None as "not posted".
    """
    url = dat_sources.route_for(db, kind)
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            payload = {"embeds": [embed], "username": BOT_NAME}
            if BOT_AVATAR:                    # normally None — see the note above
                payload["avatar_url"] = BOT_AVATAR
            r = await c.post(url, params={"wait": "true"}, json=payload)
            if r.status_code // 100 == 2:
                return r.json().get("id")
            log.warning("discord post failed", status=r.status_code, body=r.text[:200])
    except Exception as e:  # noqa
        log.warning("discord post error", error=str(e)[:200])
    return None


async def _edit(db, msg_id: str | None, embed: dict, kind: str = "new_set") -> bool:
    """Edit a card in place. MUST use the same kind as the post that created it — the message
    id only exists on that webhook, so editing through another one 404s."""
    url = dat_sources.route_for(db, kind)
    if not url or not msg_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.patch(f"{url}/messages/{msg_id}", json={"embeds": [embed]})
            if r.status_code // 100 == 2:
                return True
            # _post logs its non-2xx; this did not, so a failed edit was invisible -- the
            # caller silently fell back to posting a SECOND message and the original sat on
            # "Sourcing…" forever, reading as a stuck job. 429 (rate limit) is the usual cause
            # when a tick sources several sets at once.
            log.warning("discord edit failed", status=r.status_code,
                        retry_after=r.headers.get("retry-after"), body=r.text[:200])
            return False
    except Exception as e:  # noqa
        log.warning("discord edit error", error=str(e)[:200])
        return False


def _ra_link(game_id) -> str | None:
    return RA_GAME_URL.format(gid=game_id) if game_id else None


async def notify_new_set_detected(db, title: str, console: str, box: str | None,
                                  game_id=None) -> str | None:
    """🆕 A new tracked-system set appeared and sourcing is starting."""
    return await _post(db, _embed("🆕 New RA set detected", f"**{title}**\n{console}",
                                  BLURPLE, box, footer="Sourcing…", url=_ra_link(game_id)),
                       kind="new_set")


async def notify_source_result(db, msg_id: str | None, title: str, console: str,
                               box: str | None, ok: bool, reason: str = "", game_id=None) -> None:
    """Edit the detected message in place to the ✅/❌ outcome (posts fresh if edit unavailable)."""
    if ok:
        embed = _embed("✅ New RA set sourced", f"**{title}**\n{console}", GREEN, box,
                       footer="Sourced & verified — added to library", url=_ra_link(game_id))
    else:
        embed = _embed("❌ New RA set — sourcing failed", f"**{title}**\n{console}", RED, box,
                       footer=reason or "No hash-matching source found", url=_ra_link(game_id))
    if not await _edit(db, msg_id, embed, kind="new_set"):
        await _post(db, embed, kind="new_set")


async def notify_late_success(db, game_id, title: str, console: str, box: str | None) -> bool:
    """Turn a previously-red card green when the game is sourced later.

    A game that fails on the hour it is detected often arrives days later — a sweep retry, a
    new upload, or a manual drop-off. Without this its card stays "sourcing failed" forever
    and the channel disagrees with the library.
    """
    from app.core import dat_sources
    msg_id = dat_sources.failed_card(db, game_id)
    if not msg_id:
        return False
    embed = _embed("✅ New RA set sourced", f"**{title}**\n{console}", GREEN, box,
                   footer="Sourced & verified — added to library (after an earlier attempt failed)",
                   url=_ra_link(game_id))
    ok = await _edit(db, msg_id, embed, kind="new_set")
    if ok:
        dat_sources.forget_failed_card(db, game_id)
        log.info("discord card updated after late success", game_id=game_id, title=title)
    return ok


async def notify_untracked(db, title: str, console: str, game_id=None) -> None:
    """ℹ️ New set on a system not yet ingested into RomFleet — FYI only, no action."""
    await _post(db, _embed("ℹ️ New RA set (untracked system)", f"**{title}**\n{console}", GREY,
                           footer="Not tracked in RomFleet — no sourcing attempted",
                           url=_ra_link(game_id)), kind="untracked")


async def notify_revision(db, title: str, console: str, box: str | None = None,
                          game_id=None, still_owned: bool | None = None) -> str | None:
    """🔄 An EXISTING set was revised — RA changed the achievement set or its accepted hashes.

    Previously counted in the tick summary and then dropped on the floor, so a revision was
    invisible in the channel. It is worth surfacing precisely because a revision can change
    which dumps RA accepts: a set we owned yesterday can stop matching today, and that is the
    one case where "we already have it" quietly stops being true.
    """
    if not box and game_id:
        box = await _ra_images(game_id)
    if still_owned is True:
        footer = "Set revised — a matching ROM is still in the library"
    elif still_owned is False:
        footer = "Set revised — NO owned ROM matches the new hash list; re-source needed"
    else:
        footer = "Set revised — achievements and/or accepted hashes changed"
    return await _post(db, _embed("🔄 RA set revised", f"**{title}**\n{console}", BLURPLE, box,
                                  footer=footer, url=_ra_link(game_id)), kind="revision")


async def notify_revision_result(db, msg_id: str | None, title: str, console: str,
                                 box: str | None, ok: bool, reason: str = "",
                                 game_id=None) -> None:
    """Edit the revision card in place once an automatic re-source has run."""
    if ok:
        embed = _embed("✅ Revised set re-sourced", f"**{title}**\n{console}", GREEN, box,
                       footer="Revision changed the accepted hashes — a matching dump was "
                              "sourced & verified", url=_ra_link(game_id))
    else:
        embed = _embed("⚠️ Revised set — re-source failed", f"**{title}**\n{console}", RED, box,
                       footer=reason or "No hash-matching source found for the revised hash list",
                       url=_ra_link(game_id))
    if not await _edit(db, msg_id, embed, kind="revision"):
        await _post(db, embed, kind="revision")


async def notify_subset(db, title: str, console: str, box: str | None = None,
                        game_id: int | None = None, console_id: int | None = None) -> None:
    """ℹ️ A [Subset]/[Bonus] set — achievements layered onto a base ROM we may already own.
    There is no distinct ROM to acquire, so sourcing is never attempted (it could only ever
    report failure). The one thing worth reporting is whether the base ROM is actually in the
    library, because that is the difference between "ready to play" and "nothing you can do
    with this yet"."""
    owned, base_gid = (_base_lookup(db, title, console_id) if console_id is not None
                       else (None, None))
    if not box and game_id:
        box = await _ra_images(game_id, fallback_game_id=base_gid)
    if owned is True:
        footer = "Subset — base ROM owned, ready to play"
    elif owned is False:
        footer = f"Subset — base ROM MISSING: {_base_title(title)}"
    else:
        footer = "Subset — plays on the base ROM, nothing to source"
    await _post(db, _embed("ℹ️ New RA subset", f"**{title}**\n{console}", GREY, box,
                           footer=footer,
                           url=RA_GAME_URL.format(gid=game_id) if game_id else None),
                kind="subset")


async def notify_delisted(db, count: int, console: str) -> None:
    """⚠️ RA removed/demoted a set we track.

    Worth surfacing because both outcomes are silent: a ROM we own stays "matched" against a
    set that no longer exists, and a game we never sourced would otherwise sit in the wanted
    list forever as unobtainable. Nothing scanned for this before 2026-08-09.
    """
    await _post(db, _embed("⚠️ RA set delisted",
                           f"**{count} set(s) removed or demoted**\n{console}", RED,
                           footer="No longer in RA's game list — excluded from the wanted list"),
                kind="delisted")


async def send_test(db) -> bool:
    """Post a one-off test embed so the user can confirm the webhook works."""
    # Deliberately ignores the per-event toggles: this answers "does this webhook work?",
    # which must stay answerable even when every event class is switched off.
    url = dat_sources.get_discord_webhook(db)
    if not url:
        return False
    mid = await _post(db, _embed("🔔 RomFleet connected",
                                 "Notifications for new RA releases will appear here.", BLURPLE,
                                 footer="Test message"), kind="_test")
    return mid is not None


# ── Weekly roundup ────────────────────────────────────────────────────────────
#
# Posted Saturdays. The channel already gets a card per event as it happens, but those scroll
# away and none of them answer the only question worth asking at a glance: are we still at 100%?
# So the roundup LEADS with coverage and then recaps the week.
#
# Everything here is reconstructed from the `events` table (kinds ra_added / ra_subset /
# ra_revision / ra_delisted / sourced / source_failed) rather than a separate journal, so it
# stays correct even if a Discord post failed at the time.
MAX_LIST = 18                 # entries per section before "+N more" — keeps us under 4096 chars


def _coverage_line(db) -> tuple[str, int, int, float]:
    """'9,731 / 9,747 ROMs = 99.8% RA Coverage' from the coverage_snap the UI already keeps."""
    from app.db.models import Setting
    import json as _json
    row = db.query(Setting).filter(Setting.key == "coverage_snap").first()
    snap = {}
    if row is not None:
        snap = _json.loads(row.value) if isinstance(row.value, str) else (row.value or {})
    catalog = sum(int(v.get("catalog") or 0) for v in snap.values())
    owned = sum(int(v.get("owned") or 0) for v in snap.values())
    pct = (100.0 * owned / catalog) if catalog else 0.0
    return f"{owned:,} / {catalog:,} ROMs = {pct:.1f}% RA Coverage", owned, catalog, pct


def _game_meta(db, ra_game_id):
    """(title, console_id, achievements, points, owned) for an RA game id, from our catalog."""
    from app.db.models import Game, RomFile
    if not ra_game_id:
        return None
    g = db.query(Game).filter(Game.ra_game_id == ra_game_id).first()
    if not g:
        return None
    owned = db.query(RomFile).filter(RomFile.game_id == g.id,
                                     RomFile.match_status == "matched").count() > 0
    return g.title, g.console_id, (g.num_achievements or 0), (g.points or 0), owned


def _strip_prefix(title: str) -> str:
    """Event titles are sentences ('New set added to RA — Flooder (Nintendo Game Boy)')."""
    for sep in (" — ", " -- "):
        if sep in (title or ""):
            return title.split(sep, 1)[1].strip()
    return (title or "").strip()


def _line(db, gid, fallback_title: str, tag: str = "") -> str:
    meta = _game_meta(db, gid)
    name = (meta[0] if meta else None) or _strip_prefix(fallback_title) or "(untitled)"
    link = _ra_link(gid)
    label = f"[{name}]({link})" if link else f"**{name}**"
    bits = []
    if meta and meta[2]:
        bits.append(f"{meta[2]} achv")
    if meta and meta[3]:
        bits.append(f"{meta[3]} pts")
    if tag:
        bits.append(tag)
    return f"• {label}" + (f" · {' · '.join(bits)}" if bits else "")


def _section(lines: list[str]) -> str:
    if not lines:
        return "_nothing this week_"
    shown, extra = lines[:MAX_LIST], max(0, len(lines) - MAX_LIST)
    body = "\n".join(shown)
    if extra:
        body += f"\n_…and {extra} more_"
    return body[:4000]


async def build_weekly_roundup(db, days: int = 7) -> list[dict]:
    """Compose the roundup embeds. Separated from posting so it can be previewed/dry-run."""
    import datetime as _dt
    from app.db.models import Event
    from app.core.console_meta import display_name
    since = _dt.datetime.utcnow() - _dt.timedelta(days=days)
    evs = (db.query(Event).filter(Event.created_at >= since)
             .order_by(Event.created_at.asc()).all())

    def gid_of(e):
        return (e.data or {}).get("game_id")

    # Live sourcing truth, not what the card said at the time: a game that failed on Tuesday and
    # arrived Thursday must read as sourced here.
    # Two producers write ra_added: the hourly claims monitor logs ONE EVENT PER GAME (with a
    # game_id), while the nightly reconcile logs an AGGREGATE ("7 new game(s) added to RA —
    # Sony PlayStation 2", data={count, console}, no game_id). Rendering the aggregate through
    # the per-game formatter printed a bare console name as if it were a game.
    added, subsets, revisions = {}, {}, {}
    bulk = 0
    for e in evs:
        gid = gid_of(e)
        if e.kind == "ra_added" and gid is None:
            bulk += int((e.data or {}).get("count") or 0)
            continue
        if gid is None:
            continue
        if e.kind == "ra_added":
            meta = _game_meta(db, gid)
            owned = meta[4] if meta else False
            added[gid] = _line(db, gid, e.title, "✅ sourced" if owned else "❌ not sourced")
        elif e.kind == "ra_subset":
            meta = _game_meta(db, gid)
            base_ok = "base owned" if (meta and meta[4]) else "plays on base ROM"
            subsets[gid] = _line(db, gid, e.title, base_ok)
        elif e.kind == "ra_revision":
            # A set revised twice in a week is one entry, keyed by game — the channel already
            # got a card each time; the digest should not repeat the same title.
            meta = _game_meta(db, gid)
            still = (e.data or {}).get("owned")
            tag = ("still matches" if still or (meta and meta[4])
                   else "⚠️ no owned ROM matches — re-source")
            revisions[gid] = _line(db, gid, e.title, tag)
    added, subsets, revisions = (list(added.values()), list(subsets.values()),
                                 list(revisions.values()))

    # Delistings: the event carries only a count per console, so name the games from ES, where
    # ingest marks them delisted, and report whether OUR file is still on disk.
    removed = []
    consoles = {(e.data or {}).get("console") for e in evs if e.kind == "ra_delisted"}
    consoles.discard(None)
    if consoles:
        try:
            from app.core.elasticsearch import get_es, init_es, close_es
            from app.db.models import Game, RomFile, System
            await init_es()
            try:
                es = get_es()
                for cid in sorted(consoles):
                    r = await es.search(index=settings.es_index_games, size=40, query={
                        "bool": {"filter": [{"term": {"delisted": True}},
                                            {"term": {"console_id": cid}}]}})
                    for h in r.get("hits", {}).get("hits", []):
                        s = h.get("_source", {})
                        rgid = s.get("id") or s.get("ra_game_id")
                        g = db.query(Game).filter(Game.ra_game_id == rgid).first()
                        held = (db.query(RomFile).filter(RomFile.game_id == g.id).count()
                                if g else 0)
                        state = (f"our file kept ({held})" if held
                                 else "no file held")
                        nm = s.get("title") or (g.title if g else "?")
                        link = _ra_link(rgid)
                        srow = db.query(System).filter_by(ra_console_id=cid).first()
                        cname = display_name(cid, srow.ra_name if srow else "") or f"console {cid}"
                        removed.append(f"• {f'[{nm}]({link})' if link else nm} · "
                                       f"{cname} · {state}")
            finally:
                await close_es()
        except Exception as e:  # noqa
            log.warning("roundup delist lookup failed", error=str(e)[:150])

    head, owned, catalog, pct = _coverage_line(db)
    colour = GREEN if pct >= 99.95 else (BLURPLE if pct >= 99 else RED)
    end = _dt.datetime.utcnow()
    span = f"{(end - _dt.timedelta(days=days)):%b %d} – {end:%b %d, %Y}"

    embeds = [_embed(f"🏆 {head}", f"**Weekly RA roundup** · {span}\n\n"
                     f"🆕 {len(added) + bulk} new · 🧩 {len(subsets)} subsets · "
                     f"🔄 {len(revisions)} revisions · ⚠️ {len(removed)} removed",
                     colour, footer="RomFleet weekly summary")]

    if bulk:
        # Say so rather than silently under-reporting: the nightly reconcile knows only a count.
        added = added + [f"_+{bulk} more added by the nightly reconcile (no per-game detail)_"]
    for emoji, name, lines in (("🆕", "New on RA", added),
                               ("🧩", "New subsets", subsets),
                               ("🔄", "Revisions", revisions),
                               ("⚠️", "Removed from RA", removed)):
        if not lines:
            continue
        embeds.append(_embed(f"{emoji} {name} ({len(lines)})", _section(lines),
                             GREY if emoji in ("🧩",) else
                             (RED if emoji == "⚠️" else BLURPLE)))
    return embeds


def _roundup_stamp(db, write: bool = False):
    """Read (or set) the last-posted timestamp, kept in the dat_sources blob."""
    import datetime as _dt, json as _json
    from app.db.models import Setting
    from sqlalchemy.orm.attributes import flag_modified
    row = db.query(Setting).filter(Setting.key == "dat_sources").first()
    if row is None:
        return None
    blob = _json.loads(row.value) if isinstance(row.value, str) else (row.value or {})
    if write:
        blob["_last_roundup"] = _dt.datetime.utcnow().isoformat(timespec="seconds")
        row.value = _json.dumps(blob) if isinstance(row.value, str) else blob
        flag_modified(row, "value")
        db.commit()
        return blob["_last_roundup"]
    raw = blob.get("_last_roundup")
    try:
        return _dt.datetime.fromisoformat(raw) if raw else None
    except Exception:  # noqa
        return None


async def post_weekly_roundup(db, days: int = 7, force: bool = False) -> dict:
    """Post the roundup. Discord caps a message at 10 embeds / 6000 chars, and we send ≤5.

    Guarded against double-posting: the Saturday timer and a manual/CLI run would otherwise
    both fire on the same day and put two identical digests in the channel.
    """
    import datetime as _dt
    last = _roundup_stamp(db)
    if last and not force:
        age_h = (_dt.datetime.utcnow() - last).total_seconds() / 3600
        if age_h < 72:
            return {"posted": False, "reason": f"already posted {age_h:.1f}h ago",
                    "last": last.isoformat(timespec="seconds")}
    url = dat_sources.route_for(db, "roundup")
    embeds = await build_weekly_roundup(db, days=days)
    if not url:
        return {"posted": False, "reason": "roundup disabled or no webhook configured",
                "embeds": len(embeds)}
    # Boxart for the lead card: the first new set of the week, title-screen fallback included.
    try:
        import datetime as _dt
        from app.db.models import Event
        since = _dt.datetime.utcnow() - _dt.timedelta(days=days)
        first = (db.query(Event).filter(Event.kind == "ra_added", Event.created_at >= since)
                   .order_by(Event.created_at.asc()).first())
        gid = (first.data or {}).get("game_id") if first else None
        if gid:
            art = await _ra_images(gid)
            if art:
                embeds[0]["thumbnail"] = {"url": _boxart(art)}
    except Exception as e:  # noqa
        log.warning("roundup art failed", error=str(e)[:120])
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(url, params={"wait": "true"},
                             json={"embeds": embeds, "username": BOT_NAME})
            ok = r.status_code // 100 == 2
            if not ok:
                log.warning("roundup post failed", status=r.status_code, body=r.text[:200])
            else:
                _roundup_stamp(db, write=True)
            return {"posted": ok, "status": r.status_code, "embeds": len(embeds)}
    except Exception as e:  # noqa
        log.warning("roundup post error", error=str(e)[:200])
        return {"posted": False, "error": str(e)[:200], "embeds": len(embeds)}
