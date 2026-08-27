"""Shared scraper behaviour: auto-lock on scrape.

Model: a scrape sets only UNLOCKED fields (overwriting) and then LOCKS them; it skips
already-locked fields. Media likewise — a media type with a locked asset is skipped;
newly downloaded media is auto-locked. To let another source replace something, unlock it.
This makes "Scrape all" naturally touch only games with missing/unlocked content.
"""
from __future__ import annotations
import hashlib
from pathlib import Path
from app.db.models import MediaAsset


def apply_fields(game, values: dict) -> list[str]:
    """Set each provided field if UNLOCKED, then lock it. Returns the fields changed."""
    from app.core.categories import normalize_category
    locked = set(game.locked_fields or [])
    filled = []
    for k, v in values.items():
        if v is None or str(v).strip() == "":
            continue
        if k in locked:
            continue
        val = normalize_category(str(v)) if k == "category" else str(v)
        setattr(game, k, val)
        locked.add(k)
        filled.append(k)
    if filled:
        game.locked_fields = sorted(locked)
    return filled


def set_source(game, source: str, sid, name: str | None = None):
    game.source_ids = {**(game.source_ids or {}), source: sid}
    if name:
        game.source_names = {**(game.source_names or {}), source: name}


def save_media(db, game, media_type: str, source: str, url: str, media_store: Path,
               http, ext: str | None = None) -> bool:
    """Download + attach a media asset, auto-locked. Skips a type that already has a
    LOCKED asset, and skips a duplicate (same type+source)."""
    if any(m.media_type == media_type and m.is_locked for m in game.media):
        return False
    if any(m.media_type == media_type and m.source == source for m in game.media):
        return False
    try:
        r = http.get(url, timeout=60)
        r.raise_for_status()
    except Exception:
        return False
    md5 = hashlib.md5(r.content, usedforsecurity=False).hexdigest()
    # content dedup: don't store a file byte-identical to one this game already has
    if any(m.content_md5 == md5 for m in game.media):
        return False
    if not ext:
        ext = Path(url.split("?")[0]).suffix or ".jpg"
    rel = Path(str(game.console_id)) / str(game.id) / f"{media_type}-{source}{ext}"
    (media_store / rel).parent.mkdir(parents=True, exist_ok=True)
    (media_store / rel).write_bytes(r.content)
    have_primary = any(m.media_type == media_type and m.is_primary for m in game.media)
    db.add(MediaAsset(game_id=game.id, media_type=media_type, source=source,
                      path=str(rel), url=url, is_primary=not have_primary, is_locked=True,
                      content_md5=md5))
    return True
