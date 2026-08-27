from __future__ import annotations
from pathlib import Path
from typing import ClassVar
from pydantic_settings import BaseSettings, SettingsConfigDict

# repo root = .../romfleet  (this file is backend/app/core/config.py)
REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # --- RetroAchievements (gate + metadata + playtime) ---
    ra_username: str = ""
    ra_api_key: str = ""
    ra_api_base: str = "https://retroachievements.org/API"
    ra_media_base: str = "https://media.retroachievements.org"

    # --- Elasticsearch (RA hash-match index) ---
    es_url: str = "http://127.0.0.1:9200"
    es_index_games: str = "ra_games"
    es_index_roms: str = "local_roms"

    # --- Postgres (authoritative store) ---
    database_url: str = "postgresql+psycopg2://romfleet:romfleet@127.0.0.1:5432/romfleet"

    # --- Redis / Celery ---
    redis_url: str = "redis://127.0.0.1:6379/0"

    # --- Paths ---
    # Ingest SOURCE (read-only bind mount of the existing collection)
    src_roms_path: str = "/mnt/src/roms"
    # RomFleet canonical library (read-write, built one system at a time)
    canonical_path: str = "/mnt/romfleet"
    # config dir holding systems.yaml (defaults to repo config/)
    config_path: str = str(REPO_ROOT / "config")

    # LaunchBox GamesDB (scraper spine) — local SQLite metadata db (no creds needed).
    # Downloaded separately; see docs/INSTALL.md. Default is install-prefix-independent.
    lbdb_path: str = "/var/lib/romfleet/LaunchBox.Metadata.db"

    # vendored DATs (identification/naming; optionally membership gate)
    dats_path: str = str(REPO_ROOT / "dats")

    # TheGamesDB (supplementary metadata + media; REST API needs the UUID key)
    tgdb_api_key: str = ""

    # EmuMovies (supplementary video snaps; reverse-engineered API)
    emu_user: str = ""
    emu_pass: str = ""

    # IGDB (metadata + media; Twitch OAuth app)
    igdb_client_id: str = ""
    igdb_client_secret: str = ""

    # ScreenScraper (metadata + media; needs a registered dev softname)
    ss_dev_id: str = ""
    ss_dev_password: str = ""
    ss_softname: str = "RomFleet"
    ss_user: str = ""
    ss_pass: str = ""

    log_level: str = "INFO"

    # Which entry in config/endpoints.yaml commands assume when none is given.
    # Kept out of the code so the repo carries no particular machine's name.
    default_endpoint: str = "desktop"

    # ── Readiness ─────────────────────────────────────────────────────────────
    # Every field above defaults to something, so the app STARTS regardless. That
    # is deliberate (a scraper without credentials should degrade, not block the
    # server) but it used to mean a completely unconfigured install looked healthy
    # while matching nothing at all — the RA gate silently had no key, so no ROM
    # could ever match and the library just stayed empty.
    # ClassVar, not a field: pydantic rejects a bare class attribute on a settings
    # model ("A non-annotated attribute was detected"), which fails at IMPORT time —
    # and config.py is imported by everything, so it takes the whole app down.
    REQUIRED: ClassVar[tuple[tuple[str, str], ...]] = (
        ("ra_username", "RA_USERNAME"), ("ra_api_key", "RA_API_KEY"))

    def missing_required(self) -> list[str]:
        """Env var names that must be set for the core RA gate to work."""
        return [env for attr, env in self.REQUIRED if not (getattr(self, attr, "") or "").strip()]


settings = Settings()

# Say so loudly at import time. Without this the first sign of trouble is an
# ingest run that matches zero games and reports no error.
if settings.missing_required():
    import warnings
    warnings.warn(
        "RomFleet is not fully configured: "
        + ", ".join(settings.missing_required())
        + " is unset. The RetroAchievements gate cannot match anything, so ingest "
          "will find zero games and report no error. See docs/INSTALL.md.",
        RuntimeWarning, stacklevel=2)
