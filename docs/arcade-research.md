# RomFleet Arcade Support — Research Report

_Prepared overnight 2026-07-15 as prep for arcade (MAME + FBNeo) implementation. Research only — no code changed._

## Executive summary

Arcade is fundamentally different from every console RomFleet handles today, but the good news is that **RA's arcade identity is the simplest hash in the whole system**: for console 27 the "hash" is just `MD5(romname)` — the MD5 of the zip's base filename with the extension stripped. Content bytes are never read. RomFleet already has an `ArcadeHasher` (shells `rahash 27`) and stub `arcade`/`naomi`/`atomiswave` systems in `config/systems.yaml`, so the plumbing exists. What's missing is (a) a **DAT-driven catalog** so we know the universe of romnames and their pretty titles, and (b) **romname-keyed scraping** (ArcadeDB + ScreenScraper `romnom`).

---

## 1. Arcade ROM identification fundamentals

### The MAME model (vs RomFleet's one-file-one-hash model)

RomFleet's console model is: **one file → one whole-file (or RA-normalized) hash → one game**. Arcade breaks that at the identity layer:

- **A "game" is a `.zip` containing many individual ROM chips**, each identified by its own **CRC32 + SHA1**, declared in a DAT.
- **There is no single canonical whole-zip hash.** Two verified-good copies of `sf2.zip` can have different zip-file MD5s yet be byte-identical in the chips that matter. "Is this romset correct?" is a **set-membership question against a DAT**, not a file-hash comparison.
- **Parent/clone + merge modes.** Games have a *parent* (`sf2`) and *clones* (`sf2ce`, `sf2j`). Shared chips can be stored **non-merged** (self-contained), **split** (clone references parent), or **merged** (parent + clones in one zip). Plus **BIOS sets** (`neogeo.zip`, `pgm.zip`, `cps3.zip`) and **device sets** that are required but are *not games*.
- **DAT files** (`mame -listxml` output, or clrmamepro/no-intro-style XML) enumerate every set, its ROMs (name/size/crc/sha1), `romof`/`cloneof`/`isbios`/`isdevice`, and the `<description>` (pretty title).

**Implication:** identification should be **DAT-set membership keyed by romname**, with optional per-chip CRC verification, rather than a whole-file hash. The "hash" survives only for the RA gate, where it degenerates into `MD5(romname)`.

### How RetroAchievements hashes arcade (console 27) — exact algorithm

Confirmed from RA docs and `rc_hash_arcade`:

1. Take the **base filename**, strip directory + extension → `sf2.zip` → `sf2`.
2. **MD5 those bytes.** `RA_hash("sf2.zip") = MD5("sf2")`. **Case-sensitive** (MAME romnames are lowercase, so a non-issue).
3. **Zip contents are never opened.** CHD-based arcade (`kinst`, `sfiii3`) hashes the same way — `MD5("sfiii3")` — the CHD is irrelevant to identity. Sidesteps all CHD-hashing complexity.
4. **Software-list folder prefixes.** `rc_hash_arcade` prepends a lowercased parent-dir + `_` **only** for a fixed set of MAME/MESS console software-list folder names (`nes`, `fds`, `sms`, `msx`, `ngp`, `pce`, `sgx`, `tg16`, `neocd`, `coleco`, `sg1000`, `genesis`, `gamegear`, `megadriv(e)`, `pcengine`, `channelf`, `spectrum`, `supergrafx`, `zxspectrum`, `mastersystem`, `colecovision`). **True arcade has no prefix** — plain `MD5(romname)`. `neogeo`, `nss`, `playchoice`, `stv` are NOT in the list.

**Bottom line:** gating arcade like a console is trivial. `API_GetGameList(console_id=27, h=1)` returns every achievement-having arcade game with hash = `MD5(romname)` and hash `Name` = the romname. Existing `sync.py` ingests exactly this shape. `ArcadeHasher.hash_file` (rahash 27) already produces the right key.

---

## 2. ArcadeDB (adb.arcadeitalia.net) — ArcadeDatabase by motoschifo

