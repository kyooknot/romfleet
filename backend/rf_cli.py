#!/usr/bin/env python3
"""Run long RomFleet jobs OUTSIDE uvicorn.

/api/systems/{folder}/source-missing and /reingest do their work inline in the request handler,
so a multi-hour sweep makes the entire API and UI unreachable until it finishes -- disconnecting
the client does not stop it either, because the server keeps processing. Same code, invoked here,
leaves the site up.

Usage:  rf_cli.py {ingest|rederive|source-missing|gamelist} <folder> [limit|ids=1,2,3]

`ids=` targets specific RA game ids instead of walking the whole missing list. A system-wide
ps2 sweep hits the 2h cap long before it reaches the tail, so the games that most need a
re-attempt (stale verdicts from before a fix) never get one.
"""
import asyncio, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
os.chdir(Path(__file__).resolve().parents[1] / "backend")


async def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    job, folder = sys.argv[1], sys.argv[2]
    arg3 = sys.argv[3] if len(sys.argv) > 3 else None
    ids = None
    limit = None
    if arg3:
        if arg3.startswith("ids="):
            ids = [int(x) for x in arg3[4:].split(",") if x.strip()]
        else:
            limit = int(arg3)
    from app.core.elasticsearch import get_es, init_es, close_es
    await init_es()
    try:
        if job == "ingest":
            from app.services.ingest import ingest_system
            res = await ingest_system(get_es(), folder, limit)
        elif job == "rederive":
            from app.services.ingest import rederive_system
            res = await rederive_system(get_es(), folder)
        elif job == "source-missing":
            from app.services.sourcing import source_missing
            res = await source_missing(folder, limit, ids=ids) if ids else \
                await source_missing(folder, limit)
        elif job == "gamelist":
            # Batocera gamelist.xml is rendered from the CURRENT library, so it goes stale the
            # moment ROMs are converted, re-selected or re-sourced. A 2026-07-31 audit found
            # 284 entries across 34 systems pointing at files that no longer exist -- 59 of
            # them PS2, where every .iso and .cue had become a .chd.
            from app.services.render_gamelist import render_system
            res = render_system(folder)
        else:
            raise SystemExit("unknown job: %s" % job)
    finally:
        await close_es()
    print(res, flush=True)


asyncio.run(main())
