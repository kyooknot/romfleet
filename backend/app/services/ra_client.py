"""
RetroAchievements API client.

Handles all outbound calls to the RA API with rate-limit-aware
retry logic and caching via Redis.
"""

from __future__ import annotations
import asyncio
import httpx
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type
from app.core.config import settings
import structlog

log = structlog.get_logger()


class RAApiClient:
    BASE = settings.ra_api_base
    AUTH = {"y": settings.ra_api_key, "z": settings.ra_username}

    def __init__(self):
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self):
        await self._client.aclose()

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(5),
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
    )
    async def _get(self, endpoint: str, params: dict) -> dict | list:
        params.update(self.AUTH)
        resp = await self._client.get(f"{self.BASE}/{endpoint}", params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_console_ids(self) -> list[dict]:
        """All systems on RA."""
        data = await self._get("API_GetConsoleIDs.php", {"a": 1, "g": 1})
        return data if isinstance(data, list) else []

    async def get_game_list(self, console_id: int) -> list[dict]:
        """
        All games for a console, with hashes included.
        f=1 = only games with achievements
        h=1 = include hashes
        """
        data = await self._get("API_GetGameList.php", {
            "i": console_id,
            "f": 1,
            "h": 1,
        })
        return data if isinstance(data, list) else []

    async def get_game_extended(self, game_id: int) -> dict:
        """Per-game details, including ImageBoxArt — which the bulk
        API_GetGameList response does NOT carry. Used to enrich the hourly
        new-games Discord report with box art."""
        data = await self._get("API_GetGameExtended.php", {"i": game_id})
        return data if isinstance(data, dict) else {}

    async def get_game_hashes(self, game_id: int) -> list[dict]:
        """All hashes for a specific game (used for detailed hash info)."""
        data = await self._get("API_GetGameHashes.php", {"i": game_id})
        return data.get("Results", []) if isinstance(data, dict) else []

    async def get_recently_played(self, count: int = 50, offset: int = 0) -> list[dict]:
        """The user's most-recently-played games (newest first). Each row carries LastPlayed
        (a 'YYYY-MM-DD HH:MM:SS' timestamp) but NO playtime — playtime needs the per-game
        GetGameInfoAndUserProgress call. c max 50; page with offset. Used to bound which games
        we refresh playtime/last-played for (only ones actually played can have changed)."""
        data = await self._get("API_GetUserRecentlyPlayedGames.php",
                               {"u": settings.ra_username, "c": count, "o": offset})
        return data if isinstance(data, list) else []

    async def get_game_user_progress(self, game_id: int) -> dict:
        """Per-game progress for the user — INCLUDING UserTotalPlaytime (minutes), RA's
        authoritative time-played figure (verified live). Keyed by RA GameID (== Game.ra_game_id)."""
        data = await self._get("API_GetGameInfoAndUserProgress.php",
                               {"u": settings.ra_username, "g": game_id})
        return data if isinstance(data, dict) else {}

    async def get_completed_claims(self) -> list[dict]:
        """The ~1000 most-recently COMPLETED set claims across ALL consoles, newest-first —
        RA's machine-readable 'new releases' feed. One call replaces the per-console sweep for
        hourly monitoring. Each row: GameID, GameTitle, ConsoleID, ConsoleName, SetType
        (0=new set, 1=revision), DoneTime."""
        data = await self._get("API_GetClaims.php", {"k": 1})
        return data if isinstance(data, list) else []

    async def get_all_games_with_hashes(
        self,
        console_ids: list[int],
        progress_callback=None,
    ) -> list[dict]:
        """
        Fetch game + hash data for all requested consoles.
        Returns a flat list of game dicts each with a 'Hashes' key.
        """
        all_games = []
        for i, cid in enumerate(console_ids):
            try:
                games = await self.get_game_list(cid)
                all_games.extend(games)
                if progress_callback:
                    await progress_callback(i + 1, len(console_ids), cid)
                # Be polite to RA's servers
                await asyncio.sleep(0.5)
            except Exception as e:
                log.error("Failed to fetch game list", console_id=cid, error=str(e))
        return all_games


# Module-level singleton
ra_client = RAApiClient()
