"""Batocera / EmulationStation `gamelist.xml` renderer.

Turns the curated canonical library (Game + selected RomFile + MediaAsset rows) into a
per-system `gamelist.xml` dropped straight into `/mnt/romfleet/roms/<folder>/` — the
same directory a Batocera device already mounts and reads its ROMs from (unlike
LaunchBox, there is no separate "export bundle": the system folder itself *is* the
Batocera library root, so `<path>`/`<image>`/`<video>` are all relative to it).

Schema (the standard EmulationStation/Batocera `gamelist.xml`, stable across RetroPie/
ES-DE/Batocera forks for years — no plugin, no live device needed to confirm it):

    <gameList>
      <game>
        <path>./Some Game (USA).zip</path>
        <name>Some Game</name>
        <desc>...</desc>
        <image>./images/Some Game-image.png</image>
        <video>./videos/Some Game-video.mp4</video>
        <marquee>./images/Some Game-marquee.png</marquee>
        <rating>0.8</rating>                       <!-- 0.0-1.0, NOT 0-5 stars -->
        <releasedate>19980101T000000</releasedate>  <!-- ES's own datetime format -->
        <developer>...</developer>
        <publisher>...</publisher>
        <genre>...</genre>
        <players>1-2</players>
        <favorite>true</favorite>
        <hidden>true</hidden>
      </game>
    </gameList>

Selection (WHICH RomFiles belong in the library) is delegated entirely to
`organize.build_plan()` — the SAME 1G1R/full-set decision that governs what gets
published into `canonical_path/roms/<folder>/`. This renderer does not re-decide
"which version is the keeper". Naming (`<path>`, the on-disk existence check, the
media-file basename) is NOT re-derived from build_plan's canonical-name computation,
though — it comes straight from the kept RomFile's own `.filename`, exactly like
render_launchbox's `ApplicationPath`. A published RomFile's name can legitimately
diverge from the DB's current RA/DAT "canonical" name (e.g. hack-competition entries
published under a short curated name rather than the RA hash-name) without that being
stale or wrong — `build_plan` is consulted only for membership, never for spelling.
The renderer still skips (and counts/logs) any kept RomFile whose file has gone
missing from disk entirely, so a `<game>` Batocera can't launch is never emitted.

Competition / multi-file sets (the point of this module): Batocera has no
AdditionalApplication equivalent, so every kept file in a game_id group becomes its
OWN `<game>` entry — nothing is hidden. Entries are named "<Set title> — <entry
label>", reusing render_launchbox's `_extra_name` labeling (Disc N / Part N / Vol N /
bare competition-entry name minus shared prefix) so naming is consistent across both
frontends. A Batocera `<folder>` grouping element exists but maps to a REAL on-disk
subdirectory (`<folder>./Some Set/Some Entry.zip` requires the rom to physically live
in `roms/<system>/Some Set/`) — moving hundreds of already-canonical files into
subfolders is a bigger, riskier change than this task calls for, so flat
prefixed-name entries are the default here; see the render report for the folder
option write-up.
"""
from __future__ import annotations

import re
import shutil
import xml.sax.saxutils as sax
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload
import structlog

from app.core.config import settings
from app.core.systems import get_system_by_folder
from app.db.session import SessionLocal
from app.db.models import Game, RomFile, MediaAsset
from app.services.organize import build_plan
from app.services.render_launchbox import (
    _extra_name, _disc_num, _esc, _is_hidden, _write_with_backup,
    sanitize_media_title, NO_VIDEO_SYSTEMS, _DISC_RE, _PART_RE,
)

log = structlog.get_logger()


# ------------------------------------------------------------------------------------
# RomFleet MediaAsset.media_type -> Batocera gamelist element + on-disk convention
# ------------------------------------------------------------------------------------
# Only the fields the task spec asks for. Media lives under <system_folder>/images/ and
# <system_folder>/videos/ (siblings of the ROMs themselves — there's no per-platform Data/
# tree like LaunchBox; each Batocera system folder is its own root). Filenames are keyed
# off the GAME's title (sanitize_media_title, same convention as render_launchbox) so every
# sibling entry in a competition/multi-file set shares ONE copy of the game's box art/video
# instead of duplicating it per launchable entry.
@dataclass(frozen=True)
class BatoMediaTarget:
    subdir: str    # "images" | "videos"
    suffix: str    # filename suffix, e.g. "-image"
    tag: str       # gamelist element name


