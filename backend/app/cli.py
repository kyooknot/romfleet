"""RomFleet Phase-1 CLI.

  python -m app.cli init-db
  python -m app.cli sync-ra --console 1        # pull Genesis games+hashes into ES
  python -m app.cli ingest --system megadrive  # scan -> RA-hash -> gate -> Postgres
"""
from __future__ import annotations
import asyncio
import typer
from app.core.elasticsearch import init_es, close_es, get_es
from app.db.session import init_db
from app.services.ingest import sync_console_to_es, ingest_system, enrich_console

cli = typer.Typer(add_completion=False, help="RomFleet Phase-1 ingest CLI")


@cli.command("init-db")
def init_db_cmd():
    """Create the Postgres tables."""
    init_db()
    typer.echo("DB tables created.")


@cli.command("sync-ra")
def sync_ra_cmd(console: int = typer.Option(..., help="RA console id (Genesis=1)")):
    """Pull an RA console's games + hashes into the ra_games ES index (the gate source)."""
    async def _run():
        await init_es()
        try:
            return await sync_console_to_es(get_es(), console)
        finally:
            await close_es()
    r = asyncio.run(_run())
    typer.echo(r)


@cli.command("enrich-ra")
def enrich_ra_cmd(console: int = typer.Option(..., help="RA console id"),
                  limit: int = typer.Option(0, help="0 = all games")):
    """Pull full hash names/labels (region/revision) for a console's games into ES."""
    async def _run():
        await init_es()
        try:
            return await enrich_console(get_es(), console, limit or None)
        finally:
            await close_es()
    typer.echo(asyncio.run(_run()))


@cli.command("build-lbdb")
def build_lbdb_cmd(zip: str = typer.Option("", help="LaunchBox Metadata.zip path (default: alongside LBDB_PATH)")):
    """Build the LBDB SQLite index from LaunchBox's public Metadata.zip (self-sourced)."""
    from app.services.lbdb_build import build_lbdb_db
    from app.core.config import settings
    typer.echo(build_lbdb_db(zip, settings.lbdb_path))


@cli.command("scrape-lbdb")
def scrape_lbdb_cmd(system: str = typer.Option(..., help="Batocera folder, e.g. megadrive"),
                    limit: int = typer.Option(0, help="0 = all games"),
                    platform: str = typer.Option("", help="override LBDB platform name")):
    """Scrape metadata + US media for a system's games from the LaunchBox GamesDB."""
    from app.services.scraper_lbdb import scrape_system
    r = scrape_system(system, limit or None, platform or None)
    typer.echo(r)


@cli.command("lbdb-platforms")
def lbdb_platforms_cmd(like: str = typer.Option(..., help="substring to search LBDB platform names")):
    """List LBDB platform names matching a substring (to pin the console mapping)."""
    from app.services.scraper_lbdb import LbdbScraper
    s = LbdbScraper()
    typer.echo(s.list_platforms(like))
    s.close()


@cli.command("lock-existing")
def lock_existing_cmd():
    """One-time: lock every non-empty metadata field + all media across all games
    (applies the 'scraped = locked' rule to data scraped before auto-lock)."""
    from app.db.session import SessionLocal
    from app.db.models import Game, MediaAsset
    db = SessionLocal()
    ng = 0
    for g in db.query(Game).all():
        locked = set(g.locked_fields or [])
        before = set(locked)
        for f in Game.EDITABLE:
            if getattr(g, f):
                locked.add(f)
        if locked != before:
            g.locked_fields = sorted(locked)
            ng += 1
    nm = db.query(MediaAsset).filter(MediaAsset.is_locked.is_(False)).update({"is_locked": True})
    db.commit(); db.close()
    typer.echo({"games_field_locked": ng, "media_locked": nm})


