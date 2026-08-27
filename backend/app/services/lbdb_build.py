"""Build the LaunchBox GamesDB SQLite index from the public Metadata.zip.

LaunchBox publishes its metadata as raw XML (https://gamesdb.launchbox-app.com/Metadata.zip);
the app imports it into a local SQLite db. RomFleet does the same so it self-sources the
GamesDB (no dependency on any LaunchBox install). We stream Metadata.xml (~500 MB) with
iterparse into the exact schema `scraper_lbdb.LbdbScraper` queries.

CompareName is stored as the raw Name; the scraper normalizes both sides with the same
`_norm`, so matching is self-consistent.
"""
from __future__ import annotations
import sqlite3
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
import structlog

log = structlog.get_logger()

SCHEMA = """
DROP TABLE IF EXISTS Games;
DROP TABLE IF EXISTS GameAlternateTitles;
DROP TABLE IF EXISTS GameImages;
CREATE TABLE Games (
  DatabaseID INTEGER PRIMARY KEY, Name TEXT, CompareName TEXT, Platform TEXT,
  ReleaseDate TEXT, Overview TEXT, Developer TEXT, Publisher TEXT,
  Genres TEXT, MaxPlayers TEXT, ESRB TEXT, CommunityRating TEXT, ReleaseType TEXT);
CREATE TABLE GameAlternateTitles (
  AlternateName TEXT, DatabaseID INTEGER, Region TEXT, AltNameCompareValue TEXT);
CREATE TABLE GameImages (
  FileName TEXT, DatabaseId INTEGER, Type TEXT, Region TEXT, CRC32 TEXT);
"""
INDEXES = """
CREATE INDEX ix_games_platform ON Games(Platform);
CREATE INDEX ix_alt_dbid ON GameAlternateTitles(DatabaseID);
CREATE INDEX ix_img_dbid ON GameImages(DatabaseId);
"""


def build_lbdb_db(zip_path: str, db_path: str) -> dict:
    tmp = db_path + ".building"
    Path(tmp).unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    cur = con.cursor()

    games: list = []
    alts: list = []
    imgs: list = []
    n_g = n_a = n_i = 0

    def flush():
        nonlocal n_g, n_a, n_i
        if games:
            cur.executemany("INSERT OR REPLACE INTO Games VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", games)
            n_g += len(games); games.clear()
        if alts:
            cur.executemany("INSERT INTO GameAlternateTitles VALUES (?,?,?,?)", alts)
            n_a += len(alts); alts.clear()
        if imgs:
            cur.executemany("INSERT INTO GameImages VALUES (?,?,?,?,?)", imgs)
            n_i += len(imgs); imgs.clear()

    with zipfile.ZipFile(zip_path) as z, z.open("Metadata.xml") as f:
        context = ET.iterparse(f, events=("start", "end"))
        _, root = next(context)
        processed = 0
        for ev, el in context:
            if ev != "end":
                continue
            tag = el.tag
            if tag == "Game":
                d = {c.tag: c.text for c in el}
                did = d.get("DatabaseID")
                if did:
                    name = d.get("Name") or ""
                    games.append((int(did), name, name, d.get("Platform"), d.get("ReleaseDate"),
                                  d.get("Overview"), d.get("Developer"), d.get("Publisher"),
                                  d.get("Genres"), d.get("MaxPlayers"), d.get("ESRB"),
                                  d.get("CommunityRating"), d.get("ReleaseType")))
            elif tag == "GameAlternateName":
                d = {c.tag: c.text for c in el}
                did, an = d.get("DatabaseID"), d.get("AlternateName")
                if did and an:
                    alts.append((an, int(did), d.get("Region"), an))
            elif tag == "GameImage":
                d = {c.tag: c.text for c in el}
                did, fn = d.get("DatabaseID"), d.get("FileName")
                if did and fn:
                    imgs.append((fn, int(did), d.get("Type"), d.get("Region"), d.get("CRC32")))
            else:
                continue
            el.clear()
            processed += 1
            if processed % 20000 == 0:
                flush()
                root.clear()   # free processed siblings — keeps memory bounded
    flush()
    cur.executescript(INDEXES)
    con.commit()
    con.close()
    Path(tmp).replace(db_path)   # atomic swap in
    result = {"games": n_g, "alt_titles": n_a, "images": n_i, "db": db_path}
    log.info("LBDB build complete", **result)
    return result
