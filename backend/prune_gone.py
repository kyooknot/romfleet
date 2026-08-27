#!/usr/bin/env python3
"""Drop rom_files rows whose file no longer exists on disk.

ingest_system only ADDS what it finds; it never notices a path that disappeared. After the
cue/bin -> chd conversion that left 17 stale rows behind, so the library reported 830 ROMs
against 812 real files. Dry-run by default; pass --apply to commit.

Usage: prune_gone.py <folder> [--apply]
"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
os.chdir(Path(__file__).resolve().parents[1] / "backend")
from app.db.session import SessionLocal
from app.db.models import RomFile
from app.services.ingest import prune_orphan_games

folder = [a for a in sys.argv[1:] if not a.startswith("--")][0]
apply = "--apply" in sys.argv
db = SessionLocal()
try:
    rows = db.query(RomFile).filter(RomFile.system_folder == folder).all()
    gone = [r for r in rows if not os.path.exists(r.path)]
    print("%s: %d rom_files rows, %d point at files that no longer exist" % (folder, len(rows), len(gone)))
    for r in gone[:30]:
        print("   %-9s %s" % (r.match_status, os.path.basename(r.path)[:74]))
    if not apply:
        print("\nDRY RUN — pass --apply to delete")
    else:
        for r in gone:
            db.query(RomFile).filter_by(id=r.id).delete()
        db.commit()
        n = prune_orphan_games(db)
        print("\ndeleted %d stale rom_files, pruned %d orphan game(s)" % (len(gone), n))
        left = db.query(RomFile).filter(RomFile.system_folder == folder).count()
        print("rom_files now: %d" % left)
finally:
    db.close()
