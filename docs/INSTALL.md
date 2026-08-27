# Installing RomFleet

Written to be followed literally, by a person or an agent. Every step has a **verify**
command and states what a wrong result means. Do not skip a verify — several failures in
this stack are silent, and the section *"Failures that look like success"* at the end
exists because each one has actually happened.

Placeholders: `<ROOT>` is the install prefix (this document assumes `/opt/romfleet`,
but nothing in the code requires it). `<USER>` is the Unix account that runs the service.

---

## 0. What you are installing

| Piece | Why |
|---|---|
| **PostgreSQL 14+** | authoritative library (systems, games, ROM files, settings) |
| **Elasticsearch 8** | the RetroAchievements hash index — the *gate* every ROM is matched against |
| **Redis** | Celery broker for background work |
| **Python 3.11+** | the app. 3.10 is the floor (`str \| None` syntax); 3.11 is what it runs on |
| A **RetroAchievements API key** | without it nothing can ever match — see step 4 |

Disk: the app itself is small. The canonical library is as large as your ROM collection.

---

## 0.5 Hardware

Sizing below comes from a **measured** production install (LXC on Proxmox, ~10k ROMs across
69 systems), not from guesswork: peak **3.94 GB** RAM over a week, average 3.6 GB, with
Elasticsearch, PostgreSQL, Redis and the app all in the same container. CPU sat near idle
between jobs.

| | Minimum | Recommended | Large collection (30k+ ROMs) |
|---|---|---|---|
| **CPU** | 2 cores | **4 cores** | 6–8 cores |
| **RAM** | 6 GB | **8 GB** | 12–16 GB |
| **System disk** | 20 GB | **40 GB** | 60 GB |
| **Library storage** | size of your collection | + ~10% headroom | separate spindle/pool |

### What actually consumes what

**Elasticsearch is the floor.** It wants ~2 GB of heap before it will behave, and it is why
the minimum is 6 GB rather than 2. Everything else — Postgres, Redis, the FastAPI app — is
comfortable in well under 1 GB each. Below 6 GB total, ES starts refusing allocations during
`sync-ra` and the failure looks like a network timeout.

**CPU is bursty, not sustained.** The measured install averages under 1% and peaks at 3%,
because the work is episodic: hashing during ingest, and disc-image decompression when
sourcing CD-based systems. Extra cores shorten an ingest run; they do nothing the rest of
the time. Two cores is genuinely usable if you are patient.

**Disc systems are the expensive ones.** Hashing a cartridge is a file read. Hashing a
PS2/GameCube/Wii image means decompressing CHD/RVZ first, which is CPU-heavy and needs
scratch space. If your collection is disc-heavy, buy cores before RAM.

**Storage: plan for two copies during ingest.** `SRC_ROMS_PATH` (your existing collection,
mounted read-only) and `CANONICAL_PATH` (the library RomFleet builds) are separate trees.
They can live on the same pool, but the peak requirement is roughly *both at once* until you
retire the source. Everything else — DATs ~1 GB, the LaunchBox metadata DB ~2 GB, media/box
art growing with the library — lives on the system disk unless you point it elsewhere.

**Swap: give it 2 GB.** The measured install has 2 GB and uses about 1 GB of it, mostly
kernel-idle pages, and never thrashes. Zero swap is survivable but makes an ES memory spike
during a large `sync-ra` fatal rather than slow.

### Deployment shape

Nothing requires a container or a VM — it is four ordinary services. The reference install
is a single Proxmox LXC with the ROM library **bind-mounted** rather than copied in, which
keeps the container small and the data outside its lifecycle. A bare-metal box, a VM, or
Docker Compose all work equally well.

**Do not put the ROM library inside the container's own disk.** It will be many terabytes,
and container backups then try to include it.

---

## 1. System packages

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git curl \
                        postgresql redis-server
```

Elasticsearch 8 is not in Debian's repos; install from Elastic's APT repo, or run it in
Docker. It must answer on `http://127.0.0.1:9200`.

