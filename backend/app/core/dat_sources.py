"""DAT-source auto-update config: per-source cadence + last-update tracking.

RA and LLA are pullable (RA API / GitHub); No-Intro and Redump are upload-only. Stored in
the `settings` table under key 'dat_sources'.
"""
from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy.orm.attributes import flag_modified
from app.db.models import Setting

_KEY = "dat_sources"
CADENCES = {"manual": None, "hourly": 3600, "daily": 86400, "weekly": 604800, "monthly": 2592000}

DEFAULTS = {
    "ra":       {"cadence": "hourly",  "last_update": None, "last_result": None},
    "lla":      {"cadence": "weekly",  "last_update": None, "last_result": None},
    "no-intro": {"cadence": "manual",  "last_update": None, "last_result": None},
    "redump":   {"cadence": "manual",  "last_update": None, "last_result": None},
}
_LABEL = {"ra": "RetroAchievements", "lla": "Lost Level Archive",
          "no-intro": "No-Intro", "redump": "Redump"}
PULLABLE = {"ra", "lla"}   # no-intro / redump have no open API — upload only


def get_config(db) -> dict:
    row = db.get(Setting, _KEY)
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    if row and row.value:
        for k, v in row.value.items():
            if k in cfg:
                cfg[k].update(v)
    return cfg


def _write(db, cfg: dict):
    row = db.get(Setting, _KEY)
    if not row:
        row = Setting(key=_KEY, value={})
        db.add(row)
    val = dict(row.value or {})   # preserve non-source keys (e.g. _auto_source)
    val.update(cfg)
    row.value = val
    flag_modified(row, "value")
    db.commit()


def save_config(db, values: dict):
    cfg = get_config(db)
    for k, v in (values or {}).items():
        if k in cfg and (v or {}).get("cadence") in CADENCES:
            cfg[k]["cadence"] = v["cadence"]
    _write(db, cfg)
    if "auto_source" in (values or {}):
        set_auto_source(db, bool(values["auto_source"]))
    if "discord_webhook" in (values or {}):
        set_discord_webhook(db, values["discord_webhook"])
    if "discord_notify" in (values or {}):
        set_notify_config(db, values["discord_notify"])


def _get_val(db, key, default=None):
    row = db.get(Setting, _KEY)
    return (row.value or {}).get(key, default) if row else default


def _set_val(db, key, value):
    row = db.get(Setting, _KEY)
    if not row:
        row = Setting(key=_KEY, value={})
        db.add(row)
    val = dict(row.value or {})
    val[key] = value
    row.value = val
    flag_modified(row, "value")
    db.commit()


# ── Discord notifications (webhook URL the user pastes from their channel) ──────
def get_discord_webhook(db) -> str:
    return _get_val(db, "_discord_webhook", "") or ""


def set_discord_webhook(db, url: str):
    _set_val(db, "_discord_webhook", (url or "").strip())


# ── Notification routing: which events go to which webhook ─────────────────────
#
# One webhook and an all-or-nothing switch was fine while there was one channel. This lets a
# deployment split traffic (e.g. loud sourcing activity in one channel, the weekly summary in
# another) and silence event classes it does not care about.
#
# ⚠️ `new_set` and `revision` each cover a PAIR of posts: the initial card and the ✅/❌ edit
# that lands on it later. They deliberately cannot be split or half-disabled — the result is a
# PATCH to the first message, so a different webhook cannot reach it and a disabled result
# would strand a "Sourcing…" card that never resolves.
#
# Back-compat: with no explicit config, every event is enabled and routed to the legacy
# `_discord_webhook`, so an existing install behaves exactly as before.

_NOTIFY_KEY = "_discord_notify"
DEFAULT_WEBHOOK = "default"

EVENT_KINDS = {
    "new_set":   "New RA set detected, and its sourced/failed result",
    "revision":  "Existing set revised, and its re-source result",
    "subset":    "New [Subset]/[Bonus] set — nothing to source",
    "untracked": "New set on a system RomFleet does not track",
    "delisted":  "RA removed or demoted a set",
    "roundup":   "Weekly coverage summary",
}


