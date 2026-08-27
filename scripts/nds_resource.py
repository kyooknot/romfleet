from pathlib import Path
import asyncio, sys, time
# Resolve the backend package relative to THIS file, so the tool works under any
# install prefix rather than only /opt/romfleet.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services.missing import missing_for_system
from app.services.sourcing import source_missing
L=str(Path(__file__).resolve().parents[1] / "logs" / "nds_torrent_resource.log")
def log(m): open(L,"a").write(f"{time.strftime('%H:%M:%S')} {m}\n")
async def main():
    log("start nds re-source (torrent enabled): missing="+str([g['title'] for g in (await missing_for_system('nds'))['missing']]))
    r=await source_missing("nds", allow_search=True)
    via={k.replace('via_',''):v for k,v in r.items() if k.startswith('via_') and v}
    log(f"RESULT sourced={r.get('sourced')} not_found={r.get('not_found')} via={via} added={r.get('added')}")
    log("still missing: "+str([g['title'] for g in (await missing_for_system('nds'))['missing']]))
asyncio.run(main())
