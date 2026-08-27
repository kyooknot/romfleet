from pathlib import Path
import asyncio, sys, time, json, shutil
# Resolve the backend package relative to THIS file, so the tool works under any
# install prefix rather than only /opt/romfleet.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.core.systems import load_systems
from app.services.missing import missing_for_system
from app.services.sourcing import _sources_for, source_missing

LOG = str(Path(__file__).resolve().parents[1] / "logs" / "sweep_source_20260721.log")
def log(m):
    line=f"{time.strftime('%Y-%m-%d %H:%M:%S')} {m}"
    open(LOG,"a").write(line+"\n")

async def main():
    targets=[]
    for s in load_systems():
        n=len((await missing_for_system(s.folder))['missing'])
        if n>0 and _sources_for(s.folder):   # has a registered source; fallbacks (search/patch) run inside
            targets.append((s.folder,n))
    targets.sort(key=lambda t:t[1])
    log(f"START post-fix sweep: {len(targets)} systems, {sum(t[1] for t in targets)} missing (excludes no-source ps2)")
    log("targets: "+", ".join(f"{f}({n})" for f,n in targets))
    grand={"sourced":0,"not_found":0,"errors":0}
    vias={}
    for folder,n in targets:
        if shutil.disk_usage("/mnt/romfleet").free/1e9 < 500:
            log("ABORT low disk"); break
        log(f">>> {folder}: {n} missing — sourcing (search+multidisc+patch)...")
        try:
            r=await source_missing(folder, allow_search=True)
            via={k.replace('via_',''):v for k,v in r.items() if k.startswith('via_') and v}
            log(f"<<< {folder}: sourced={r.get('sourced')} not_found={r.get('not_found')} via={via} added={r.get('added')}")
            for k in grand: grand[k]+=(r.get(k) or 0)
            for k,v in via.items(): vias[k]=vias.get(k,0)+v
        except Exception as e:
            log(f"!!! {folder}: {type(e).__name__}: {e}")
        await asyncio.sleep(4)
    log(f"DONE sweep. Totals: {json.dumps(grand)} | by-method: {json.dumps(vias)}")
asyncio.run(main())