def get_notify_config(db) -> dict:
    """{'webhooks': {name: {'url','label'}}, 'events': {kind: {'enabled','webhook'}}}."""
    raw = _get_val(db, _NOTIFY_KEY, {}) or {}
    hooks = dict(raw.get("webhooks") or {})
    # The legacy single webhook IS the default one until a config says otherwise.
    legacy = get_discord_webhook(db)
    if DEFAULT_WEBHOOK not in hooks:
        hooks[DEFAULT_WEBHOOK] = {"url": legacy, "label": "Default"}
    elif not (hooks[DEFAULT_WEBHOOK].get("url") or "").strip():
        hooks[DEFAULT_WEBHOOK]["url"] = legacy
    events = {}
    for kind in EVENT_KINDS:
        e = dict((raw.get("events") or {}).get(kind) or {})
        events[kind] = {"enabled": bool(e.get("enabled", True)),
                        "webhook": e.get("webhook") or DEFAULT_WEBHOOK}
    return {"webhooks": hooks, "events": events}


def set_notify_config(db, cfg: dict):
    """Persist routing. Unknown event kinds and webhook refs that do not exist are dropped."""
    cfg = cfg or {}
    hooks = {}
    for name, h in (cfg.get("webhooks") or {}).items():
        name = str(name).strip()
        if not name:
            continue
        hooks[name] = {"url": (h or {}).get("url", "").strip(),
                       "label": (h or {}).get("label") or name}
    events = {}
    for kind, e in (cfg.get("events") or {}).items():
        if kind not in EVENT_KINDS:
            continue
        target = (e or {}).get("webhook") or DEFAULT_WEBHOOK
        if target not in hooks and target != DEFAULT_WEBHOOK:
            target = DEFAULT_WEBHOOK      # never route into a webhook that isn't defined
        events[kind] = {"enabled": bool((e or {}).get("enabled", True)), "webhook": target}
    _set_val(db, _NOTIFY_KEY, {"webhooks": hooks, "events": events})
    # Keep the legacy key in step so anything still reading it sees the default webhook.
    if DEFAULT_WEBHOOK in hooks:
        set_discord_webhook(db, hooks[DEFAULT_WEBHOOK]["url"])


def route_for(db, kind: str) -> str | None:
    """The webhook URL an event kind should post to, or None if it must not be sent.

    Returning None for an unknown kind would silently drop a newly-added notification, so an
    unrecognised kind falls back to the default webhook — a stray post is easier to notice and
    fix than a missing one.
    """
    cfg = get_notify_config(db)
    if kind not in EVENT_KINDS:
        return (cfg["webhooks"].get(DEFAULT_WEBHOOK) or {}).get("url") or None
    ev = cfg["events"][kind]
    if not ev["enabled"]:
        return None
    hook = cfg["webhooks"].get(ev["webhook"]) or cfg["webhooks"].get(DEFAULT_WEBHOOK) or {}
    return (hook.get("url") or "").strip() or None


# ── Discord cards for games that FAILED to source ───────────────────────────────
# The monitor posts "detected", then edits that message to ✅/❌. The message id only lived for
# the length of one tick, so when a game that failed was sourced later — by a sweep, a manual
# drop-off, or simply a retry that found a new upload — its red card stayed red forever.
# Remembering the id lets whoever sources it next go back and turn the card green.
def remember_failed_card(db, game_id, msg_id: str):
    if not msg_id:
        return
    cards = _get_val(db, "_discord_failed_cards", {}) or {}
    cards[str(game_id)] = msg_id
    _set_val(db, "_discord_failed_cards", cards)


def failed_card(db, game_id) -> str | None:
    return (_get_val(db, "_discord_failed_cards", {}) or {}).get(str(game_id))


def forget_failed_card(db, game_id):
    cards = _get_val(db, "_discord_failed_cards", {}) or {}
    if cards.pop(str(game_id), None) is not None:
        _set_val(db, "_discord_failed_cards", cards)


# The watermark only advances at the END of a tick, so anything that kills the tick after the
# subset FYIs are posted -- an exception, a restart, the ES session bug -- leaves it unmoved and
# the next tick announces the very same subsets again. That is how one Rune Factory subset
# posted repeatedly. The watermark stays as-is (it must, so genuinely unprocessed claims are
# retried); dedupe the announcement itself instead.
def subset_announced(db, game_id) -> bool:
    return str(game_id) in (_get_val(db, "_ra_announced_subsets", []) or [])


