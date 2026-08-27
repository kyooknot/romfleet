from pathlib import Path
import asyncio, sys, time, json, shutil
# Resolve the backend package relative to THIS file, so the tool works under any
# install prefix rather than only /opt/romfleet.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.core.systems import load_systems
from app.services.missing import missing_for_system
from app.services.sourcing import _sources_for, source_missing

LOG = str(Path(__file__).resolve().parents[1] / "logs" / "overnight_source_20260720.log")

def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    with open(LOG, "a") as f:
        f.write(line + "\n")

async def main():
    targets = []
    for s in load_systems():
        try:
            m = await missing_for_system(s.folder)
            miss = len(m["missing"])
        except Exception as e:
            log(f"SKIP {s.folder}: missing-calc error {e}")
            continue
        if miss > 0 and _sources_for(s.folder):
            targets.append((s.folder, miss))
    targets.sort(key=lambda t: t[1])  # smallest first: quick wins, gentle ramp
    log(f"START overnight sourcing: {len(targets)} systems, {sum(t[1] for t in targets)} missing games")
    log("targets: " + ", ".join(f"{f}({n})" for f, n in targets))
    grand = {"sourced": 0, "not_found": 0, "errors": 0}
    for folder, miss in targets:
        free_gb = shutil.disk_usage("/mnt/romfleet").free / 1e9
        if free_gb < 500:
            log(f"ABORT: low disk {free_gb:.0f}GB before {folder}")
            break
        log(f">>> {folder}: {miss} missing ({free_gb:.0f}GB free) — sourcing (allow_search)...")
        try:
            r = await source_missing(folder, allow_search=True)
            summary = {k: r.get(k) for k in ("wanted", "sourced", "not_found", "errors")}
            log(f"<<< {folder}: {json.dumps(summary)}")
            for k in grand:
                grand[k] += (r.get(k) or 0)
        except Exception as e:
            log(f"!!! {folder}: EXCEPTION {type(e).__name__}: {e}")
        await asyncio.sleep(5)  # gentle between systems (archive.org courtesy)
    log(f"DONE overnight sourcing. Totals: {json.dumps(grand)}")

asyncio.run(main())