@cli.command("dedupe-media")
def dedupe_media_cmd():
    """Collapse byte-identical media per game (e.g. 8 identical boxarts -> 1). Backfills
    content_md5, keeps the primary/locked/oldest of each duplicate group, deletes the rest."""
    import hashlib
    from pathlib import Path
    from app.core.config import settings
    from app.db.session import SessionLocal
    from app.db.models import MediaAsset
    store = Path(settings.canonical_path) / "media"
    db = SessionLocal()
    assets = db.query(MediaAsset).all()
    # backfill hashes
    for m in assets:
        if not m.content_md5 and m.path and (store / m.path).exists():
            m.content_md5 = hashlib.md5((store / m.path).read_bytes(), usedforsecurity=False).hexdigest()
    db.flush()
    # group by (game_id, content_md5); keep best, delete rest
    groups: dict = {}
    for m in assets:
        if m.content_md5:
            groups.setdefault((m.game_id, m.content_md5), []).append(m)
    removed = 0
    for (gid, md5), items in groups.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda m: (not m.is_primary, not m.is_locked, m.id))  # keep primary>locked>oldest
        for m in items[1:]:
            if m.path:
                (store / m.path).unlink(missing_ok=True)
            db.delete(m)
            removed += 1
    db.commit(); db.close()
    typer.echo({"duplicate_media_removed": removed})


@cli.command("render-launchbox")
def render_launchbox_cmd(
    system: str = typer.Option(..., help="Batocera folder, e.g. megadrive (0 = all systems)"),
    rom_base: str = typer.Option(..., help=r"Windows-side roms root for this LaunchBox instance, e.g. Z:\roms or \\NAS\share\roms"),
    out: str = typer.Option(..., help="Output dir for the LaunchBox export bundle (Data/, Images/, Videos/, Manuals/)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute the diff/media plan without writing anything"),
):
    """Render the curated canonical library to a LaunchBox Platform XML + media bundle."""
    from app.services.render_launchbox import render_platform, render_all

    if system == "0":
        results = render_all(rom_base, out, dry_run)
    else:
        results = [render_platform(system, rom_base, out, dry_run)]

    for r in results:
        typer.echo(f"\n== {r.system} -> {r.platform} ==")
        typer.echo(f"  games: {r.games}  multi_disc: {r.multi_disc}  additional_apps: {r.additional_apps}")
        typer.echo(f"  skipped_no_rom: {r.skipped_no_rom}  media_copied: {r.media_copied}  "
                   f"missing_boxfront: {r.media_missing_primary}")
        xr = r.xml_result
        typer.echo(f"  xml: {xr.get('path')}  changed={xr.get('changed')}  "
                   f"{'(dry-run)' if dry_run else 'written=' + str(xr.get('written'))}")
        if dry_run and xr.get("diff"):
            typer.echo(xr["diff"])
        for m in r.media_results:
            if m.get("error"):
                typer.echo(f"  media MISSING: {m['media_type']} for {m['game']} ({m['src']})")


@cli.command("render-gamelist")
def render_gamelist_cmd(
    system: str = typer.Option(..., help="Batocera folder, e.g. n64"),
    out: str = typer.Option("", help="Output dir (default: <canonical_path>/roms/<system> — "
                             "the live Batocera share; pass a scratch dir to review first)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute the diff/media plan without writing anything"),
    no_media: bool = typer.Option(False, "--no-media", help="Skip copying media (XML only)"),
):
    """Render the curated canonical library to a Batocera/EmulationStation gamelist.xml.

    Competition / multi-file sets (SimpleFlips comps, Zelda MM parts, Mario Party Legacy
    vols, multi-disc games, MSU-1, Satellaview broadcasts) render as one <game> entry PER
    kept file, named "<Set title> — <entry label>" — nothing is hidden behind an
    AdditionalApplication-style attachment (Batocera has no such concept).
    """
    from app.services.render_gamelist import render_system

    r = render_system(system, out or None, dry_run, media=not no_media)
    typer.echo(f"\n== {r.system} ==")
    typer.echo(f"  entries: {r.entries}  standalone: {r.standalone}  "
               f"competition_sets: {r.competition_sets}  competition_entries: {r.competition_entries}")
    typer.echo(f"  hidden: {r.hidden}  skipped_not_published: {r.skipped_not_published}  "
               f"media_copied: {r.media_copied}  missing_boxfront: {r.media_missing_primary}")
    xr = r.xml_result
    typer.echo(f"  xml: {xr.get('path')}  changed={xr.get('changed')}  "
               f"{'(dry-run)' if dry_run else 'written=' + str(xr.get('written'))}")
    if dry_run and xr.get("diff"):
        typer.echo(xr["diff"])
    for m in r.media_results:
        if m.get("error"):
            typer.echo(f"  media MISSING: {m['media_type']} for {m['game']} ({m['src']})")


