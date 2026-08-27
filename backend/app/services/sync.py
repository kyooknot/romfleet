"""
RA Sync Service.

Pulls game + hash data from RA API and writes it into the ra_games
Elasticsearch index. On each sync, any game that receives new hashes
has its preferred_hash recomputed.
"""

from __future__ import annotations
from datetime import datetime, timezone
from elasticsearch import AsyncElasticsearch
from app.core.config import settings
from app.core.hash_priority import parse_and_score, select_preferred
from app.core.systems import load_systems
from app.services.ra_client import ra_client
import structlog

log = structlog.get_logger()


def _normalise_date(value: str | None) -> str | None:
    """
    RA returns dates as "2023-11-01 20:00:31" (space separator).
    Elasticsearch date type requires ISO 8601 "2023-11-01T20:00:31".
    """
    if not value:
        return None
    return value.replace(" ", "T")


async def sync_ra_games(es: AsyncElasticsearch, job_id: str | None = None) -> dict:
    """
    Full sync: fetch ALL RA consoles (from RA API directly), write to ES.
    Uses RA's console list as the source of truth, not systems.yaml.
    systems.yaml is only used for local ROM scanning — not for what we sync.
    Returns a summary dict with counts.
    """
    # Get all consoles from RA directly — a=1 (active), g=1 (has games)
    try:
        ra_consoles = await ra_client.get_console_ids()
        console_ids = [c["ID"] for c in ra_consoles if isinstance(c.get("ID"), int)]
        log.info("Fetched RA console list", count=len(console_ids))
    except Exception as e:
        log.warning("Could not fetch RA console list, falling back to systems.yaml", error=str(e))
        systems = load_systems()
        console_ids = list({s.ra_console_id for s in systems})

    log.info("Starting RA sync", console_count=len(console_ids))

    new_games = 0
    updated_games = 0
    errors = 0
    new_hashes_by_game: list[int] = []
    new_game_ids: list[int] = []

    for console_id in console_ids:
        try:
            games = await ra_client.get_game_list(console_id)
            for game in games:
                result = await _upsert_game(es, game, console_id)
                if result == "created":
                    new_games += 1
                    gid = game.get("ID") or game.get("GameID")
                    if gid:
                        new_game_ids.append(gid)
                elif result == "updated_hashes":
                    updated_games += 1
                    new_hashes_by_game.append(game.get("ID"))
        except Exception as e:
            log.error("Sync failed for console", console_id=console_id, error=str(e))
            errors += 1

    summary = {
        "new_games": new_games,
        "updated_games": updated_games,
        "errors": errors,
        "new_hash_game_ids": new_hashes_by_game,
        "new_game_ids": new_game_ids,
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }
    # Patch-url enrichment must run on the weekly path too — it previously only
    # existed in the manual dashboard sync, so automation never enriched.
    try:
        summary["patch_enrichment"] = await enrich_missing_patch_urls(es)
    except Exception as ex:
        log.warning("Patch enrichment failed", error=str(ex))
    log.info("RA sync complete", **summary)
    return summary


async def _upsert_game(es: AsyncElasticsearch, game: dict, console_id: int) -> str:
    """
    Insert or update a single game document.
    Returns 'created' | 'updated_hashes' | 'unchanged'.
    """
    game_id = game.get("ID") or game.get("GameID")
    if not game_id:
        return "unchanged"

    raw_hashes: list[str] = game.get("Hashes", [])

    parsed_hashes = [
        parse_and_score(md5=h.lower(), name="", labels=[], patch_url=None)
        for h in raw_hashes
    ]
    preferred = select_preferred(parsed_hashes)

    doc = {
        "game_id":          game_id,
        "title":            game.get("Title", ""),
        "console_id":       console_id,
        "console_name":     game.get("ConsoleName", ""),
        "num_achievements": game.get("NumAchievements", 0),
        "num_leaderboards": game.get("NumLeaderboards", 0),
        "points":           game.get("Points", 0),
        "image_icon":       game.get("ImageIcon", ""),
        "forum_topic_id":   game.get("ForumTopicID"),
        "date_modified":    _normalise_date(game.get("DateModified")),
        "last_synced":      datetime.now(timezone.utc).isoformat(),
        "preferred_hash":   preferred.md5 if preferred else None,
        "hashes": [
            {
                "md5":                  ph.md5,
                "name":                 ph.raw_name,
                "label":                ph.label,
                "patch_url":            ph.patch_url,
                "regions":              ph.regions,
                "revision":             ph.revision,
                "is_translation":       ph.is_translation,
                "is_english_translation": ph.is_english_translation,
                "translation_lang":     ph.translation_lang,
                "translation_version":  ph.translation_version,
                "priority_score":       ph.priority_score,
            }
            for ph in parsed_hashes
        ],
    }

    try:
        existing = await es.get(index=settings.es_index_games, id=str(game_id))
        existing_by_md5 = {h["md5"]: h for h in existing["_source"].get("hashes", [])}
        new_hash_set = {ph.md5 for ph in parsed_hashes}
        if new_hash_set != set(existing_by_md5):
            # GetGameList carries bare md5s; name/label/patch_url exist only via
            # GetGameHashes enrichment. Re-parse retained hashes with their stored
            # enrichment so a hash-list change doesn't silently wipe it.
            merged = [
                parse_and_score(
                    md5=ph.md5,
                    name=existing_by_md5.get(ph.md5, {}).get("name") or "",
                    labels=[existing_by_md5[ph.md5]["label"]]
                        if existing_by_md5.get(ph.md5, {}).get("label") else [],
                    patch_url=existing_by_md5.get(ph.md5, {}).get("patch_url"),
                )
                for ph in parsed_hashes
            ]
            preferred = select_preferred(merged)
            doc["preferred_hash"] = preferred.md5 if preferred else None
            doc["hashes"] = [
                {
                    "md5":                  ph.md5,
                    "name":                 ph.raw_name,
                    "label":                ph.label,
                    "patch_url":            ph.patch_url,
                    "regions":              ph.regions,
                    "revision":             ph.revision,
                    "is_translation":       ph.is_translation,
                    "translation_version":  ph.translation_version,
                    "priority_score":       ph.priority_score,
                }
                for ph in merged
            ]
            await es.index(index=settings.es_index_games, id=str(game_id), document=doc)
            return "updated_hashes"
        return "unchanged"
    except Exception:
        await es.index(index=settings.es_index_games, id=str(game_id), document=doc)
        return "created"