Purpose-built arcade scraper API, keyed off **MAME romnames**. Base: `https://adb.arcadeitalia.net/service_scraper.php`. UTF-8 JSON, top-level `release` + `result` array. **Attribution required** ("Arcade Database by motoschifo").

### Commands (`ajax=`)
| `ajax` | Purpose | Params |
|---|---|---|
| `query_mame` | Full metadata + media + video | `game_name` (`;`-separated), `use_parent`, `resize`, `lang` |
| `query_mame_like` | Fuzzy similar-romset lookup | `game_name` |
| `query_mame_media` | Media only | `game_name`, `resize` |
| `download_status` | Bandwidth/quota state | — |

### Key params
- **`game_name`** — MAME romname(s), `;`-separated (`atetris;mslug`).
- **`use_parent=1`** — resolve clone → parent (media usually filed under parent). Important for clone-heavy FBNeo sets.
- **`resize`** — `0` | `100h|200h|300h` | `100w|200w|300w`.
- **`lang`** — `it` | `en`.

### Example requests
```
https://adb.arcadeitalia.net/service_scraper.php?ajax=query_mame&game_name=sf2
https://adb.arcadeitalia.net/service_scraper.php?ajax=query_mame&game_name=atetris;mslug&resize=300h
https://adb.arcadeitalia.net/service_scraper.php?ajax=query_mame&game_name=mslug2t&use_parent=1
```

### Response shape (live `game_name=sf2`)
Metadata: `game_name`, `title` ("Street Fighter II: The World Warrior (World 910522)"), `cloneof`, `year`, `manufacturer`, `genre`, `players`, `nplayers` ("2P sim"), `rate`, `status` ("GOOD"), `serie`, `languages`, `emulator_name`, `history` (long bio). Technical: `screen_orientation`, `screen_resolution`, `input_controls`, `input_buttons`, `buttons_colors`.

**Media URL fields** (mostly PNG): `url_image_ingame` (snap), `url_image_title`, `url_image_marquee`, `url_image_cabinet`, `url_image_flyer`, `url_image_cpanel`, `url_image_pcb`, `url_image_bezel`, `url_image_boss`, `url_image_logo` (wheel), `url_image_box`, `url_image_score`, `url_image_select`, `url_image_versus`, `url_image_gameover`, `url_icon`, `url_manual` (PDF), video: `youtube_video_id`, `url_video_shortplay`, `url_video_shortplay_hd`.

### Rate limits / etiquette
- **Single connection per IP** — must be single-threaded (no concurrent fan-out).
- GET URLs capped ~800 chars (batch via `;`, or POST).
- Media downloads have per-IP daily/global/weekly/monthly quotas — query `ajax=download_status`.

Maps onto RomFleet media types: marquee→`logo`/marquee, `title`→`titlescreen`, `ingame`→`screenshot`, `flyer`→`boxfront`, `cabinet`→new `cabinet` type, `cpanel`→control-panel, `shortplay*`→`video`.

---

## 3. MAME metadata sources

Primary catalog: `mame -listxml`. Per `<machine>`: `name` (romname), `cloneof`/`romof`/`isbios`/`isdevice`, **`<description>` (pretty title — the romname→title map)**, `<year>`, `<manufacturer>`, `<rom>` (name/size/crc/sha1/status), `<input players>`/`<control type>`, `<display>`, `<driver status="good|imperfect|preliminary">`, `<disk>` for CHD games.

Supplementary (progetto-SNAPS / progettosnaps.net, mirrored on GitHub/pleasuredome):
- **history.dat / history.xml** — descriptions/bios.
- **catver.ini** — category/genre (de-facto genre source).
- **catlist.ini / genre.ini** — alternate categories.
- **nplayers.ini** — players + sim/alt.
- languages.ini, mameinfo.dat, command.dat, gameinit.dat.