def mark_subset_announced(db, game_id):
    seen = _get_val(db, "_ra_announced_subsets", []) or []
    if str(game_id) not in seen:
        seen.append(str(game_id))
        _set_val(db, "_ra_announced_subsets", seen[-500:])   # bounded; old ids can't recur


# ── Hourly claims monitor watermark (newest DoneTime already processed) ─────────
def get_ra_claims_watermark(db) -> str:
    return _get_val(db, "_ra_claims_watermark", "") or ""


def set_ra_claims_watermark(db, done_time: str):
    _set_val(db, "_ra_claims_watermark", done_time or "")


# ── Nightly full RA reconcile: run once/day in an overnight UTC window ───────────
RA_FULL_HOUR_UTC = 7   # 03:00 EDT / 02:00 EST — overnight for the user (America/New_York)


def ra_full_due(db) -> bool:
    now = datetime.now(timezone.utc)
    if now.hour != RA_FULL_HOUR_UTC:
        return False
    return _get_val(db, "_ra_full_date") != now.date().isoformat()


def mark_ra_full(db):
    _set_val(db, "_ra_full_date", datetime.now(timezone.utc).date().isoformat())


def get_ra_max_per_tick(db) -> int:
    """Cap on RA GetGameList calls per tick — bounds the API rate regardless of system count."""
    row = db.get(Setting, _KEY)
    return int((row.value or {}).get("_ra_max_per_tick", 12)) if row else 12


def get_ra_checked(db) -> dict:
    row = db.get(Setting, _KEY)
    return dict((row.value or {}).get("_ra_checked", {})) if row else {}


def mark_ra_checked(db, console_id: int):
    row = db.get(Setting, _KEY)
    if not row:
        row = Setting(key=_KEY, value={})
        db.add(row)
    val = dict(row.value or {})
    checked = dict(val.get("_ra_checked", {}))
    checked[str(console_id)] = datetime.now(timezone.utc).isoformat()
    val["_ra_checked"] = checked
    row.value = val
    flag_modified(row, "value")
    db.commit()


def get_auto_source(db) -> bool:
    """Global: after an RA pull, auto-source newly-missing games from archives."""
    row = db.get(Setting, _KEY)
    return bool((row.value or {}).get("_auto_source", False)) if row else False


def set_auto_source(db, v: bool):
    row = db.get(Setting, _KEY)
    if not row:
        row = Setting(key=_KEY, value={})
        db.add(row)
    val = dict(row.value or {})
    val["_auto_source"] = bool(v)
    row.value = val
    flag_modified(row, "value")
    db.commit()


def record_update(db, source: str, result: dict):
    cfg = get_config(db)
    if source in cfg:
        cfg[source]["last_update"] = datetime.now(timezone.utc).isoformat()
        cfg[source]["last_result"] = result
        _write(db, cfg)


def due_sources(db) -> list[str]:
    """Pullable sources whose cadence has elapsed since last_update (the tick uses this)."""
    cfg = get_config(db)
    now = datetime.now(timezone.utc)
    out = []
    for src in PULLABLE:
        secs = CADENCES.get(cfg[src].get("cadence"))
        if secs is None:
            continue
        lu = cfg[src].get("last_update")
        if not lu:
            out.append(src)
            continue
        try:
            elapsed = (now - datetime.fromisoformat(lu)).total_seconds()
        except Exception:  # noqa
            elapsed = secs + 1
        # grace so an hourly cadence fires on every hourly tick despite a few seconds' drift
        # (last_update lands a couple seconds after :00, so a strict >= secs would skip a tick)
        if elapsed >= secs - min(secs * 0.1, 300):
            out.append(src)
    return out


def view(db) -> dict:
    cfg = get_config(db)
    hook = get_discord_webhook(db)
    return {"sources": [{"id": s, "label": _LABEL[s], "pullable": s in PULLABLE, **c}
                        for s, c in cfg.items()],
            "cadences": list(CADENCES), "auto_source": get_auto_source(db),
            "discord_webhook": hook, "discord_configured": bool(hook),
            "discord_notify": get_notify_config(db),
            "discord_event_kinds": EVENT_KINDS}
