"""Scraper credentials — enter in the UI (stored in DB), falling back to .env.

A UI-entered value (settings table, key 'credentials') overrides the matching .env
field. Secrets are never returned in full — the API masks them (••••last4). Use
`cred(key)` everywhere a scraper needs a credential.
"""
from __future__ import annotations
from app.core.config import settings
from app.db.models import Setting

# UI schema: source -> label + [(config_key, label, kind)]  kind: text | secret
CRED_FIELDS = {
    "ra":        {"label": "RetroAchievements",
                  "fields": [("ra_username", "Username", "text"), ("ra_api_key", "Web API key", "secret")]},
    "tgdb":      {"label": "TheGamesDB",
                  "fields": [("tgdb_api_key", "API key (UUID)", "secret")]},
    "igdb":      {"label": "IGDB (Twitch app)",
                  "fields": [("igdb_client_id", "Client ID", "text"), ("igdb_client_secret", "Client secret", "secret")]},
    "ss":        {"label": "ScreenScraper",
                  "fields": [("ss_dev_id", "Dev ID", "text"), ("ss_dev_password", "Dev password", "secret"),
                             ("ss_softname", "Softname", "text"),
                             ("ss_user", "User", "text"), ("ss_pass", "Password", "secret")]},
    "emumovies": {"label": "EmuMovies",
                  "fields": [("emu_user", "User", "text"), ("emu_pass", "Password", "secret")]},
}

_cache: dict | None = None


def _db_creds(db) -> dict:
    row = db.get(Setting, "credentials")
    return dict(row.value) if row and row.value else {}


def invalidate():
    global _cache
    _cache = None


def cred(key: str) -> str:
    """Effective value: DB override, else .env (config). Never empty-crashes."""
    global _cache
    if _cache is None:
        from app.db.session import SessionLocal
        with SessionLocal() as db:
            _cache = _db_creds(db)
    v = _cache.get(key)
    return v if v not in (None, "") else (getattr(settings, key, "") or "")


def masked(db) -> dict:
    """Per-field {set, value|masked} for the UI — secrets never sent in full."""
    dbc = _db_creds(db)
    out: dict = {}
    for meta in CRED_FIELDS.values():
        for key, _label, kind in meta["fields"]:
            eff = dbc.get(key) or getattr(settings, key, "") or ""
            src = "ui" if dbc.get(key) else ("env" if getattr(settings, key, "") else None)
            if kind == "secret":
                out[key] = {"set": bool(eff), "masked": ("••••" + eff[-4:]) if len(eff) >= 4 else ("••••" if eff else ""), "source": src}
            else:
                out[key] = {"set": bool(eff), "value": eff, "source": src}
    return out


def save_creds(db, values: dict) -> None:
    """Blank value = keep existing; '__CLEAR__' = remove the DB override."""
    row = db.get(Setting, "credentials")
    cur = dict(row.value) if row and row.value else {}
    for k, v in (values or {}).items():
        if v is None or v == "":
            continue
        if v == "__CLEAR__":
            cur.pop(k, None)
        else:
            cur[k] = v
    if row:
        row.value = cur
    else:
        db.add(Setting(key="credentials", value=cur))
    db.commit()
    invalidate()
