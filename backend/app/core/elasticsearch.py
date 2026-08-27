import asyncio

from elasticsearch import AsyncElasticsearch
from app.core.config import settings

# Module-level client — reused across requests
es: AsyncElasticsearch = None

# init_es/close_es are called by ~11 background tasks as if they owned the client, but it is a
# single shared global. Unguarded, init_es built a SECOND client (leaking the first) and
# close_es shut down whichever one was current -- so two overlapping tasks killed each other's
# session. Sourcing psx and wii at the same time produced exactly that: wii finished, called
# close_es, and psx died mid-run with "RuntimeError: Session is closed" and a 500.
# Refcount instead: first in creates, last out closes.
_refs = 0
_lock = asyncio.Lock()


def get_es() -> AsyncElasticsearch:
    return es


async def init_es():
    """Create the ES client (once) and ensure indices exist. Safe to call concurrently."""
    global es, _refs
    async with _lock:
        if es is None:
            es = AsyncElasticsearch(settings.es_url)
            await _ensure_indices()
        _refs += 1


async def close_es():
    """Release one reference; the client closes only when the last holder is done."""
    global es, _refs
    async with _lock:
        _refs = max(0, _refs - 1)
        if _refs == 0 and es is not None:
            await es.close()
            es = None


async def _ensure_indices():
    """Create indices with mappings if they don't already exist."""

    # ── ra_games index ────────────────────────────────────────────────────────
    games_mapping = {
        "mappings": {
            "properties": {
                "game_id":          {"type": "integer"},
                "title":            {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                "console_id":       {"type": "integer"},
                "console_name":     {"type": "keyword"},
                "num_achievements": {"type": "integer"},
                "num_leaderboards": {"type": "integer"},
                "points":           {"type": "integer"},
                "image_icon":       {"type": "keyword", "index": False},
                "forum_topic_id":   {"type": "integer"},
                "date_modified":    {"type": "date"},
                "last_synced":      {"type": "date"},
                "preferred_hash":   {"type": "keyword"},
                "hashes": {
                    "type": "nested",
                    "properties": {
                        "md5":                     {"type": "keyword"},
                        "name":                    {"type": "text"},
                        "label":                   {"type": "keyword"},
                        "patch_url":               {"type": "keyword", "index": False},
                        "regions":                 {"type": "keyword"},
                        "revision":                {"type": "integer"},
                        "is_translation":          {"type": "boolean"},
                        "is_english_translation":  {"type": "boolean"},
                        "translation_lang":        {"type": "keyword"},
                        "translation_version":     {"type": "keyword"},
                        "priority_score":          {"type": "integer"},
                    },
                },
            }
        }
    }

    # ── local_roms index ──────────────────────────────────────────────────────
    roms_mapping = {
        "mappings": {
            "properties": {
                "filename":           {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                "filepath":           {"type": "keyword"},
                "system_folder":      {"type": "keyword"},
                "console_id":         {"type": "integer"},
                "console_name":       {"type": "keyword"},
                "computed_hash":      {"type": "keyword"},
                "hash_method":        {"type": "keyword"},
                "matched_game_id":    {"type": "integer"},
                "matched_game_title": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                "match_status":       {"type": "keyword"},   # matched | unmatched | error | pending
                "file_size_bytes":    {"type": "long"},
                "last_scanned":       {"type": "date"},
                "error_message":      {"type": "text"},
            }
        }
    }

    for index, body in [
        (settings.es_index_games, games_mapping),
        (settings.es_index_roms,  roms_mapping),
    ]:
        exists = await es.indices.exists(index=index)
        if not exists:
            # elasticsearch-py 8.x: pass mappings= kwarg, not body=
            await es.indices.create(index=index, mappings=body["mappings"])
