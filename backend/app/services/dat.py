"""Logiqx DAT parsing — identification (and optionally membership gate).

One parser for all Logiqx-format DATs (No-Intro / Redump / MAME / Lost Level Archive).
LLA DATs are keyed by RA console id (filename prefix + <header><id>), so DAT selection
per system is a direct lookup. For raw cartridge systems the DAT `md5` IS the RA hash.
"""
from __future__ import annotations
from pathlib import Path
from functools import lru_cache
import xml.etree.ElementTree as ET
from app.core.config import settings
import structlog

log = structlog.get_logger()


def _parse_dat_md5_index(path: Path) -> dict[str, str]:
    """{md5: canonical game name} for one Logiqx DAT."""
    idx: dict[str, str] = {}
    try:
        for _, el in ET.iterparse(str(path), events=("end",)):
            if el.tag == "game":
                name = el.get("name")
                rom = el.find("rom")
                if rom is not None:
                    md5 = (rom.get("md5") or "").lower()
                    if md5:
                        idx.setdefault(md5, name or rom.get("name") or "")
                el.clear()
    except Exception as e:
        log.warning("DAT parse failed", path=str(path), error=str(e)[:120])
    return idx


@lru_cache(maxsize=128)
def lla_index(console_id: int) -> dict[str, str]:
    """Merged md5->name index of the Lost Level Archive DAT(s) for an RA console id."""
    d = Path(settings.dats_path) / "lost-level-archive" / "DATs"
    if not d.is_dir():
        return {}
    idx: dict[str, str] = {}
    for f in sorted(d.glob(f"{console_id:03d} *.xml")):   # e.g. "001 - ... Genesis (v...).xml"
        for md5, name in _parse_dat_md5_index(f).items():
            idx.setdefault(md5, name)
    return idx