BATO_MEDIA: dict[str, BatoMediaTarget] = {
    "boxfront": BatoMediaTarget("images", "-image", "image"),
    "logo":     BatoMediaTarget("images", "-marquee", "marquee"),
    "video":    BatoMediaTarget("videos", "-video", "video"),
}


def _release_dt(release_date: str | None) -> str | None:
    """Batocera/ES wants %Y%m%dT%H%M%S. RomFleet stores a plain 'YYYY-MM-DD' (or
    partial 'YYYY' / 'YYYY-MM') string."""
    if not release_date:
        return None
    d = release_date.strip()
    parts = d.split("-")
    try:
        y = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 and parts[1] else 1
        day = int(parts[2]) if len(parts) > 2 and parts[2] else 1
        return f"{y:04d}{m:02d}{day:02d}T000000"
    except (ValueError, IndexError):
        return None


def _rating(game: Game) -> float | None:
    """<rating> is a 0.0-1.0 float in the ES schema. Personal star_rating (0-5) wins;
    falls back to the scraped community_rating (also 0-5) when unset — same convention
    documented on Game.star_rating for the Batocera render."""
    r = game.star_rating if game.star_rating is not None else game.community_rating
    if r is None:
        return None
    return round(max(0.0, min(1.0, r / 5.0)), 3)


def _entry_label(filenames: list[str], idx: int, folder: str | None = None) -> str:
    """Label for filenames[idx] within a multi-file (disc/part/competition) group,
    reusing render_launchbox's `_extra_name`. That function only inspects `extra_fn`
    for the Disc-N/Part-N/Vol-N regex branches (the `primary_fn` arg is only used by
    the no-marker fallback, to find/strip a shared prefix) — so ANY member of the group
    can be labeled by diffing it against any OTHER member, not just index 0."""
    other = filenames[1] if idx == 0 else filenames[0]
    return _extra_name(other, filenames[idx], folder)


_PAREN_RE = re.compile(r"\(([^)]*)\)")


def _distinguishing_tag(fn: str, sibling_fns: list[str]) -> str | None:
    """A parenthetical tag present in `fn` but in none of `sibling_fns` — used to break a
    label collision (see `_group_labels`), e.g. an "(Uncensored)" variant of the same
    disc number. Skips tags that are themselves a Disc/Part/Vol marker (already the label)."""
    other_tags: set[str] = set()
    for o in sibling_fns:
        other_tags.update(_PAREN_RE.findall(o))
    for tag in _PAREN_RE.findall(fn):
        wrapped = f"({tag})"
        if tag in other_tags:
            continue
        if _DISC_RE.search(wrapped) or _PART_RE.search(wrapped):
            continue
        return tag
    return None


def _group_labels(filenames: list[str], folder: str | None = None) -> list[str]:
    """Entry label for every file in a multi-file group, with collision disambiguation.
    Disc/Part/Vol markers are normally unique per file, but a genuine content variant that
    shares a disc number (e.g. a normal + an "(Uncensored)" Disc 2) isn't stripped by
    organize's 1G1R slotting — both get kept, and would otherwise render identical labels."""
    labels = [_entry_label(filenames, i, folder) for i in range(len(filenames))]
    counts = Counter(labels)
    dup_groups: dict[str, list[int]] = {}
    for i, lab in enumerate(labels):
        if counts[lab] > 1:
            dup_groups.setdefault(lab, []).append(i)
    for lab, idxs in dup_groups.items():
        for n, i in enumerate(idxs, start=1):
            siblings = [filenames[j] for j in idxs if j != i]
            tag = _distinguishing_tag(filenames[i], siblings)
            labels[i] = f"{lab} ({tag})" if tag else f"{lab} ({n})"
    return labels


def _game_xml(title: str, path: str, game: Game, hidden: bool,
              image: str | None, video: str | None, marquee: str | None) -> str:
    lines = ["  <game>"]

    def tag(name: str, value):
        if value is None or value == "":
            return
        lines.append(f"    <{name}>{_esc(value)}</{name}>")

    tag("path", path)
    tag("name", title)
    tag("desc", game.description)
    tag("image", image)
    tag("video", video)
    tag("marquee", marquee)
    tag("rating", _rating(game))
    tag("releasedate", _release_dt(game.release_date))
    tag("developer", game.developer)
    tag("publisher", game.publisher)
    tag("genre", game.genre)
    tag("players", game.players)
    if getattr(game, "is_favorite", False):
        tag("favorite", "true")
    # play state (canonical, merged fleet-wide by sync_batocera_state before this render).
    # <playcount> is the accumulated total; <lastplayed> is ES's %Y%m%dT%H%M%S; <gametime> is
    # SECONDS and comes from RA's authoritative playtime (playtime_minutes), NOT the device.
    if getattr(game, "play_count", 0):
        tag("playcount", game.play_count)
    _lp = getattr(game, "last_played", None)
    if _lp is not None:
        tag("lastplayed", _lp.strftime("%Y%m%dT%H%M%S"))
    _ptm = getattr(game, "playtime_minutes", None)
    if _ptm:
        tag("gametime", int(_ptm) * 60)
    if hidden:
        tag("hidden", "true")
    lines.append("  </game>")
    return "\n".join(lines)