**Verify:**
```bash
python3 --version            # expect >= 3.11
pg_isready                   # expect "accepting connections"
redis-cli ping               # expect PONG
curl -s localhost:9200 | head -3   # expect a JSON banner with "number" : "8.x"
```
An Elasticsearch that answers on 9200 but reports **7.x will fail at query time**, not at
startup — the index mappings assume 8.x.

---

## 2. Database

```bash
sudo -u postgres createuser romfleet --pwprompt
sudo -u postgres createdb  romfleet -O romfleet --encoding=UTF8
```

⚠️ **The encoding must be UTF8.** Game titles carry accents, kana and CJK. A database
created with `SQL_ASCII` ingests happily and then throws `UnicodeDecodeError` somewhere
deep in a scrape days later.

**Verify:**
```bash
sudo -u postgres psql -tAc \
  "select datname, pg_encoding_to_char(encoding) from pg_database where datname='romfleet'"
# expect: romfleet|UTF8
```

---

## 2.5 External binaries — **do not skip this**

RomFleet shells out to four command-line tools. They are not Python packages and `pip` will
not bring them. **Without `rahash` nothing can be hashed at all**, which means nothing
matches, which means the whole application does nothing — while appearing to run perfectly.

| Tool | Needed for | Where it comes from |
|---|---|---|
| **`rahash`** | **everything** — 25 call sites; produces the RetroAchievements hash for every ROM | prebuilt binary from RALibretro releases — see below |
| `chdman` | CHD disc images (PS2, GameCube, Wii, CD systems) | `apt-get install mame-tools` |
| `7z` | zipped and 7z-compressed ROMs | `apt-get install p7zip-full` |
| `chd_extract` | zstd-compressed CHDs, which `chdman` < 0.264 cannot read | `sudo bash tools/chd_extract/build.sh` (in this repo) |

```bash
sudo apt-get install -y mame-tools p7zip-full build-essential cmake git
sudo bash tools/chd_extract/build.sh      # builds libchdr + installs /usr/local/bin/chd_extract
```

### Getting `rahash`

`rahash` is **RAHasher**, the RetroAchievements hashing CLI. You do not have to build it —
upstream ships a prebuilt Linux binary with every release. The hashers prepend
`/usr/local/bin` to `PATH` (see `backend/app/hashers/__init__.py`), so install it there.

```bash
# latest RAHasher-x64-Linux asset from the canonical repo
curl -sL "$(curl -s https://api.github.com/repos/RetroAchievements/RALibretro/releases/latest \
  | grep -o 'https://[^"]*RAHasher-x64-Linux[^"]*\.zip')" -o /tmp/rahasher.zip
unzip -j -o /tmp/rahasher.zip 'bin64/RAHasher' -d /tmp
sudo install -m 755 /tmp/RAHasher /usr/local/bin/rahash    # note the lowercase name
```

⚠️ **Install it as lowercase `rahash`.** Every call site invokes `rahash`; the zip contains
`bin64/RAHasher`. A correctly downloaded binary under the wrong name fails exactly like a
missing one.

**Two sources, both GPL-3.0:**

