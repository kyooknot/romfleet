from pathlib import Path
#!/usr/bin/env python3
"""Print the RA content hash of one file using RomFleet's own hasher.

Importing app.hashers is what puts /usr/local/bin on PATH (rahash, chd_extract live there and
are NOT on the default PATH in this container) -- the exact gap that produced every
"hasher errored -- toolchain problem" verdict in the Missing list.

Usage: hash_one.py <system-folder> <path>
"""
import asyncio, os, sys
# Resolve the backend package relative to THIS file, so the tool works under any
# install prefix rather than only /opt/romfleet.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
os.chdir(Path(__file__).resolve().parents[1] / "backend")
from app.hashers import get_hasher

async def main() -> None:
    folder, path = sys.argv[1], sys.argv[2]
    H = get_hasher(folder)
    try:
        r = await asyncio.wait_for(H.hash_file(path), timeout=2700)
        print((str(r).strip().split() or [""])[-1].lower())
    except Exception as e:
        print("ERR:" + str(e)[:90].replace("\n", " "))

asyncio.run(main())
