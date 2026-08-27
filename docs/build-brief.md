# Build Prompt — RA-Gated ROM Fleet Manager (working name: RomFleet)

> This is the authoritative project brief, preserved verbatim. `ARCHITECTURE.md` distills it plus the
> decisions taken on 2026-07-09.

## 1. Mission

A **central server holds the one canonical ROM library** — inventory, hashes, metadata, and media — and each
**enrolled machine is a client** that receives a *projection* of that library in whatever format its frontend
expects. One source of truth, many frontend adapters.

The user maintains a system's library centrally until it's in a good state, then **"releases"** that system
to the fleet with a button; every enrolled agent then pulls the update so all machines share the same games,
media, metadata, and saves.

Target frontends (priority order): 1) Windows LaunchBox/BigBox, 2) Linux Batocera, 3) (later) SteamOS ES-DE.

## 2. Guiding principles (non-negotiable)

- **RA is the gate.** Only games with RetroAchievements integration belong in the library. A file that doesn't
  match an RA hash is *not* inventoried as a game.
- **DATs are enrichment, not the filter.** No-Intro/Redump/MAME/Lost Level Archive identify, name, region-tag,
  and source the correct file. Membership = "has an RA hash."
- **Server-authoritative for content; non-destructive on agents.** Writes to remote frontend config by
  merging — owns only entries it created (tagged by source), preserves user/emulator content.
- **Dry-run + backup + rollback for every remote mutation.** Every remote action reversible. (The user has
  repeatedly had to hand-revert experiments; sacred.)
- **LAN-only** for agent/library/save traffic. Only off-LAN case: a Steam Deck downloading games locally (later).
- **Don't reinvent solved problems; wrap them.** Saves = Syncthing, embedded in and supervised by the agent —
  one install + one enrollment; the endpoint never configures Syncthing. RomFleet library traffic stays
  unidirectional server → agent; Syncthing saves are a separate peer-to-peer transport the agent manages.

## 3. Reuse from existing repos

From `raRomScannerApp` (`backend/app/…`): `hashers/__init__.py` (per-family RA hashers — crown jewel, port
faithfully; Phase 1 needs cartridge/raw hashers), `core/hash_priority.py` (region→rev→translation→label
scorer), `services/ra_client.py`, `services/sync.py` + `core/elasticsearch.py` (RA DB pull/index),
`config/systems.yaml` (canonical registry — keep schema, extend), `services/m3u.py` (multi-disc, defer).

From the private gaming-rig repo: `scripts/emumovies-videos.ps1`/`emumovies-arcade.ps1` (working EmuMovies API — reuse
as supplementary video source), `ra-playtime-sync.md` (RA measured-playtime model for the play-stats channel),
`launchbox-bigbox-kiosk.md` (LaunchBox ground truth — read before the LaunchBox adapter in Phase 3).

Treat ROM/BIOS/save data as the only thing to preserve; port the logic modules as reference.

## 4. Core data model (server, authoritative — Postgres)

`System` · `Game` (RA game id anchor; canonical descriptive metadata) · `RomFile` (path/size/hashes RA-MD5 +
DAT CRC32/MD5/SHA1, DAT identification, match state, satisfied Game; scorer picks primary) · `MediaAsset`
(typed: boxfront/boxback/titlescreen/screenshot/logo/marquee/video/manual; canonical once; adapters map naming)
· `BiosFile` (first-class, hash-verified, per-system distribution) · `SaveArtifact` (battery saves; states out)
· `Machine` (enrolled agent: token, frontend, OS, profile) · `Release` (per-system versioned manifest of
RomFile+Media+Bios with hashes).

Canonical on-NFS layout = Batocera folder names from `systems.yaml`.

## 5. Ingest → curate → release pipeline (Phase-1 heart)

1. Scan NFS ROM dir for a system. 2. Hash each file with the correct RA hasher. 3. **RA gate**: MD5 matches a
known RA hash? no → not a library game (record for info); yes → in. 4. **Identify & name** via DATs (all four
in Logiqx XML → one parser). Lost Level Archive covers the RA-only hack/homebrew/translation long tail; its
DATs are keyed by RA console id (filename prefix + `<header><id>` = `ra_console_id`), and for cartridge systems
the DAT `md5` **is** the RA hash → gate + identify collapse to one lookup. Vendor DATs into the repo. 5.
Prioritize candidates per Game → primary. 6. Dedupe/tidy (hash-verified); defer CHD/RVZ compression. 7. Scrape
metadata + media via ScreenScraper (spine) + EmuMovies (video). 8. Edit in UI (fix match, pick primary, edit
metadata, manage media, resolve unmatched). 9. Release → new version → agents pull (Phase 2+).

## 6. Sync & conflict rules (design now; fleet parts Phase 2+)

