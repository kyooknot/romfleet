"""RomFleet Postgres schema.

System · Game (RA anchor OR manual) · MediaAsset (typed media) · RomFile.
Release / Machine land in later phases.
"""
from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import String, Integer, BigInteger, Boolean, Text, DateTime, ForeignKey, JSON, UniqueConstraint, Float
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Setting(Base):
    """Key/value app settings (JSON values). e.g. key='scoring' -> priority profile."""
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


class System(Base):
    __tablename__ = "systems"
    id: Mapped[int] = mapped_column(primary_key=True)
    folder: Mapped[str] = mapped_column(String, unique=True, index=True)
    ra_console_id: Mapped[int] = mapped_column(Integer, index=True)
    ra_name: Mapped[str] = mapped_column(String)
    hash_method: Mapped[str] = mapped_column(String)
    # per-system overrides of the systems.yaml defaults (null gate = use yaml default):
    gate: Mapped[str | None] = mapped_column(String, nullable=True)   # ra|no-intro|redump|mame|lla
    selection: Mapped[str] = mapped_column(String, default="1g1r")    # 1g1r | fullset
    compress: Mapped[str] = mapped_column(String, default="none")     # none | rvz (GC/Wii) [| chd later]


class Game(Base):
    """A library game. Anchored on an RA game id, OR user-created (is_manual) for
    a ROM that matches no database. Descriptive metadata is editable in the UI;
    any field the user edits is added to `locked_fields` so re-scrape won't clobber it."""
    __tablename__ = "games"
    id: Mapped[int] = mapped_column(primary_key=True)
    ra_game_id: Mapped[int | None] = mapped_column(Integer, unique=True, index=True, nullable=True)
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    console_id: Mapped[int] = mapped_column(Integer, index=True)
    # which database anchors this game's identity: "ra" | "no-intro" | "redump" | "mame" | "lla".
    gate_source: Mapped[str] = mapped_column(String, default="ra", index=True)
    # non-RA anchor key: for DAT-gated games, the grouping key — normalized base title
    # (1G1R) or the specific version name (full-set). Unique per (console_id, gate_source).
    group_key: Mapped[str | None] = mapped_column(String, index=True, nullable=True)

    # --- descriptive metadata (scraped or hand-entered) ---
    title: Mapped[str] = mapped_column(String)
    # RA category (Homebrew/Hack/Unlicensed/Prototype/…) kept OUT of the title so the
    # exported title stays clean; shown as a badge, not sent to ES/Batocera.
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    sort_title: Mapped[str | None] = mapped_column(String, nullable=True)
    region: Mapped[str | None] = mapped_column(String, nullable=True)
    players: Mapped[str | None] = mapped_column(String, nullable=True)
    genre: Mapped[str | None] = mapped_column(String, nullable=True)
    developer: Mapped[str | None] = mapped_column(String, nullable=True)
    publisher: Mapped[str | None] = mapped_column(String, nullable=True)
    release_date: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    rating: Mapped[str | None] = mapped_column(String, nullable=True)

    # manual per-source id overrides — every scraper tries these BEFORE fuzzy name-match.
    # {"lbdb": 162727, "tgdb": 88168, "igdb": 1234, "ss": 5678, "emumovies": "Exact Title", "ra": 17687}
    source_ids: Mapped[dict] = mapped_column(JSON, default=dict)
    source_names: Mapped[dict] = mapped_column(JSON, default=dict)   # matched game name per source (confirmation)
    lbdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)   # legacy (migrated into source_ids)
    tgdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- RA-derived (read-only reference) ---
    num_achievements: Mapped[int] = mapped_column(Integer, default=0)
    points: Mapped[int] = mapped_column(Integer, default=0)
    preferred_hash: Mapped[str | None] = mapped_column(String, nullable=True)

    # --- user state, synced bidirectionally with frontends (central & fleet-wide) ---
    # A game favorited in ANY endpoint (LaunchBox, Batocera, …) is pulled here and
    # rendered <Favorite>true</Favorite> into EVERY endpoint, surviving re-pushes.
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # personal 0-5 star grade (user-set, synced fleet-wide like is_favorite). Renders to
    # LaunchBox <StarRatingFloat> and Batocera <rating> (÷5). community_rating is the
    # scraped crowd score (LB <CommunityStarRating>); it seeds display/Batocera when the
    # personal grade is unset, but the two stay distinct.
    star_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    community_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    # endpoints (agents) this game is hidden on — ["*"] = hidden on ALL agents, else a list
    # of endpoint names (e.g. hide mature games from the kids' machine only). Renders
    # <Hide>true> for a matching endpoint.
    hidden_on: Mapped[list] = mapped_column(JSON, default=list)

    # --- play state, synced bidirectionally with EVERY surface (LaunchBox, Batocera, Web UI) ---
    # play_count is the ACCUMULATED total across all surfaces. Each surface's last-observed
    # count lives in play_sync so a read->write cycle only ADDS genuinely-new plays and never
    # double-counts (delta = surface_current - play_sync[surface]; then play_sync[surface] is
    # re-baselined to the canonical total after we write it back out). last_played is the
    # fleet-wide max. playtime_minutes is RA-sourced ONLY — the agents' own playtime is ignored
    # and overwritten by RetroAchievements' authoritative UserTotalPlaytime.
    play_count: Mapped[int] = mapped_column(Integer, default=0)
    last_played: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    playtime_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)   # RA (minutes)
    play_sync: Mapped[dict] = mapped_column(JSON, default=dict)   # {"<endpoint>": 3, "batocera": 5}

    # field names the user has manually edited — scrape must not overwrite these
    locked_fields: Mapped[list] = mapped_column(JSON, default=list)
    metadata_source: Mapped[str | None] = mapped_column(String, nullable=True)  # lbdb|emumovies|tgdb|manual
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    media: Mapped[list["MediaAsset"]] = relationship(back_populates="game", cascade="all, delete-orphan")
    roms: Mapped[list["RomFile"]] = relationship(back_populates="game")

    EDITABLE = ("title", "category", "sort_title", "region", "players", "genre", "developer",
                "publisher", "release_date", "description", "rating")