| Repo | Use it when |
|---|---|
| **[RetroAchievements/RALibretro](https://github.com/RetroAchievements/RALibretro)** | **Default.** Canonical upstream, actively maintained, ships `RAHasher-x64-Linux` (~0.5 MB) with each release. |
| [LeXofLeviafan/RAHasher](https://github.com/LeXofLeviafan/RAHasher) | A fork packaged as a standalone hasher-only repo. Same binary, same licence, but it tracks upstream at a lag — prefer upstream unless you specifically want the smaller project. |

*(rcheevos is the achievement **library** RAHasher builds on, not the CLI — don't go looking
for the tool there.)*

**Confirm the interface**, because the app depends on its exact shape:

```bash
rahash                      # usage: RAHasher [-v] [-s systempath] systemid filepath
rahash 1 /path/to/any.md    # a bare 32-char md5 on stdout, exit 0
```

Verified against 1.8.4: for console 1 (Mega Drive, a raw-hash system) the output is
byte-identical to `md5sum` of the same file, and the call signature matches
`subprocess.run(["rahash", <console_id>, <path>])` exactly.

**Verify all four:**
```bash
for t in rahash chdman 7z chd_extract; do
  command -v "$t" >/dev/null && echo "  $t ok" || echo "  $t MISSING"
done
rahash 1 /path/to/any/known/rom      # expect a 32-char md5 on stdout
```

---

## 3. Code and dependencies

```bash
sudo install -d -o <USER> -g <USER> <ROOT>
sudo -u <USER> git clone <REPO_URL> <ROOT>
cd <ROOT>
sudo -u <USER> python3 -m venv venv
sudo -u <USER> ./venv/bin/pip install -r backend/requirements.txt
```

⚠️ **`backend/requirements.txt` pins nothing** — all 15 entries are `>=` ranges, so two
installs months apart get different versions and may not behave the same. If you care about
reproducing a working install, freeze after a good one:
`./venv/bin/pip freeze > backend/requirements.lock` and install from that instead.

**Verify:**
```bash
./venv/bin/python -c "import fastapi, sqlalchemy, elasticsearch, httpx; print('deps ok')"
```

---

## 4. Configuration

```bash
cp .env.example backend/.env
chmod 600 backend/.env
$EDITOR backend/.env
```

**`RA_USERNAME` and `RA_API_KEY` are mandatory.** Get the key from
retroachievements.org → Settings → *Web API Key*. Everything else has a working default;
every scraper credential is optional and simply disables that source when blank.

**Verify:**
```bash
cd <ROOT>/backend
../venv/bin/python -c "
from app.core.config import settings
m = settings.missing_required()
print('MISSING:', m) if m else print('config ok')"
```
Importing the config with `RA_API_KEY` unset emits a `RuntimeWarning`. That warning is the
only thing standing between you and an install that runs perfectly and matches nothing.

---

## 5. Schema and the RA index

```bash
cd <ROOT>/backend
../venv/bin/python -m app.cli init-db
../venv/bin/python -m app.cli sync-ra --console 1     # one console first, ~1 min
```

`sync-ra` pulls RetroAchievements' game + hash list into Elasticsearch. **Nothing can be
ingested for a console until its RA data is synced** — an unsynced console yields zero
matches and no error.

**Verify:**
```bash
curl -s 'localhost:9200/ra_games/_count' | python3 -m json.tool   # count > 0
```

---

## 6. DAT files

RomFleet does **not** ship third-party DATs. Lost Level Archive declares no licence, so
redistributing it here is not granted — the app fetches the current set itself:

```bash
cd <ROOT>/backend
../venv/bin/python -c "
from app.services.lla_fetch import update_lla_dats; print(update_lla_dats())"
```

Thereafter the `lla` source refreshes weekly on its own cadence. No-Intro and Redump have
no open API and must be downloaded by hand into `dats/` if you want them.

**Verify:** `ls dats/lost-level-archive/DATs/*.xml | wc -l` — expect dozens.

---

## 7. Paths

Set in `.env`:

- `SRC_ROMS_PATH` — the collection you ingest **from**. Mount it **read-only**; ingest
  never writes here and a read-only mount makes that guarantee real.
- `CANONICAL_PATH` — the library RomFleet builds and serves. Read-write, and it will grow
  to the size of your collection.

**Verify:**
```bash
test -r "$SRC_ROMS_PATH" && echo "source readable"
touch "$CANONICAL_PATH/.wtest" && rm "$CANONICAL_PATH/.wtest" && echo "canonical writable"
```

---

## 8. Run it

```ini
# /etc/systemd/system/romfleet-api.service
[Unit]
Description=RomFleet API + Web UI
After=network-online.target postgresql.service redis-server.service

[Service]
User=<USER>
WorkingDirectory=<ROOT>/backend
ExecStart=<ROOT>/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now romfleet-api
```

**Verify:**
```bash
systemctl is-active romfleet-api                      # active
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/   # 200
```

The UI is `backend/app/web/index.html`, served at `/static`. There is **no separate
frontend build step** — no npm, no bundler.

---

## 9. First ingest

```bash
cd <ROOT>/backend
../venv/bin/python -m app.cli ingest --system megadrive
```

**Verify:** the run reports matched/unmatched counts, and
`curl -s localhost:8000/api/systems` shows a non-zero owned count.

If **every** file is unmatched, the cause is almost always one of: `sync-ra` was not run
for that console, `RA_API_KEY` is unset, or `SRC_ROMS_PATH` points somewhere empty. Check
in that order.

---

## 10. Optional: fleet endpoints

Only needed to push the library to LaunchBox/Batocera machines.

```bash
cp config/endpoints.yaml.example config/endpoints.yaml
install -d -m 700 /var/lib/romfleet/endpoints/desktop
ssh-keygen -t ed25519 -N "" -C romfleet-deploy@desktop \
  -f /var/lib/romfleet/endpoints/desktop/id_ed25519
# append the .pub to the endpoint's authorized_keys, then edit endpoints.yaml
```

Set `DEFAULT_ENDPOINT` in `.env` to the endpoint name commands should assume.

⚠️ **Generate keys outside the repo tree.** A key under `config/` or `deploy/` is one
`git add -f` from being published. `config/endpoints.yaml` is gitignored for this reason.

**Verify:** `curl -s localhost:8000/api/endpoints` lists your endpoint. A `TypeError` at
startup instead means `endpoints.yaml` has a wrong key — the loader does
`Endpoint(name=<key>, **cfg)`, so the field names are exact.

---

## 11. Optional: Discord notifications

Configured in the **database**, not `.env` — see [`notifications.md`](notifications.md).
Paste a webhook URL in Settings, press Test, then choose which event kinds go where.

---

## Failures that look like success

Read this section. Every entry is silent by default.

| Symptom | Real cause |
|---|---|
| Ingest matches **zero** games, no error | `RA_API_KEY` unset, or `sync-ra` never run for that console |
| A console matches nothing while others work | that console's RA data was never synced |
| `UnicodeDecodeError` days after install | the database was created `SQL_ASCII`, not UTF8 |
| Discord silent | no webhook set, or that event kind disabled — see `notifications.md` |
| A "Sourcing…" card never resolves | the follow-up edit failed (usually a 429). It is logged; check the service journal |
| Scrapers return nothing | their credentials are blank. Optional by design — nothing warns you |
| Every ROM unhashable; ingest matches nothing | **`rahash` is not on PATH** — see §2.5. This is the most likely cause on a fresh install |
| Cartridges hash fine, every disc system fails | `chdman` and/or `7z` missing |
| PS2/GameCube/Wii CHDs fail while other discs work | zstd-compressed CHDs — build `chd_extract` (§2.5) |
| `TypeError` on startup mentioning `Endpoint` | wrong key in `config/endpoints.yaml` |
| `PydanticUserError: A non-annotated attribute was detected` | a bare class attribute was added to `Settings`. Annotate it `ClassVar[...]` — `config.py` is imported by everything, so this fails the whole app at import |
| Pushes go to the wrong machine, or `unknown endpoint 'desktop'` | `DEFAULT_ENDPOINT` in `.env` does not name an entry in `config/endpoints.yaml` |
| Elasticsearch queries fail though `:9200` answers | it is 7.x; 8.x is required |

## Upgrading

```bash
cd <ROOT> && git pull
./venv/bin/pip install -r backend/requirements.txt
sudo systemctl restart romfleet-api
```

There is no migration framework. `init-db` calls SQLAlchemy's `create_all`, which creates
**missing tables** but never alters an existing one — so a release that changes a column
needs hand-written SQL. Read the diff before upgrading. Back up Postgres first:
`sudo -u postgres pg_dump -Fc romfleet > romfleet-$(date +%F).dump`

⚠️ Do **not** dump via `su - postgres -c "pg_dump -Fc" > file`: a login shell writes its
MOTD into the binary stream and the archive is silently corrupt. Use `pg_dump -f`.
