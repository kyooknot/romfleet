"""
M3U playlist generator for multi-disc games.

Finds matched ROMs with "(Disc X)" in the filename, groups them by
game_id, and for each group with 2+ discs:
  1. Creates a .m3u playlist in the main system folder
  2. Moves the disc files into a multidisc/ subfolder
  3. Updates ES records with new filepaths

Single-disc .chd files (no disc marker) are never touched.
Already-processed games (m3u exists or files already in multidisc/) are skipped.
"""
from __future__ import annotations
import re
import shutil
from pathlib import Path
from datetime import datetime, timezone
from elasticsearch import AsyncElasticsearch
from app.core.config import settings
import structlog

log = structlog.get_logger()

MULTIDISC_DIR = "multidisc"


def _disc_number(filename: str) -> int:
    m = re.search(r'\(Disc\s*(\d+)\)', filename, re.IGNORECASE)
    return int(m.group(1)) if m else 0


def _m3u_stem(filename: str) -> str:
    """Strip '(Disc X)' and trailing whitespace from a filename stem."""
    stem = Path(filename).stem
    return re.sub(r'\s*\(Disc\s*\d+\)', '', stem, flags=re.IGNORECASE).strip()


async def generate_m3u(
    es: AsyncElasticsearch,
    roms_root: Path,
    system_folder: str | None = None,
    celery_task=None,
    job_id: str | None = None,
) -> dict:
    # Query all matched ROMs that have a disc marker in the filename
    query: dict = {
        "bool": {
            "must": [
                {"term": {"match_status": "matched"}},
                {"wildcard": {"filename.keyword": {"value": "*(Disc*", "case_insensitive": True}}},
            ]
        }
    }
    if system_folder:
        query["bool"]["must"].append({"term": {"system_folder": system_folder}})

    resp = await es.search(
        index=settings.es_index_roms,
        body={"query": query, "size": 10000,
              "_source": ["filename", "filepath", "system_folder", "matched_game_id", "matched_game_title"]},
    )

    # Group by (system_folder, matched_game_id)
    groups: dict[tuple, list[dict]] = {}
    for hit in resp["hits"]["hits"]:
        src = hit["_source"]
        key = (src["system_folder"], src.get("matched_game_id"))
        groups.setdefault(key, []).append(src)

    created = skipped = moved = errors = 0

    for (folder, game_id), discs in groups.items():
        if len(discs) < 2:
            skipped += 1
            continue

        discs.sort(key=lambda d: _disc_number(d["filename"]))
        system_dir  = roms_root / folder
        multi_dir   = system_dir / MULTIDISC_DIR
        m3u_name    = _m3u_stem(discs[0]["filename"]) + ".m3u"
        m3u_path    = system_dir / m3u_name

        if m3u_path.exists():
            skipped += 1
            continue

        # Move disc files and collect relative paths for the playlist
        playlist_lines: list[str] = []
        ok = True
        for disc in discs:
            src_path = Path(disc["filepath"])

            # Already in multidisc/ — just record the relative path
            if MULTIDISC_DIR in src_path.parts:
                playlist_lines.append(f"{MULTIDISC_DIR}/{src_path.name}")
                continue

            if not src_path.exists():
                log.warning("Disc file missing — skipping game",
                            game=disc.get("matched_game_title"), file=str(src_path))
                ok = False
                break

            multi_dir.mkdir(parents=True, exist_ok=True)
            dest_path = multi_dir / src_path.name
            try:
                shutil.move(str(src_path), str(dest_path))
                moved += 1
                # Update ES record
                await es.update(
                    index=settings.es_index_roms,
                    id=str(src_path),
                    doc={"filepath": str(dest_path), "filename": dest_path.name},
                    retry_on_conflict=3,
                )
            except Exception as e:
                log.error("Failed to move disc file", src=str(src_path), error=str(e))
                ok = False
                break
            playlist_lines.append(f"{MULTIDISC_DIR}/{src_path.name}")

        if not ok:
            errors += 1
            continue

        # Write .m3u
        try:
            m3u_path.write_text("\n".join(playlist_lines) + "\n", encoding="utf-8")
            created += 1
            log.info("Created m3u", path=str(m3u_path), discs=len(discs))
        except Exception as e:
            log.error("Failed to write m3u", path=str(m3u_path), error=str(e))
            errors += 1

        if celery_task:
            celery_task.update_state(
                state="STARTED",
                meta={"created": created, "skipped": skipped, "errors": errors},
            )

    summary = {
        "created":  created,
        "skipped":  skipped,
        "moved":    moved,
        "errors":   errors,
        "run_at":   datetime.now(timezone.utc).isoformat(),
    }
    log.info("M3U generation complete", **summary)
    return summary