Fleet traffic **server → agent only**. Play stats from RA API (server-side, `UserTotalPlaytime`, exact+alias
diacritic-normalized matching per `ra-playtime-sync.md`) pushed down as metadata. Save states out of scope (RA
hardcore forbids). Battery saves = Syncthing.

Server → agent (authoritative): ROMs, BIOS, metadata, media, play stats, folder layout, frontend config
entries. Agent-side scrapers/auto-import disabled.

### 6.1 Saves — embed & supervise Syncthing
Agent bundles the Syncthing **binary** via Go `embed` (not the library — internal packages unstable); automate
via its REST API; pin the version. Launch as managed child (`--no-browser --no-default-folder --home=<dir>`),
health-check/restart, bind GUI/REST to `127.0.0.1` with agent-generated API key, LAN-only
(`globalAnnounceEnabled=false`, `relaysEnabled=false`). Agent creates saves folders at the emulator's expected
path, presets Staggered versioning (`.stversions`), maps each emulator save location into the synced tree.
**Introducer fan-out**: server runs an always-on Syncthing hub marked introducer (authoritative saves +
versioning); agent reports its device id on enrollment → hub adds device + shares folder → introducer
auto-propagates fleet membership. Data paths are peer-to-peer mesh (survive hub offline; only new enrollments
need hub). Deprovisioning explicit (unshare + teardown). Conflict surfacing: agent polls REST for
`.sync-conflict-*`, reports counts to a read-only UI; never auto-merge.

### 6.2 Why Syncthing is safe
Version vectors for causal order (not wall-clock); mtime only a tie-break for true concurrent conflicts
(loser renamed `.sync-conflict-…`, never deleted); Staggered versioning archives superseded versions.

### Remote config writes
Merge never overwrite (own only tagged entries); backup + dry-run/preview + rollback on every write.

### Enrollment & release
Agent registers with token + profile (frontend/OS/systems/direct-NFS-vs-cache/paths). Release is
content-addressed, pull-based: manifest of files+hashes; agent diffs local, fetches deltas (or points frontend
at NFS), runs adapter. One-directional ⇒ re-pull always restores correctness.

## 7. Recommended stack

Server: Python + FastAPI + Celery; Postgres authoritative; **Elasticsearch kept** as RA-hash index (decision).
Scraper: ScreenScraper + EmuMovies (video). Agent: single static Go binary, Syncthing embedded (Windows
service / Batocera `/userdata` + `services/` hook / SteamOS `/home` + user systemd). Frontend: React + Vite.
Canonical folders: Batocera names. Save transport: Syncthing, embedded, REST-driven, server introducer hub.

## 8. Phased plan

- **Phase 1 (build now):** early cartridge systems only. Extended `systems.yaml` + Postgres schema; ingest 1–7;
  NFS organized to Batocera layout; web UI to browse + edit everything (matches, primary ROM, metadata, media,
  unmatched, BIOS verify); **Release** action cutting a versioned per-system manifest; define stable
  agent-facing API + manifest format. No agents.
- **Phase 2:** Go agent + embedded Syncthing; one-step enroll/profile; pull-a-Release (deltas, hash-verified);
  RA play stats; introducer fan-out; deprovisioning. No frontend adapters yet.
- **Phase 3:** frontend adapters — LaunchBox/BigBox (non-destructive XML merge; respect co-run + Automated
  Imports realities), then Batocera (`es_systems.cfg` + `gamelist.xml`). Disable agent-side scraping.
  Dry-run/preview/rollback throughout.
- **Phase 4:** disc systems (CHD/RVZ + path rewrite, M3U), ES-DE adapter, Steam Deck local-download + VPN.

## 9. Non-goals for Phase 1

No agents / remote writes / frontend XML; no disc conversion / multi-disc / arcade/MAME; no off-LAN; no
save-sync protocol (wrap Syncthing); no save states ever; don't preserve old-repo structure beyond ROM/BIOS/
save files + the §3 logic modules.

## 10. Definition of done (Phase 1)

User can: point the server at the NFS store, run ingest for a cartridge system, watch it hash → RA-gate →
DAT-identify → scrape, open the web UI, correct matches/metadata/media, verify BIOS, and click **Release** to
produce a versioned, manifest-backed snapshot — with the agent-facing API and manifest format defined and
stable, ready for Phase 2.

## Open choices — RESOLVED 2026-07-09

1. Hash index: **keep Elasticsearch**. 2. ScreenScraper creds: gitignored `.env` (like `emumovies.env`) —
   *still needed*. 3. Phase-1 systems: **Genesis vertical slice first**, then all ~41 cartridge (36 `raw` +
   NES + SNES). 4. Syncthing topology decided (hub introducer + mesh); confirm server always-on for
   enrollment. Additional: repo = new `~/Documents/claude/romfleet`; deploy = new pve LXC `romfleet`.