@dataclass
class GamelistStats:
    system: str
    entries: int = 0
    standalone: int = 0
    competition_sets: int = 0
    competition_entries: int = 0
    hidden: int = 0
    skipped_not_published: int = 0
    media_copied: int = 0
    media_missing_primary: int = 0
    xml_result: dict = field(default_factory=dict)
    media_results: list = field(default_factory=list)
    sync_in: dict = field(default_factory=dict)


def _selected_roms(db, folder: str):
    """WHICH RomFiles are in this system's library: reuse organize.build_plan() ONLY to
    decide membership (op in keep/rename == the 1G1R/full-set keeper set, the same
    decision that drives apply_publish) — never to recompute a name. The `<path>` a
    kept RomFile is published under is its own `RomFile.filename`; that IS the on-disk
    name (render_launchbox derives ApplicationPath the same way, straight from
    `r.filename`, not from a re-derived canonical name — the DB's "canonical" name can
    legitimately drift from a curated/human-renamed publish, e.g. hack-competition
    entries kept under a short curated name rather than the RA hash-name)."""
    plan = build_plan(db, folder)
    keep_ids = [a["rom_id"] for a in plan["actions"] if a["op"] in ("keep", "rename")]
    if not keep_ids:
        return {}
    roms = db.scalars(select(RomFile).where(RomFile.id.in_(keep_ids))
                       .options(selectinload(RomFile.game))).all()
    by_game: dict[int, list[RomFile]] = {}
    for r in roms:
        if r.game_id is None or r.game is None:
            continue
        by_game.setdefault(r.game_id, []).append(r)
    return by_game


def _copy_media(game: Game, group_key: str, output_dir: Path, dry_run: bool,
                 skip_video: bool = False) -> list[dict]:
    """Copy each media_type's primary asset once per game (shared by every entry in a
    competition/multi-file group — mirrors how LaunchBox's AdditionalApplication
    entries all show the parent game's media, no per-disc/per-entry art exists)."""
    results = []
    sanitized = sanitize_media_title(group_key)
    if not sanitized:
        return results
    by_type: dict[str, MediaAsset] = {}
    for m in game.media:
        if not m.path:
            continue
        if skip_video and m.media_type == "video":
            continue
        if m.media_type not in by_type or m.is_primary:
            by_type.setdefault(m.media_type, m)
            if m.is_primary:
                by_type[m.media_type] = m
    media_store = Path(settings.canonical_path) / "media"
    for mtype, asset in by_type.items():
        target = BATO_MEDIA.get(mtype)
        if not target:
            continue
        src = media_store / asset.path
        ext = Path(asset.path).suffix or ".jpg"
        dest = output_dir / target.subdir / f"{sanitized}{target.suffix}{ext}"
        r = {"game": game.title, "media_type": mtype, "src": str(src), "dest": str(dest)}
        if not src.exists():
            r["error"] = "source missing"
            results.append(r)
            continue
        if dry_run:
            r["would_copy"] = not (dest.exists() and dest.stat().st_size == src.stat().st_size)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            r["copied"] = True
        results.append(r)
    return results


