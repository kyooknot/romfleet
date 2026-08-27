# RomFleet

RA-gated ROM **fleet manager**: one canonical, server-authoritative library → projected to each machine's
frontend (LaunchBox/BigBox, Batocera, later ES-DE). Curate a system centrally, then **Release** it to the fleet.

- **Vision & full spec:** [`docs/build-brief.md`](docs/build-brief.md)
- **Architecture & decisions:** [`ARCHITECTURE.md`](ARCHITECTURE.md)
- **Deploy (Docker / bare metal / Proxmox):** [`docs/DEPLOY.md`](docs/DEPLOY.md)
- **Install detail + hardware sizing:** [`docs/INSTALL.md`](docs/INSTALL.md)
- **DAT providers:** [`docs/dat-providers.md`](docs/dat-providers.md)
- **Licence:** MIT — [`LICENSE`](LICENSE), rationale in [`docs/licensing.md`](docs/licensing.md)
- **Notifications:** [`docs/notifications.md`](docs/notifications.md)
- **Before sharing this repo:** [`docs/PUBLIC-RELEASE.md`](docs/PUBLIC-RELEASE.md)

## Status

**Phase 1 — canonical server + curation UI.** Building the ingest pipeline as a *vertical slice on Genesis*
first, then fanning out to all ~41 cartridge systems.

- [x] Repo scaffold + ported logic modules (hashers, hash-priority, RA client, sync, ES, m3u)
- [x] Host container: Postgres (UTF-8) + Elasticsearch 8 + Redis, app deployed to the install prefix — see [`docs/INSTALL.md`](docs/INSTALL.md)
- [x] Postgres schema (System / Game / RomFile) via SQLAlchemy
- [x] **Ingest slice PROVEN**: `scan → RawHasher (RA MD5) → RA gate (ES) → persist` — Genesis 40/40 matched end-to-end
- [ ] DAT identify (Lost Level Archive — vendored) → prioritize → tidy
- [ ] Scrape (LaunchBox GamesDB spine + EmuMovies + TheGamesDB)
- [ ] Web UI (browse + edit matches/metadata/media, BIOS verify)
- [ ] Release versioning + manifest + stable agent-facing API
- [ ] Fan out from the Genesis slice to all ~41 cartridge systems

**CLI (on the LXC):** `python -m app.cli {init-db | sync-ra --console N | ingest --system <folder>}`

## Layout

```
config/systems.yaml      canonical system registry
backend/app/
  hashers/               RA hashers (ported — crown jewel)
  core/                  hash_priority (scorer), elasticsearch, config, dat_sources
  services/              ra_client, sync (RA DB), dat, ingest, scraper, discord_notify
  db/                    SQLAlchemy models + session
  api/                   FastAPI routes
  workers/               Celery
  web/                   the UI — index.html + art, served at /static by main.py
scripts/                 operational scripts (nightly sourcing sweeps, onboarding)
dats/                    vendored Logiqx DATs (No-Intro / Lost Level Archive)
tests/                   standalone tests (no DB, no network)
```

> ⚠️ Earlier revisions of this file described a `frontend/` React + Vite app and a
> `deploy/` directory of provisioning runbooks. **Neither exists.** The UI is the single
> `backend/app/web/index.html`, and there are currently **no install instructions in the
> repo** — see [`docs/PUBLIC-RELEASE.md`](docs/PUBLIC-RELEASE.md).

## Ported from (do not reinvent)

An earlier private ROM-scanner project (hasher + RA-client logic modules) and a private
gaming-rig repo (EmuMovies scripts, RA-playtime, LaunchBox ground-truth docs). Both stay
put as reference; nothing here depends on them at runtime.
