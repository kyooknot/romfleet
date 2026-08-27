# RomFleet — Architecture

A **central server holds the one canonical, RA-gated ROM library** (inventory, hashes, metadata, media,
BIOS, saves). Each enrolled machine is a **client** that receives a *projection* of that library in its
frontend's format. One source of truth, many frontend adapters. The user curates a system centrally, then
**Releases** it to the fleet.

> Full brief: see `docs/build-brief.md`. This file is the distilled architecture + the decisions taken.

## Decisions (2026-07-09)

| Choice | Decision | Rationale |
|---|---|---|
| Repo | Its own repo | Clean separation from the two private predecessors it borrows from: a gaming-rig repo (EmuMovies scripts, LaunchBox ground truth) and an earlier ROM scanner (hasher + RA-client modules). Neither is public; nothing here depends on them at runtime. |
| Phase-1 scope | **Vertical slice on Genesis** first, then fan out to all ~41 cartridge systems | De-risk the whole pipeline end-to-end on one system before generalizing. |
| Hash index | **Keep Elasticsearch** | Reuse the existing RA-hash index approach from `raRomScannerApp`. |
| Server deploy | **New pve LXC (`romfleet`)** | Production-ish infra from the start; Postgres + ES + Redis + FastAPI + Celery live there. |
| Server stack | Python + FastAPI + Celery + Postgres + ES | Lets us lift `raRomScannerApp` modules almost directly. |
| Scraper spine | ScreenScraper (canonical) + EmuMovies (video) | Frontend-agnostic + RA-aware; LBDB is a validated supplement (from the 2026-07 PS2 work). |
| Agent (Phase 2) | Single static Go binary, Syncthing embedded | No runtime deps on immutable/overlay OSes. |

## Guiding principles (non-negotiable)

1. **RA is the gate.** Only games with a RetroAchievements hash are inventoried as library games. Non-matching
   files are identified for info only, never released.
2. **DATs are enrichment, not the filter.** No-Intro / Redump / MAME / **Lost Level Archive** identify, name,
   region-tag, and source the correct file. Membership = "has an RA hash."
3. **Server-authoritative for content; non-destructive on agents.** Remote frontend files are **merged** — the
   app owns only entries it created (tagged by source); user/emulator content is preserved.
4. **Dry-run + backup + rollback for every remote mutation.** Sacred — the user has repeatedly hand-reverted
   experiments.
5. **LAN-only** for agent/library/save traffic (Steam Deck local-download is the only future off-LAN case).
6. **Wrap solved problems, don't reinvent.** Saves = Syncthing, embedded in and supervised by the agent.

### Cartridge hashing nuance (matters for the ingest design)

"Cartridge" is **two** hasher classes, branch on `hash_method`:
- **`raw`** (36 systems: Genesis, GB/GBC/GBA, SMS, GG, Virtual Boy, …): the RA hash **is** the raw-file MD5,
  so the RA gate and the DAT-`md5` identification **collapse into one MD5 lookup**.
- **header-normalizing** (`nes`: iNES header stripped; `snes`: copier header + interleave): run the RA hasher
  first, *then* match. The DAT `md5` ≠ raw-file MD5 for these.

## Data model (Postgres, authoritative)

`System` · `Game` (RA game id = anchor) · `RomFile` (path/size/hashes/DAT-id/match-state → satisfies a Game;
priority scorer picks the **primary**) · `MediaAsset` (typed, canonical, once) · `BiosFile` (first-class,
hash-verified) · `SaveArtifact` (battery saves only — no states) · `Machine` (enrolled agent + profile) ·
`Release` (per-system **versioned** manifest of RomFile+Media+Bios with hashes).

Canonical on-NFS layout uses **Batocera folder names** from `systems.yaml` as the neutral convention.

## Ingest → curate → release pipeline (Phase-1 heart)

`scan → hash (RA hasher) → RA gate → DAT identify (incl. Lost Level Archive) → prioritize (region/rev/
translation scorer) → tidy → scrape (ScreenScraper + EmuMovies) → UI edit → Release (versioned manifest)`.

## Sync model (Phase 2+, designed now)

**RomFleet fleet traffic is server → agent only.** No agent→server data channel:
- **Play stats** come from the RA API (server-side), pushed down as metadata.
- **Save states** don't exist (RA hardcore forbids them). Battery saves ride **Syncthing** (embedded in the
  agent, REST-driven, server runs an always-on introducer hub; mesh data paths). RomFleet only configures &
  monitors it — never merges saves.

## Reuse map (§3 of the brief)

Ported into this repo (adapt imports during the slice build):
- `backend/app/hashers/__init__.py` — RA hashers (**crown jewel**)
- `backend/app/core/hash_priority.py` — region→rev→translation scorer
- `backend/app/core/elasticsearch.py`, `services/ra_client.py`, `services/sync.py`, `services/m3u.py`
- `config/systems.yaml` — 68-system registry (extend with media/DAT/frontend fields)

Reference (stay in the private gaming-rig repo): `scripts/emumovies-*.ps1`, `ra-playtime-sync.md`,
`launchbox-bigbox-kiosk.md`.

## Phased plan

- **Phase 1 (now):** canonical server + curation UI; ingest steps 1–7; Release versioning + **stable
  agent-facing API/manifest** interface. No agents, no remote writes.
- **Phase 2:** Go agent + embedded Syncthing; enroll/profile; pull-a-Release (deltas); RA play stats;
  introducer fan-out; deprovisioning.
- **Phase 3:** frontend adapters — LaunchBox/BigBox (non-destructive XML merge), then Batocera.
- **Phase 4:** disc systems (CHD/RVZ, M3U), ES-DE, Steam Deck local-download + VPN.

## Non-goals for Phase 1

No agents / remote writes / frontend XML; no disc conversion / multi-disc / arcade; no off-LAN; no save-sync
protocol (wrap Syncthing); no save states ever.
