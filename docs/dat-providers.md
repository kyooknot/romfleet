# DAT providers

A DAT is a catalogue of known-good dumps — name, size, CRC/MD5/SHA1 — used to *identify* a
file and name it canonically. RomFleet keeps the **RetroAchievements hash list** as the gate
(a ROM is "real" if RA will award achievements on it) and uses DATs for identification,
naming and completeness.

**RomFleet vendors no DAT files.** They are third-party data under their own terms; several
of the projects below declare no licence at all, so redistributing them inside this repo was
never permitted. Fetch what you need into `DATS_PATH` (default `dats/`).

## What ships wired up

| Source | ID | How | Covers |
|---|---|---|---|
| **RetroAchievements** | `ra` | API, hourly | the achievement gate — not a DAT, but the authority on which hashes count |
| **Lost Level Archive** | `lla` | GitHub, weekly | aftermarket, homebrew, hacks, translations — the long tail No-Intro excludes |
| **No-Intro** | `no-intro` | manual upload | cartridge + handheld, the canonical clean-dump standard |
| **Redump** | `redump` | manual upload | optical disc (PS1/2, Saturn, Dreamcast, GameCube, Wii) |

`ra` and `lla` are *pullable* — the app fetches them itself. No-Intro and Redump have no open
API and must be downloaded by hand.

## Getting the two manual ones

**No-Intro** — <https://datomatic.no-intro.org>. *Download → Daily* gives per-system or
bundled DATs. There is no API and the download is behind a form; scripted fetching is
against the spirit of the site and breaks regularly. Download, unzip into
`dats/no-intro/`, re-run identification.

**Redump** — <http://redump.org/downloads/>. Per-system DAT downloads, plus cuesheet packs
that matter for multi-track discs. Drop into `dats/redump/`.

Refresh both a few times a year; they change slowly.

## Worth adding

Ordered by value-for-effort. Each is a public Git repo, so
[`backend/app/services/lla_fetch.py`](../backend/app/services/lla_fetch.py) is a working
template — it walks a GitHub tree, downloads matching files, and prunes stale versions.
Register a new source in `app/core/dat_sources.py` (`DEFAULTS`, `_LABEL`, `PULLABLE`) and
dispatch it in `app/services/dat_update.py`.

| Provider | Covers | Pullable? |
|---|---|---|
| **libretro-database** <br><https://github.com/libretro/libretro-database> | aggregated DATs + RDB metadata across nearly every system RetroArch supports. Broadest single source. | ✅ GitHub |
| **FinalBurn Neo** <br><https://github.com/finalburnneo/FBNeo> | arcade, `dats/` in-repo. Pairs with the FBNeo core most setups already run. | ✅ GitHub |
| **MAME** <br><https://github.com/mamedev/mame> | arcade, authoritative. The DAT *is* `mame -listxml`; releases ship it. Large — 250 MB+ uncompressed. | ✅ GitHub releases |
| **TOSEC** <br><https://www.tosecdev.org> | home computers and obscure hardware — Amiga, Atari, MSX, CPC, Spectrum. Where No-Intro does not reach. | ❌ manual |
| **Redump cuesheets** <br><http://redump.org/downloads/> | `.cue` files for multi-track discs. Worth it if you keep bin/cue rather than CHD. | ❌ manual |

## Choosing between them

They overlap and disagree. A practical order of preference:

1. **RetroAchievements** decides whether a dump counts at all. Non-negotiable — it is the gate.
2. **No-Intro / Redump** for canonical naming of commercial releases.
3. **Lost Level Archive** for anything aftermarket, homebrew, hacked or translated.
4. **libretro-database** as a broad fallback where none of the above has an entry.
5. **MAME / FBNeo** for arcade only — arcade sets are versioned against the emulator, not
   the hardware, so match the DAT to the core you actually run.

⚠️ **Arcade is genuinely different.** A MAME 0.245 set will not validate against a MAME 0.260
DAT. Pin the DAT to your emulator version rather than always taking the newest, or every
audit will report false misses. `docs/fbneo-arcade-audit.md` is a worked example.

## What these are not

DATs identify *dumps*. They carry no box art, no descriptions, no video. That is the
scrapers' job — LaunchBox GamesDB, ScreenScraper, TheGamesDB, IGDB, EmuMovies — configured
separately in `.env`. And no DAT provider distributes ROMs; they distribute the hashes that
let you tell whether the file you already have is the right one.
