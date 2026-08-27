# Deployment paths

Three supported shapes. They differ only in how the four services and the four external
binaries get onto the machine — configuration, first run and verification are identical and
live in [`INSTALL.md`](INSTALL.md).

| | Docker Compose | Bare metal / VM | Proxmox LXC |
|---|---|---|---|
| **Effort** | lowest | highest | medium |
| **External binaries** | baked into the image | **you install all four by hand** | you install all four by hand |
| **Upgrades** | rebuild image | `git pull` + pip | `git pull` + pip |
| **ROM library** | bind-mounted volume | native path | **bind mount from the host** |
| **Best when** | you want it working today | you already run Postgres/ES | you already run Proxmox |
| **Tested** | ⚠️ **not yet** — see below | ✅ | ✅ reference install |

> ⚠️ **The Docker path is written but unverified.** `Dockerfile` and `docker-compose.yml`
> are committed and the reasoning behind each line is documented, but they have **not been
> built or run** — the machine they were authored on has no Docker. Treat the first build as
> something to debug, not something to trust. The bare-metal and LXC paths are both in
> production use.

Whichever you pick, read [`INSTALL.md` §0.5](INSTALL.md) for sizing first. The floor is
**6 GB RAM**, and it is Elasticsearch's ~2 GB heap that sets it.

---

## A. Docker Compose

The only path where the external binaries come for free — the image installs `rahash`,
`chdman`, `7z` and builds `chd_extract`, and **the build fails if any of them is missing**
rather than letting you discover it during an ingest that silently matches nothing.

```bash
git clone <REPO_URL> romfleet && cd romfleet
cp .env.example .env
$EDITOR .env          # RA_USERNAME, RA_API_KEY, SRC_ROMS_PATH, CANONICAL_PATH are required

# Elasticsearch will not start without this:
sudo sysctl -w vm.max_map_count=262144
echo 'vm.max_map_count=262144' | sudo tee /etc/sysctl.d/99-romfleet.conf

docker compose up -d --build
docker compose exec app python -m app.cli init-db
docker compose exec app python -m app.cli sync-ra --console 1
```

**Verify:**
```bash
docker compose ps                       # all four healthy
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/    # 200
docker compose exec app rahash          # prints RAHasher usage
```

Notes:
- `SRC_ROMS_PATH` is mounted **read-only**. Ingest never writes to your source collection and
  the `:ro` flag makes that a guarantee rather than a promise.
- `.env` is read by compose *and* by the app. Keep it mode 600.
- Compose pins Elasticsearch **8.x** — the client requires it (`elasticsearch>=8.0,<9.0`).
  A 9.x image will start and then fail at query time.

---

## B. Bare metal or VM

Most control, most work: you install and maintain Postgres, Elasticsearch, Redis, Python
*and* the four external binaries yourself.

Follow [`INSTALL.md`](INSTALL.md) start to finish — it is written for exactly this shape, with
a verify step per stage. **Do not skip §2.5**; `rahash` is the one that silently breaks
everything.

Suits you if you already run these services and would rather add a tenant than another
container stack.

---

## C. Proxmox LXC

The reference install. An **unprivileged** container is fine; nothing here needs privilege.

```
Debian 13, 4 cores, 8 GB RAM, 40 GB rootfs, features: nesting=1
```

Then follow [`INSTALL.md`](INSTALL.md) inside the container — from the app's point of view an
LXC is bare metal.

**Bind-mount the ROM library from the host rather than copying it in:**

```bash
pct set <VMID> -mp0 /pool/roms,mp=/mnt/src/roms,ro=1,backup=0
pct set <VMID> -mp1 /pool/romfleet,mp=/mnt/romfleet,backup=0
```

- `ro=1` on the source enforces read-only at the container boundary.
- **`backup=0` matters.** Without it `vzdump` tries to back up your entire ROM collection
  every night alongside a 40 GB rootfs.
- A container that will not start after adding a mount usually has a **missing host
  directory** — `pct start <VMID> --debug` is the only thing that says so plainly.

Unprivileged containers map UIDs, so a bind-mounted directory must be owned by the mapped
range (`100000:100000`) or writes fail with permission errors that name a UID you have never
seen.

---

## What none of these do for you

- **The ROM library is yours.** RomFleet indexes and organises files you already have; it
  ships no ROMs, no BIOS images, no artwork.
- **DATs are fetched, not vendored** — see [`dat-providers.md`](dat-providers.md).
- **Credentials are per-install.** A RetroAchievements API key is mandatory; every scraper is
  optional and silently inert when blank.