def render_system(folder: str, output_root: str | Path | None = None,
                   dry_run: bool = False, db=None, media: bool = True,
                   endpoint: str | None = None) -> GamelistStats:
    """Render one Batocera system folder's curated library to gamelist.xml + media.

    output_root: defaults to `<canonical_path>/roms/<folder>` — the SAME folder the
    ROMs are already published into (Batocera has no separate export-bundle root like
    LaunchBox). Pass an alternate dir to render into a scratch location for review
    without touching the live share.
    """
    sysc = get_system_by_folder(folder)
    if not sysc:
        raise ValueError(f"unknown system folder: {folder}")

    canon_root = Path(settings.canonical_path) / "roms" / folder  # the REAL library, always
    out = Path(output_root) if output_root else canon_root        # where we WRITE (may be a scratch dir)
    to_live = out == canon_root                                   # writing to the share Batocera mounts
    stats = GamelistStats(system=folder)
    owns_db = db is None
    db = db or SessionLocal()
    try:
        # sync-IN BEFORE render/OUT: pull the device's favorite/playcount/lastplayed edits from the
        # in-place gamelist.xml so the merged canonical totals are what we render back out below.
        if not dry_run:
            try:
                from app.services.favorites_sync import sync_batocera_state
                stats.sync_in = sync_batocera_state(folder, db=db)
            except Exception as e:  # noqa: best-effort, never block a render
                log.warning("batocera pre-sync failed", folder=folder, err=str(e))

        by_game = _selected_roms(db, folder)
        game_blocks: list[str] = []

        for gid, roms in by_game.items():
            game = roms[0].game
            # RomFile.filename — the actual published name — sorted the same way
            # render_launchbox orders discs/parts (disc/part number, then name) so entry
            # labeling and "which one is the reference for prefix-diffing" line up
            # across renderers.
            roms = sorted(roms, key=lambda r: (_disc_num(r.filename), r.filename))
            filenames = [r.filename for r in roms]
            is_set = len(roms) > 1
            labels = _group_labels(filenames, folder) if is_set else []
            if is_set:
                stats.competition_sets += 1

            hidden = _is_hidden(game, endpoint)

            group_media_done = False
            image_rel = video_rel = marquee_rel = None
            for idx, r in enumerate(roms):
                canon_name = r.filename
                rom_path = canon_root / canon_name
                if not rom_path.exists():
                    # genuinely missing on disk (not a name-mismatch false positive —
                    # `canon_name` IS the RomFile's own published filename) — a real gap
                    # worth flagging (e.g. deleted outside the app), never emit a <game>
                    # Batocera can't launch.
                    stats.skipped_not_published += 1
                    log.warning("render_gamelist: published file missing on disk, skipping entry",
                                folder=folder, game=game.title, filename=canon_name)
                    continue

                title = game.title if not is_set else f"{game.title} — {labels[idx]}"
                path = f"./{canon_name}"

                if media and not group_media_done:
                    media_results = _copy_media(game, game.title, out, dry_run,
                                                skip_video=folder in NO_VIDEO_SYSTEMS)
                    stats.media_results.extend(media_results)
                    for mr in media_results:
                        if mr.get("error"):
                            continue
                        stats.media_copied += 1
                        tgt = BATO_MEDIA.get(mr["media_type"])
                        rel = f"./{tgt.subdir}/{Path(mr['dest']).name}"
                        if mr["media_type"] == "boxfront":
                            image_rel = rel
                        elif mr["media_type"] == "logo":
                            marquee_rel = rel
                        elif mr["media_type"] == "video":
                            video_rel = rel
                    if not any(m.media_type == "boxfront" for m in game.media):
                        stats.media_missing_primary += 1
                    group_media_done = True

                game_blocks.append(_game_xml(title, path, game, hidden,
                                              image_rel, video_rel, marquee_rel))
                stats.entries += 1
                if hidden:
                    stats.hidden += 1
                if is_set:
                    stats.competition_entries += 1
                else:
                    stats.standalone += 1

        xml = ['<?xml version="1.0" encoding="utf-8"?>', "<gameList>"]
        xml.extend(game_blocks)
        xml.append("</gameList>")
        content = "\n".join(xml) + "\n"

        xml_path = out / "gamelist.xml"
        # backup=False: gamelist.xml lands in the LIVE canonical share Batocera mounts, and is
        # deterministically re-renderable — timestamped .bak copies there just accumulate as junk.
        stats.xml_result = _write_with_backup(xml_path, content, dry_run, backup=False)
        # re-baseline play_sync["batocera"] to the play_count we just wrote — but ONLY when we
        # wrote to the LIVE share (not a scratch review dir), else the device never saw it.
        if not dry_run and to_live:
            try:
                from app.services.favorites_sync import writeback_playcount
                stats.sync_in["writeback"] = writeback_playcount("batocera", systems=[folder])
            except Exception as e:  # noqa
                log.warning("batocera playcount writeback failed", folder=folder, err=str(e))
    finally:
        if owns_db:
            db.close()

    log.info("render_gamelist", folder=folder, entries=stats.entries,
              competition_sets=stats.competition_sets,
              competition_entries=stats.competition_entries,
              skipped_not_published=stats.skipped_not_published,
              media_copied=stats.media_copied, dry_run=dry_run)
    return stats