RAPATCHES_TREE_URL = "https://api.github.com/repos/RetroAchievements/RAPatches/git/trees/main?recursive=1"


async def get_rapatches_game_ids() -> set[int]:
    """
    Every game id with a patch in the RAPatches GitHub repo — the authoritative
    candidate list for patch_url enrichment. One git-trees API call returns the
    full tree; patch zips are named '<gameid>-Name.zip' under <Console>/... .
    (Selecting candidates by an existing 'rapatches' hash label cannot work:
    labels themselves only appear after enrichment.)
    """
    import urllib.request as _ur, json as _json, re as _re
    req = _ur.Request(RAPATCHES_TREE_URL, headers={"User-Agent": "RAROManager/1.0"})
    with _ur.urlopen(req, timeout=30) as r:  # nosec B310 — GitHub API
        data = _json.loads(r.read())
    ids: set[int] = set()
    for entry in data.get("tree", []):
        path = entry.get("path", "")
        # Skip Removed/ and Subset patches — subsets apply automatically in
        # the emulator and are excluded from scans, stats, and patching.
        if path.endswith(".zip") and not path.startswith("Removed/") \
                and "/Subset/" not in f"/{path}":
            m = _re.match(r"(\d+)-", path.rsplit("/", 1)[-1])
            if m:
                ids.add(int(m.group(1)))
    return ids


async def enrich_missing_patch_urls(es: AsyncElasticsearch) -> dict:
    """
    Enrich (via GetGameHashes) every synced game that has a RAPatches entry but
    no patch_url stored yet. Cheap at steady state: only newly patched games and
    games whose enrichment was lost need calls. Returns {candidates, enriched}.
    """
    import asyncio as _asyncio
    patch_ids = await get_rapatches_game_ids()
    ids = [str(g) for g in patch_ids]
    need: list[int] = []
    for i in range(0, len(ids), 500):
        resp = await es.search(index=settings.es_index_games, body={
            "query": {"ids": {"values": ids[i:i + 500]}},
            "size": 500, "_source": ["game_id", "hashes.patch_url"],
        })
        for hit in resp["hits"]["hits"]:
            if not any(h.get("patch_url") for h in hit["_source"].get("hashes", [])):
                need.append(hit["_source"]["game_id"])
    enriched = 0
    for gid in need:
        try:
            await enrich_game_hashes(es, gid)
            enriched += 1
        except Exception as ex:
            log.warning("Patch enrichment failed for game", game_id=gid, error=str(ex))
        await _asyncio.sleep(0.15)  # RA API rate limit
    log.info("Patch URL enrichment", candidates=len(patch_ids), needed=len(need), enriched=enriched)
    return {"candidates": len(patch_ids), "needed": len(need), "enriched": enriched}


