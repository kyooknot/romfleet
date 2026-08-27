from __future__ import annotations
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from functools import lru_cache
from app.core.config import settings


@dataclass
class SystemConfig:
    folder: str            # Batocera folder name, e.g. "megadrive"
    ra_console_id: int
    ra_name: str
    hash_method: str       # raw | nes | snes | n64 | psx_chd | saturn_chd | ...
    extensions: list[str]  # file extensions this system uses
    emulators: list[str] = field(default_factory=list)
    # membership provider(s): which identity DB(s) confer library membership.
    # "ra" (default) | "no-intro" | "redump" | "mame" | "lla" | list of these.
    gate: str | list = "ra"
    # Sub-systems that share ONE base RA console (e.g. NGP + NGPC both = console 14) split the
    # catalog by hash-name extension so each folder counts/sources only its own games.
    name_include: list[str] = field(default_factory=list)  # keep only RA games with these hash exts
    name_exclude: list[str] = field(default_factory=list)  # drop RA games with these hash exts
    # Hash-based split (preferred over name_*): a game belongs to this sub-system if any of its
    # md5s is in the named No-Intro library hash set (backend/app/data/library/<name>.txt).
    library_include: list[str] = field(default_factory=list)  # keep games whose hash is in these
    library_exclude: list[str] = field(default_factory=list)  # drop games whose hash is in these
    # LaunchBox "scrape as" override (Data\Platforms.xml <ScrapeAs>): make a custom
    # platform borrow another platform's identity for LaunchBox metadata scraping AND its
    # native RetroAchievements console mapping. NAOMI/NAOMI2/Atomiswave -> "Arcade"
    # (RA console 27) so LaunchBox matches them by arcade-name hash and shows RA badges.
    scrape_as: str | None = None


@lru_cache(maxsize=None)
def load_library(name: str) -> frozenset:
    """md5 set for a No-Intro library (Satellaview, N64DD, …), used for the hash-based
    shared-console split. Empty set if the file is absent."""
    p = Path(__file__).resolve().parents[1] / "data" / "library" / f"{name}.txt"
    if not p.exists():
        return frozenset()
    return frozenset(l.strip().lower() for l in p.read_text().splitlines() if l.strip())


@lru_cache(maxsize=1)
def load_systems() -> list[SystemConfig]:
    path = Path(settings.config_path) / "systems.yaml"
    with open(path) as f:
        data = yaml.safe_load(f)
    return [SystemConfig(**s) for s in data["systems"]]


def gate_list(value, default="ra") -> list[str]:
    """Normalize a gate spec (DB override string 'no-intro,lla', a list, or None) into a
    list of provider ids, falling back to the systems.yaml default."""
    v = value if value not in (None, "") else default
    if isinstance(v, str):
        return [g.strip() for g in v.split(",") if g.strip()] or ["ra"]
    return list(v) if v else ["ra"]


def get_system_by_folder(folder: str) -> SystemConfig | None:
    for s in load_systems():
        if s.folder == folder:
            return s
    return None


def get_system_by_console_id(console_id: int) -> list[SystemConfig]:
    return [s for s in load_systems() if s.ra_console_id == console_id]


def folder_to_console_id() -> dict[str, int]:
    """Returns {folder_name: ra_console_id} — used by the scanner."""
    seen = {}
    for s in load_systems():
        if s.folder not in seen:
            seen[s.folder] = s.ra_console_id
    return seen