@cli.command("push-launchbox")
def push_launchbox_cmd(
    system: str = typer.Option(..., help="Batocera folder to deploy, e.g. n64"),
    endpoint: str = typer.Option(settings.default_endpoint, help="target endpoint (config/endpoints.yaml)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="render + tar but don't transfer"),
    xml_only: bool = typer.Option(False, "--xml-only", help="skip media; deploy only the Platform XML (fast path)"),
):
    """Render one system and push just that platform to a LaunchBox endpoint over SSH."""
    from app.services.push_launchbox import push_system
    r = push_system(system, endpoint, dry_run=dry_run, xml_only=xml_only)
    typer.echo(f"\n== push {r['system']} -> {r['platform']} @ {r['endpoint']} ==")
    typer.echo(f"  games: {r['games']}  additional_apps: {r['additional_apps']}  "
               f"media_copied: {r['media_copied']}")
    typer.echo(f"  archive: {r['tarball_bytes']:,} bytes  members: {r['members']}")
    typer.echo(f"  {'(dry-run, not transferred)' if r.get('dry_run') else 'deployed: ' + r.get('remote_stdout','')}")


@cli.command("process-dropoff")
def process_dropoff_cmd(dropoff: str = typer.Option("", help="dropoff dir (default <canonical>/dropoff)")):
    """Identify manually-dropped ROMs by hash, file them into their canonical system, re-ingest."""
    from app.services.dropoff import process_dropoff
    from app.services.ingest import ingest_system
    async def _run():
        init_db(); await init_es()
        try:
            r = await process_dropoff(dropoff or None)
            for s in r.get("systems", []):
                await ingest_system(get_es(), s)
            return r
        finally:
            await close_es()
    r = asyncio.run(_run())
    for x in r.get("results", []):
        typer.echo(f"  {x}")
    typer.echo(f"systems re-ingested: {r.get('systems')}")


@cli.command("process-patch-dropoff")
def process_patch_dropoff_cmd(dropoff: str = typer.Option("", help="dropoff dir (default <canonical>/dropoff)")):
    """Build owned hacks from dropped patches (IPS/BPS/UPS/xdelta): identify the RA hack, find/
    source the base, apply, verify against the RA hash, file, re-ingest."""
    from app.services.patch_dropoff import process_patch_dropoff
    from app.services.ingest import ingest_system
    async def _run():
        init_db(); await init_es()
        try:
            r = await process_patch_dropoff(dropoff or None)
            for s in r.get("systems", []):
                await ingest_system(get_es(), s)
            return r
        finally:
            await close_es()
    r = asyncio.run(_run())
    for x in r.get("results", []):
        typer.echo(f"  {x}")
    typer.echo(f"systems re-ingested: {r.get('systems')}")


@cli.command("sync-favorites")
def sync_favorites_cmd(
    endpoint: str = typer.Option(settings.default_endpoint, help="endpoint to pull Favorite flags from"),
    system: str = typer.Option("", help="limit to one system (default: all managed platforms)"),
    mirror: bool = typer.Option(False, "--mirror", help="full reconcile: also un-favorite games not favorited on the endpoint"),
):
    """Pull Favorite flags from an endpoint's LaunchBox into RomFleet's central store."""
    from app.services.favorites_sync import sync_favorites
    r = sync_favorites(endpoint, [system] if system else None, mirror=mirror)
    typer.echo(r)


@cli.command("ingest")
def ingest_cmd(system: str = typer.Option(..., help="Batocera folder, e.g. megadrive"),
               limit: int = typer.Option(0, help="0 = all files")):
    """Scan a system's source ROMs, RA-hash, gate against ES, persist to Postgres."""
    async def _run():
        init_db()
        await init_es()
        try:
            return await ingest_system(get_es(), system, limit or None)
        finally:
            await close_es()
    r = asyncio.run(_run())
    typer.echo(f"\n== ingest {system} ==")
    for k in ("scanned", "matched", "unmatched", "errors"):
        typer.echo(f"  {k:10} {r[k]}")
    typer.echo("  samples:")
    for s in r.get("samples", []):
        typer.echo(f"    {s}")


if __name__ == "__main__":
    cli()
