"""Per-console display name (manufacturer + RA name) + RA console icon for the sidebar.

RA's GetConsoleIDs returns an IconURL per console (static.retroachievements.org/...). We
cache each icon locally (LAN-only) and serve it at /static/console/<id>.png.
"""
from __future__ import annotations
from pathlib import Path
import httpx

_ICON_DIR = Path(__file__).parent.parent / "web" / "console"
_ICON_URLS: dict[int, str] | None = None

# RA console id -> manufacturer (prefixed onto the RA name for the sidebar)
MANUFACTURER = {
    1: "Sega", 11: "Sega", 15: "Sega", 10: "Sega", 9: "Sega", 39: "Sega", 40: "Sega", 33: "Sega",
    7: "Nintendo", 3: "Nintendo", 2: "Nintendo", 16: "Nintendo", 19: "Nintendo", 4: "Nintendo",
    6: "Nintendo", 5: "Nintendo", 18: "Nintendo", 28: "Nintendo", 24: "Nintendo", 81: "Nintendo",
    12: "Sony", 21: "Sony", 41: "Sony",
    25: "Atari", 51: "Atari", 13: "Atari", 17: "Atari", 77: "Atari",
    8: "NEC", 76: "NEC", 49: "NEC", 44: "Coleco", 45: "Mattel", 53: "Bandai", 14: "SNK", 56: "SNK", 46: "GCE",
}


def display_name(console_id: int, ra_name: str) -> str:
    mfr = MANUFACTURER.get(console_id)
    if mfr and not (ra_name or "").lower().startswith(mfr.lower()):
        return f"{mfr} {ra_name}".strip()
    return ra_name or ""


# Sub-systems that share an RA console but want their OWN sidebar icon (RA has no distinct
# console for them). File lives in web/console/<name> (fetched from ScreenScraper system art).
FOLDER_ICON = {"supergrafx": "supergrafx.png"}


def ensure_icon_for(folder: str | None, console_id: int) -> str | None:
    """Folder-specific icon override, else the shared RA console icon."""
    name = FOLDER_ICON.get(folder or "")
    if name and (_ICON_DIR / name).exists():
        return f"/static/console/{name}"
    return ensure_icon(console_id)


def _icon_urls() -> dict[int, str]:
    global _ICON_URLS
    if _ICON_URLS is None:
        _ICON_URLS = {}
        try:
            from app.core.credentials import cred
            from app.core.config import settings
            r = httpx.get(f"{settings.ra_api_base}/API_GetConsoleIDs.php",
                          params={"y": cred("ra_api_key"), "z": cred("ra_username")}, timeout=30)
            for c in r.json():
                if c.get("IconURL"):
                    _ICON_URLS[int(c["ID"])] = c["IconURL"]
        except Exception:  # noqa
            pass
    return _ICON_URLS


def ensure_icon(console_id: int) -> str | None:
    """Download the RA console icon if not cached; return its /static path (or None)."""
    _ICON_DIR.mkdir(parents=True, exist_ok=True)
    dest = _ICON_DIR / f"{console_id}.png"
    if not dest.exists():
        url = _icon_urls().get(console_id)
        if not url:
            return None
        try:
            r = httpx.get(url, timeout=20, follow_redirects=True)
            if r.status_code == 200 and r.content:
                dest.write_bytes(r.content)
        except Exception:  # noqa
            return None
    return f"/static/console/{console_id}.png" if dest.exists() else None