**romname → title**: use listxml `<description>` (identical to ArcadeDB's `title`).

---

## 4. FBNeo specifics

- **Romnames ~99% shared with MAME** — so RA arcade hash (`MD5(romname)`) is **emulator-agnostic**.
- **FBNeo has its own DAT** (`finalburnneo/FBNeo` → `dats/`), a strict subset of MAME + a few FBNeo-only entries (HBMAME hacks, some console romsets).
- **FBNeo-focused collection** = smaller, curated playable classics (CPS1/2/3, Neo Geo, System 16, Konami), audited via FBNeo DAT.
- Treat MAME and FBNeo as **two DAT catalogs over the same romname keyspace**. Gate against RA 27 (shared). Neo Geo is a **separate lane** — RomFleet has `neogeo` on **RA console 142**, distinct from general Arcade 27. Keep separate.

---

## 5. Existing scraper arcade support

| Scraper | Arcade support | Match key |
|---|---|---|
| ArcadeDB (new) | Excellent (purpose-built) | **romname** |
| ScreenScraper | Strong | **romname** (`romnom`, systemeid **75**) |
| EmuMovies | Strong (media) | **romname/filename** |
| LaunchBox GamesDB | Moderate | title (needs DAT title first) |
| IGDB | Weak | title |
| TheGamesDB | Weak | title |

- **ScreenScraper**: MAME/Arcade platform `systemeid=75` (also Neo-Geo 142, CPS1 6/CPS2 7/CPS3 8, Naomi 56, Atomiswave 53, Model2 54, Model3 55). **FBNeo has no distinct ID — scrapes under MAME 75.** Match by **`romnom=<romname>`** (skip the md5 path — SS stores real file hashes, not RA's `MD5(romname)`). Change: add `27: 75` to `SS_SYSTEM`, branch arcade to `romnom`.
- **LaunchBox GamesDB**: "Arcade" platform, but `scraper_lbdb.py` is title-keyed → needs DAT title first. Add `27:"Arcade"` to `LBDB_PLATFORM`. Lower hit-rate.
- **EmuMovies**: romname/filename-keyed, excellent arcade media (marquees/videos/snaps) — strong secondary media source.
- **IGDB/TGDB**: title-keyed, weak arcade coverage — last-resort metadata fill.

---

## 6. Recommendation

### 6a. Two layers: gate + catalog
1. **RA gate (achievements) — reuse existing machinery.** RA console 27 via `API_GetGameList(i=27, h=1)` flows through `sync.py`→ES. Each hash = `MD5(romname)`, hash `Name` = romname (**capture it — the join key to everything**). Neo Geo stays on 142.
2. **DAT catalog — new.** RA gate only covers achievement games; ingest a **MAME `-listxml` and/or FBNeo DAT** for the full universe, pretty titles, clone structure, BIOS/device sets, per-chip CRC/SHA1. Analogous to the existing No-Intro/LLA `dat_source.py`/`dat.py` pipeline but parses `<machine>` + nested `<rom>`/`<disk>` + clone attrs.

### 6b. Identification — DAT membership + romname hash (NOT whole-zip hash)
- Keep `ArcadeHasher` (rahash 27) as the **gate key** producer (`MD5(romname)`).
- Add **DAT-set membership** as catalog identity: local `sf2.zip` identified if `sf2` ∈ active DAT; flag `has_achievements = MD5("sf2") ∈ gate(27)`.
- Optional **per-chip CRC/SHA1 verification** = arcade analog of "hash verified" (catches bad dumps / wrong merge mode).
- **Carry romname explicitly; never rename arcade files to pretty titles** (RA hash + every scraper key off the filename). Add `romname` to RomFile/Game or derive at ingest.

### 6c. Scraping — romname-keyed, into existing architecture
- **New `scraper_arcadedb.py`** mirroring `scraper_ss.py`: `query_mame&game_name=<romname>&use_parent=1`, map `url_image_*`→media types, `genre`/`players`/`nplayers`/`manufacturer`/`year`/`history`→metadata. **Serialize (single connection)**, quota-aware, attribution.
- **Extend `scraper_ss.py`**: `SS_SYSTEM[27]=75`; console 27 → `romnom=<romname>`.
- **EmuMovies** by romname for extra marquees/videos.
- **LBDB/IGDB/TGDB** title-fill after DAT gives a title.
- Priority: **ArcadeDB → ScreenScraper → EmuMovies → LBDB/IGDB/TGDB**.

### 6d. Metadata pipeline
DAT `<description>`→title; **catver.ini**→genre; **nplayers.ini**→players; **history.dat/xml**→description; listxml `<manufacturer>`/`<year>`→dev/publisher/date; `<driver status>`→emulation-quality flag (hide `preliminary`). ArcadeDB returns most pre-joined → ArcadeDB-first minimizes INI parsing; keep progetto-SNAPS INIs as offline/bulk source.

### 6e. Biggest decisions & gotchas
1. **Filename discipline is load-bearing.** RA identity = `MD5(basename)`. Arcade must be **exempt from canonical title-renaming**.
2. **Merge mode (split/merged/non-merged).** Doesn't affect RA hashing, but affects whether clones boot (need parent zip) and per-chip verification. Pick + document a target (non-merged simplest; split = community default).
3. **BIOS/device sets** (`neogeo.zip`, `pgm.zip`, `cps3.zip`, qsound) are required but NOT games — filter `isbios`/`isdevice` out of the game list (like central-BIOS handling).
4. **Clones + 1G1R.** Arcade "1G1R" = pick parent (or preferred regional clone) per `cloneof`. `use_parent=1` for scraping. Achievements usually attach to the **parent**.
5. **CHD-based arcade** (`kinst`, `sfiii3`): identity still `MD5(romname)` — **no CHD hashing**. But deploy must ship `<romname>/*.chd` alongside `<romname>.zip`. `naomi`/`naomi2` already list `.chd`.
6. **MAME vs FBNeo catalog + Neo Geo lane.** systems.yaml says FinalBurn Neo; `archives.json` sources a FBNeo set. Keep `neogeo`=142 separate from Arcade 27. Shared romnames → shared gate; per-chip verification needs emulator-specific DAT.
7. **ArcadeDB throughput.** Single-connection + media quotas → serialized, quota-aware worker (unlike parallelizable console scrapers).

### Files to touch (implementation map)
- `config/systems.yaml` — arcade/naomi/etc. present; keep `neogeo`=142 separate; arcade files named by romname.
- `backend/app/hashers/__init__.py` — `ArcadeHasher` already correct; no identity change.
- **New** `backend/app/services/scraper_arcadedb.py` — ArcadeDB client (romname-keyed, serialized, attribution).
- `backend/app/services/scraper_ss.py` — `SS_SYSTEM[27]=75`; arcade → `romnom`.
- `backend/app/services/scraper_lbdb.py` — `27:"Arcade"` (title fallback).
- **New/extended DAT parser** (alongside `dat.py`/`dat_source.py`) — parse `mame -listxml`/FBNeo `<machine>` (romname, description, cloneof, isbios/isdevice, `<rom>` CRC/SHA1, `<disk>`).
- `backend/app/db/models.py` — add `romname` to RomFile/Game (shared join key).
- `backend/app/data/archives.json` — already has `arcade` (fbneo_RetroRusty), `naomi`, `atomiswave`.

### Key sources
- RA game-identification (arcade = MD5 of romname, case-sensitive): https://docs.retroachievements.org/developer-docs/game-identification.html
- `rc_hash_arcade` + software-list prefix rules: https://github.com/RetroAchievements/rcheevos (`src/rhash/`)
- ArcadeDB scraper API: https://adb.arcadeitalia.net/service_scraper.php
- ScreenScraper systems (MAME=75, Neo-Geo=142, CPS 6/7/8, Naomi 56, Atomiswave 53): https://www.screenscraper.fr/systemesinfos.php
- progetto-SNAPS (history.dat, catver.ini, nplayers.ini, DATs): https://www.progettosnaps.net/support/ , /catver/ , /dats/
- FBNeo DATs: https://github.com/finalburnneo/FBNeo (`dats/`)

**Codebase anchors:** `hashers/__init__.py` (`ArcadeHasher` ~L401), `config/systems.yaml` (arcade/naomi/naomi2/atomiswave L334–726), `scraper_ss.py` (`romnom` path L78–113), `scraper_lbdb.py`, `sync.py` (gate ingest `API_GetGameList` h=1), `ra_client.py` (`get_game_list`), `archives.json` (`arcade`/`naomi`/`atomiswave`).
