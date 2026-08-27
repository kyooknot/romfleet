"""Pull the Lost Level Archive DAT set straight from its GitHub repo.

LLA publishes its DATs at github.com/televandalist/lost-level-archive (DATs/*.xml), so unlike
No-Intro/Redump (which need a manual DAT-o-MATIC / redump.org download) these are fetchable.
RA membership is separate — that's pulled from the RA API via `sync-ra`/`enrich-ra`.
"""
from __future__ import annotations
from pathlib import Path
import httpx
from app.core.config import settings

REPO = "televandalist/lost-level-archive"
_TREE = f"https://api.github.com/repos/{REPO}/git/trees/HEAD?recursive=1"
_RAW = f"https://raw.githubusercontent.com/{REPO}/HEAD/"


def update_lla_dats() -> dict:
    """Download the current DATs/*.xml set into dats_path/lost-level-archive/DATs/, replacing
    stale versions. Only files that fail to download are left as-is (no partial wipe)."""
    dest = Path(settings.dats_path) / "lost-level-archive" / "DATs"
    dest.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=60, follow_redirects=True,
                      headers={"User-Agent": "RomFleet"}) as http:
        tree = http.get(_TREE).json().get("tree", [])
        paths = [t["path"] for t in tree
                 if t["path"].startswith("DATs/") and t["path"].lower().endswith(".xml")]
        new_names, downloaded, errors = set(), 0, 0
        for p in paths:
            r = http.get(_RAW + p.replace(" ", "%20"))
            if r.status_code == 200 and r.content:
                (dest / Path(p).name).write_bytes(r.content)
                new_names.add(Path(p).name)
                downloaded += 1
            else:
                errors += 1
        # drop old-version dats no longer in the published set (safe: only untouched-this-run)
        removed = 0
        for f in dest.glob("*.xml"):
            if f.name not in new_names and new_names:
                f.unlink()
                removed += 1
    return {"source": "lost-level-archive", "downloaded": downloaded, "removed": removed,
            "errors": errors, "total": len(paths), "dest": str(dest)}
