"""itch.io as a sourcing source for homebrew / aftermarket ROMs.

itch.io hosts a great deal of modern homebrew that exists nowhere else — no archive.org item,
no No-Intro entry — which is most of what RomFleet still cannot find for GB/GBC/SMS/GG/NES.

What the platform does and does not give us (checked against the docs, 2026-08-05):

  * The server-side API (api.itch.io) is scoped ENTIRELY to your own account — /profile,
    /profile/games, /games/:id/download_keys, /games/:id/purchases, /wharf/latest. There is no
    public search, no browse, and no upload listing, even for games you own. So the API is
    useless for finding somebody else's homebrew.
  * itch.io publishes NO file hashes anywhere. Unlike archive.org — where a published md5 lets
    us pick the right file without downloading — every itch candidate must be downloaded and
    hashed. That is fine here only because homebrew ROMs are 128 kB - 6 MB; it would be
    unacceptable for discs.

So this uses the two mechanisms that DO work: RSS tag feeds and the HTML search for discovery,
then the browser download flow to resolve a file. That flow, verified end to end:

    GET  <game>                          -> csrf_token
    POST <game>/download_url             -> {"url": signed download page}
    GET  <signed page>                   -> data-upload_id + names + sizes
    POST <game>/file/<upload_id>         -> {"url": direct file URL}   <- NOT the signed path

The last step is the one that is easy to get wrong: posting to `<signed page>/file/<id>` 404s;
it must be posted to the GAME url while holding the download session's cookies.
"""
from __future__ import annotations

import re
import time
from urllib.parse import quote_plus, urlparse

import structlog

log = structlog.get_logger()

BASE = "https://itch.io"
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/120.0.0.0 Safari/537.36")
# ROM extensions worth pulling, by RA console id. itch pages usually ship a Windows build
# alongside the ROM, and downloading a 5 MB .exe to hash it is pure waste.
_ARCHIVE = (".zip", ".7z", ".rar")

ROM_EXT = {
    4: (".gb",), 6: (".gbc", ".gb"), 5: (".gba",), 7: (".nes",), 3: (".sfc", ".smc"),
    11: (".sms",), 15: (".gg",), 1: (".md", ".gen", ".bin"), 8: (".pce",),
    28: (".vb",), 13: (".lnx",), 25: (".a26", ".bin"), 27: (".zip",),
    # Arduboy (71) and Uzebox (80) were absent, so rom_candidates() returned on its first line
    # and the caller reported "no candidate file found in any configured source" — which reads
    # as "this does not exist anywhere" rather than "we never looked". Both are itch-first
    # platforms: an .arduboy file is itself a zip, and .hex is the raw build (2026-08-16).
    71: (".arduboy", ".hex", ".zip"),
    80: (".uze", ".zip"),
}
_TAG = {4: "gameboy", 6: "game-boy-color", 5: "gameboy-advance", 7: "nes",
        3: "snes", 11: "sega-master-system", 15: "game-gear", 1: "sega-genesis",
        28: "virtual-boy", 8: "pc-engine", 13: "atari-lynx", 25: "atari-2600",
        71: "arduboy", 80: "uzebox"}


# itch.io rate-limits hard and fast — roughly a dozen requests in a few minutes is enough to
# start getting 429s and CSRF-less pages. Every fetch goes through _get so a 429 backs off
# rather than being read as "this game does not exist".
_MIN_GAP = 1.5
_last_hit = [0.0]


def _session():
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": _UA})
    return s


def _get(s, url, **kw):
    """GET with polite spacing and 429 backoff. Returns None once it gives up."""
    for attempt in range(4):
        gap = time.time() - _last_hit[0]
        if gap < _MIN_GAP:
            time.sleep(_MIN_GAP - gap)
        r = s.get(url, timeout=kw.pop("timeout", 40), **kw)
        _last_hit[0] = time.time()
        if r.status_code != 429:
            return r
        wait = int(r.headers.get("retry-after") or (10 * (attempt + 1)))
        log.info("itch rate-limited, backing off", url=url[:70], wait=wait)
        time.sleep(min(wait, 60))
    return None


def _csrf(html: str) -> str | None:
    for pat in (r'csrf_token"\s+value="([^"]+)"',
                r'name="csrf_token"[^>]*value="([^"]+)"',
                r'"csrf_token"\s*:\s*"([^"]+)"'):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def search_games(title: str, limit: int = 6) -> list[str]:
    """Candidate itch.io game pages for a title, via the HTML search."""
    s = _session()
    try:
        r = _get(s, f"{BASE}/search?q={quote_plus(title)}")
        if r is None or r.status_code != 200:
            log.info("itch search failed", status=(r.status_code if r else "rate-limited"),
                     title=title[:50])
            return []
        urls = re.findall(r'href="(https://[a-z0-9_-]+\.itch\.io/[a-z0-9_-]+)"', r.text, re.I)
    except Exception as e:  # noqa
        log.info("itch search error", err=str(e)[:120]); return []
    out, seen = [], set()
    for u in urls:
        if u not in seen:
            seen.add(u); out.append(u)
        if len(out) >= limit:
            break
    return out