async def enrich_missing_hash_names(es: AsyncElasticsearch, limit: int = 1500) -> dict:
    """
    Enrich (via GetGameHashes) every synced game whose hashes are all NAMELESS.

    _upsert_game stores hashes with name="" and relies on a separate per-game enrich pass to
    fill them in, but that pass only runs for games flagged CHANGED by an incremental sync — so
    anything ingested outside that path keeps empty names indefinitely. That silently breaks
    sourcing: _candidates matches an archive's filenames against the RA hash NAMES, and with no
    names it can only fall back on the RA display title. Whenever the dump is titled differently
    from the game, the game becomes unfindable no matter how many sources are registered — e.g.
    RA "Marble Saga: Kororinpa" wants the dump "Marbles! Balance Challenge (Europe)
    (En,Fr,De,Es,It)", and RA "Wall-E" wants "Disney-Pixar WALL-E (USA) (En,Fr,Es) (Rev 2)".

    A one-off backfill cleared 4,392 such games (40% of the catalog, ~100% of PS2/PSX/NDS/
    Arcade/PSP/Wii/Saturn/Dreamcast); `limit` keeps the nightly steady-state pass bounded, since
    at steady state only newly-synced games should ever show up here.
    """
    import asyncio as _asyncio
    need: list[int] = []
    after = None
    while len(need) < limit:
        body = {"size": 1000, "query": {"match_all": {}}, "sort": [{"game_id": "asc"}],
                "_source": ["game_id", "hashes.name"]}
        if after:
            body["search_after"] = after
        resp = await es.search(index=settings.es_index_games, body=body)
        hits = resp["hits"]["hits"]
        if not hits:
            break
        for hit in hits:
            src = hit["_source"]
            hs = src.get("hashes") or []
            if hs and not any((h.get("name") or "").strip() for h in hs):
                need.append(src["game_id"])
        after = hits[-1]["sort"]
    need = need[:limit]
    enriched = 0
    for gid in need:
        try:
            await enrich_game_hashes(es, gid)
            enriched += 1
        except Exception as ex:
            log.warning("Hash-name enrichment failed for game", game_id=gid, error=str(ex))
        await _asyncio.sleep(0.15)  # RA API rate limit
    if enriched:
        await es.indices.refresh(index=settings.es_index_games)
    log.info("Hash name enrichment", needed=len(need), enriched=enriched)
    return {"needed": len(need), "enriched": enriched, "capped": len(need) >= limit}


async def upsert_ra_game(es: AsyncElasticsearch, game_id: int) -> dict | None:
    """Create-or-replace ONE game in the ES gate, fully enriched — used by the hourly claims
    monitor when a brand-new set appears (cheaper and more targeted than re-syncing its whole
    console). GetGameExtended supplies metadata + box art; GetGameHashes supplies named hashes.
    Returns {game_id, title, console_id, num_hashes, box_art} or None if the game can't be read."""
    ext = await ra_client.get_game_extended(game_id)
    if not ext or not (ext.get("ID") or ext.get("GameID")):
        return None
    console_id = ext.get("ConsoleID")
    raw = await ra_client.get_game_hashes(game_id)
    parsed = [
        parse_and_score(md5=h.get("MD5", "").lower(), name=h.get("Name", ""),
                        labels=h.get("Labels", []), patch_url=h.get("PatchUrl"))
        for h in raw if h.get("MD5")
    ]
    preferred = select_preferred(parsed)
    doc = {
        "game_id":          game_id,
        "title":            ext.get("Title", ""),
        "console_id":       console_id,
        "console_name":     ext.get("ConsoleName", ""),
        "num_achievements": ext.get("NumAchievements", 0),
        "num_leaderboards": ext.get("NumLeaderboards", 0),
        "points":           ext.get("Points", 0),
        "image_icon":       ext.get("ImageIcon", ""),
        "forum_topic_id":   ext.get("ForumTopicID"),
        "date_modified":    _normalise_date(ext.get("Updated") or ext.get("DateModified")),
        "last_synced":      datetime.now(timezone.utc).isoformat(),
        "preferred_hash":   preferred.md5 if preferred else None,
        "hashes": [
            {
                "md5":                  ph.md5,
                "name":                 ph.raw_name,
                "label":                ph.label,
                "patch_url":            ph.patch_url,
                "regions":              ph.regions,
                "revision":             ph.revision,
                "is_translation":       ph.is_translation,
                "translation_version":  ph.translation_version,
                "priority_score":       ph.priority_score,
            }
            for ph in parsed
        ],
    }
    await es.index(index=settings.es_index_games, id=str(game_id), document=doc)
    box = ext.get("ImageBoxArt") or ext.get("ImageIcon") or ""
    return {"game_id": game_id, "title": ext.get("Title", ""), "console_id": console_id,
            "num_hashes": len(parsed), "box_art": box}


async def enrich_game_hashes(es: AsyncElasticsearch, game_id: int):
    """
    Fetch full hash detail (name, labels) from GetGameHashes and update ES.
    Called lazily or as a background task after initial sync.
    """
    raw = await ra_client.get_game_hashes(game_id)
    if not raw:
        return

    parsed_hashes = [
        parse_and_score(
            md5=h.get("MD5", "").lower(),
            name=h.get("Name", ""),
            labels=h.get("Labels", []),
            patch_url=h.get("PatchUrl"),
        )
        for h in raw
    ]
    preferred = select_preferred(parsed_hashes)

    await es.update(
        index=settings.es_index_games,
        id=str(game_id),
        doc={
            "preferred_hash": preferred.md5 if preferred else None,
            "hashes": [
                {
                    "md5":                  ph.md5,
                    "name":                 ph.raw_name,
                    "label":                ph.label,
                    "patch_url":            ph.patch_url,
                    "regions":              ph.regions,
                    "revision":             ph.revision,
                    "is_translation":       ph.is_translation,
                    "translation_version":  ph.translation_version,
                    "priority_score":       ph.priority_score,
                }
                for ph in parsed_hashes
            ],
        },
    )