class Event(Base):
    """Activity-feed entry for the Home page. kind = ra_added | rf_added | coverage."""
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    system_folder: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String)
    detail: Mapped[str | None] = mapped_column(String, nullable=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class Deployment(Base):
    """What each endpoint (agent) currently has deployed — one row per (endpoint, system),
    upserted on every push. Feeds the Agents status page."""
    __tablename__ = "deployments"
    id: Mapped[int] = mapped_column(primary_key=True)
    endpoint: Mapped[str] = mapped_column(String, index=True)
    system_folder: Mapped[str] = mapped_column(String, index=True)
    games: Mapped[int] = mapped_column(Integer, default=0)
    additional_apps: Mapped[int] = mapped_column(Integer, default=0)
    hidden: Mapped[int] = mapped_column(Integer, default=0)
    pushed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    __table_args__ = (UniqueConstraint("endpoint", "system_folder", name="uq_deploy"),)


class MediaAsset(Base):
    """Typed media for a game. `path` is relative to the canonical media store;
    `url` optionally records the remote source. One asset per type is is_primary."""
    __tablename__ = "media_assets"
    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    media_type: Mapped[str] = mapped_column(String, index=True)  # boxfront|boxback|titlescreen|screenshot|logo|video|manual
    source: Mapped[str] = mapped_column(String, default="manual")  # lbdb|emumovies|tgdb|manual|upload
    path: Mapped[str | None] = mapped_column(String, nullable=True)  # relative to MEDIA_STORE
    url: Mapped[str | None] = mapped_column(String, nullable=True)   # remote origin, if any
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False)  # protect from delete/replace
    content_md5: Mapped[str | None] = mapped_column(String, index=True, nullable=True)  # dedup identical files
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    game: Mapped["Game"] = relationship(back_populates="media")


class RomFile(Base):
    """A physical file on the source store. `match_status` records the RA gate."""
    __tablename__ = "rom_files"
    __table_args__ = (UniqueConstraint("path", name="uq_rom_path"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(String, index=True)
    filename: Mapped[str] = mapped_column(String)
    system_folder: Mapped[str] = mapped_column(String, index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    computed_hash: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    hash_method: Mapped[str] = mapped_column(String)
    match_status: Mapped[str] = mapped_column(String, index=True, default="pending")  # matched|unmatched|error
    ra_hash_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # which identity DBs this file's hash matched -> canonical name from each:
    # {"ra": "...", "lla": "...", "no-intro": "...", "redump": "..."}
    dat_matches: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    # ── provenance: where this file came from, so the library can be rebuilt by DOWNLOADING
    # rather than re-searching. The sourcer already resolves an exact URL for every download
    # and used to throw it away.
    #
    # source_url is the flat, exportable answer ("fetch this"). provenance carries the
    # structured detail a rebuild actually needs when the URL is dead or the file was DERIVED
    # rather than downloaded:
    #   {"kind": "disc|cart|torrent|cleancpc|itch|github|mariocube|patch|convert",
    #    "item": "<archive.org item>"            | "torrent": "<url>", "file_index": 2093,
    #    "member": "<path inside the archive>",
    #    "patch": {"url": ..., "base_md5": ..., "base_name": ...},   # hacks
    #    "converted_from": "iso", "verified": true, "at": "<iso8601>"}
    # A patched hack has no single URL — its provenance is a recipe. Keeping it structured
    # means that case is representable without another migration.
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)

    game_id: Mapped[int | None] = mapped_column(ForeignKey("games.id"), nullable=True)
    game: Mapped["Game | None"] = relationship(back_populates="roms")