def browse_tag(console_id: int, limit: int = 40) -> list[str]:
    """Game pages from a platform tag's RSS feed — the one documented machine-readable
    discovery route (append .xml to any browse URL)."""
    tag = _TAG.get(console_id)
    if not tag:
        return []
    s = _session()
    try:
        r = _get(s, f"{BASE}/games/tag-{tag}.xml")
        if r is None:
            return []
        links = re.findall(r"<link>(https://[a-z0-9_-]+\.itch\.io/[^<]+)</link>", r.text, re.I)
    except Exception as e:  # noqa
        log.info("itch tag feed error", tag=tag, err=str(e)[:120]); return []
    return links[:limit]


def uploads(game_url: str, session=None) -> tuple[list[dict], object, str | None]:
    """(uploads, session, csrf) for a game page. Each upload: {id, name, size_text}.

    Returns the live session because the file-resolution POST must reuse its cookies.
    """
    s = session or _session()
    try:
        rg = _get(s, game_url)
        if rg is None:
            return [], s, None
        h = rg.text
        csrf = _csrf(h)
        if not csrf:
            log.info("itch: no csrf (rate limited?)", game=game_url); return [], s, None
        r = s.post(f"{game_url}/download_url", data={"csrf_token": csrf}, timeout=40)
        if r.status_code != 200:
            return [], s, csrf
        page = _get(s, r.json()["url"])
        if page is None:
            return [], s, csrf
        s.headers["Referer"] = r.json()["url"]
        ids = re.findall(r'data-upload_id="(\d+)"', page.text)
        names = re.findall(r'<strong class="name" title="([^"]+)"', page.text)
        sizes = re.findall(r'<span class="file_size"><span>([^<]+)</span>', page.text)
        out = []
        for i, uid in enumerate(ids):
            out.append({"id": uid,
                        "name": names[i] if i < len(names) else "",
                        "size_text": sizes[i] if i < len(sizes) else ""})
        return out, s, csrf
    except Exception as e:  # noqa
        log.info("itch uploads error", game=game_url, err=str(e)[:140])
        return [], s, None


def resolve_file(game_url: str, upload_id: str, session, csrf: str) -> str | None:
    """Direct, time-limited URL for one upload. Must POST to the GAME url, not the signed page."""
    try:
        r = session.post(f"{game_url}/file/{upload_id}", params={"source": "game_download"},
                         data={"csrf_token": csrf}, timeout=40)
        if r.status_code == 200 and "json" in (r.headers.get("content-type") or ""):
            return r.json().get("url")
        log.info("itch file resolve failed", status=r.status_code, upload=upload_id)
    except Exception as e:  # noqa
        log.info("itch file resolve error", err=str(e)[:120])
    return None


def rom_candidates(title: str, console_id: int, limit_games: int = 4):
    """Yield (direct_url, filename, display) for uploads that LOOK like a ROM for this console.

    Yields a resolved, directly-downloadable URL so the caller can reuse the ordinary download
    + RA-hash-verify path; itch publishes no hashes, so verification is necessarily
    download-then-hash.
    """
    exts = ROM_EXT.get(console_id) or ()
    if not exts:
        return
    for game in search_games(title, limit=limit_games):
        ups, s, csrf = uploads(game)
        if not ups or not csrf:
            time.sleep(2)
            continue
        for u in ups:
            nm = (u.get("name") or "").lower()
            if not any(e in nm for e in exts) and "rom" not in nm:
                continue        # skip the Windows/Linux builds
            url = resolve_file(game, u["id"], s, csrf)
            if not url:
                continue
            fn = re.sub(r'[<>:"/\\|?*]', "-", u.get("name") or f"itch-{u['id']}")
            # Do NOT rename an archive to a ROM extension. itch homebrew is usually shipped as
            # a zip ("rom.zip"), and appending the console ext produced "rom.zip.gbc" — the
            # hasher then read a ZIP as a raw cart image and got a hash that matched nothing,
            # reported as "archives have a different dump". Every hasher already handles
            # archives, so leave them named as what they are (Loveless Losers, 2026-08-16).
            if not fn.lower().endswith(_ARCHIVE) and not any(
                    fn.lower().endswith(e) for e in exts):
                fn += exts[0]
            log.info("itch candidate", game=game, upload=u.get("name"), size=u.get("size_text"))
            yield url, fn, u.get("name") or fn
        time.sleep(2)           # be a polite guest; itch rate-limits quickly
