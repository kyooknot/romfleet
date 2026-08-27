# Public-release readiness

Audit of this repo before sharing it outside its original deployment, and what was done
about each finding. Two questions: **would a fresh deployment work**, and **does anything
here belong only to the original author**.

This document is deliberately free of the specific hostnames, IPs and account names it
originally listed — naming them here would have re-introduced exactly what it asks you to
remove.

---

## ✅ Fixed

**Install instructions now exist.** There were none — the README pointed at a `deploy/`
directory of runbooks that did not exist. See [`INSTALL.md`](INSTALL.md), written to be
followed literally, with a verify step per stage and a *"failures that look like success"*
table.

**`.env.example` was actively misleading.** It documented 9 keys the app does not read and
omitted 17 it does, so following it produced a silently misconfigured install. Regenerated
from `config.py` and checked against it programmatically.

**Nothing warned you when required credentials were missing.** Every setting defaults to
`""`, so an unconfigured install started cleanly, matched nothing, and reported no error.
`Settings.missing_required()` now exists and importing the config emits a `RuntimeWarning`
naming the unset variables.

**A licence is in place (MIT) and third-party terms are documented** —
[`licensing.md`](licensing.md) is explicit that MIT covers the code and grants nothing over
DATs, scraped metadata, or ROMs. [`dat-providers.md`](dat-providers.md) covers where to get
DATs and which are safe to fetch automatically.

**Hardware sizing is documented** from a measured install rather than guesswork —
`INSTALL.md` §0.5.

**Third-party DATs are no longer vendored.** ~8.5 MB of Lost Level Archive DATs were
committed here. That project is public but **declares no licence**, so redistribution is
not granted. They are removed and gitignored — and it costs nothing, because
`app/services/lla_fetch.py` already pulls the current set weekly. `INSTALL.md` step 6
covers the first fetch.

**Deployment-specific identity removed from the code.** The original machine's name was
hardcoded as the default endpoint in four modules; the endpoint config with its host and
key path was tracked in git; private IPs and an internal hostname appeared in the README
and in comments. Now: `DEFAULT_ENDPOINT` is a setting (default `desktop`),
`config/endpoints.yaml` is gitignored with a validated `endpoints.yaml.example` shipped in
its place, and the remaining references are generic.

**Install-prefix independence.** Ten tools hardcoded `/opt/romfleet` in `sys.path`,
`os.chdir` and log paths. All now resolve relative to `__file__`, so the tree works under
any prefix.

**The README described a layout that did not exist** — a `frontend/` React + Vite app
(there is none; the UI is a single `backend/app/web/index.html` served at `/static`) and a
`deploy/` runbook directory (it held only SSH keys). Corrected.

---

## 🔴 Blocks a fresh deployment

### Dependencies are entirely unpinned
All 15 entries in `backend/requirements.txt` are `>=` ranges. Two installs months apart get
different versions. Ship a lockfile, or accept that "works on my machine" is load-bearing.
This is the last thing standing between the repo and a reproducible install.

*(The `rahash` provenance gap that sat here is **resolved** — it is RAHasher, shipped
prebuilt with every RALibretro release. `INSTALL.md` §2.5 has the download, the lowercase
install name, and a verified interface check.)*

---

## ⚠️ Still needs a human decision

### Rotate the fleet deploy key
A live ED25519 **private key** sits in `deploy/endpoints/<name>/` on the deployed server,
used to push releases to a fleet machine.

**It has never been committed** — verified across the full history
(`git log --all --diff-filter=A` for `*.env`, `id_*`, `*.pem`, `*.key` returns nothing) —
and `.gitignore` now blocks `deploy/endpoints/**/id_*`. But it lived for months inside a
directory the README told people to expect in the repo, one `git add -f` from publication.

**Recommended:** rotate it before publishing, and move endpoint keys out of the repo tree
entirely (`/var/lib/romfleet/endpoints/<name>/`, as `endpoints.yaml.example` now shows).
Requires access to the endpoint machine's `authorized_keys`, so it is not automatable from
here.

### ~~Choose a licence~~ — done
**MIT**, see [`../LICENSE`](../LICENSE) and the rationale in [`licensing.md`](licensing.md).
AGPL-3.0 was the alternative considered. Swapping is cheap while one person holds copyright
in the tree; it gets expensive once contributors arrive.

### History rewrite — probably unnecessary, worth a decision
No secret was ever committed, but earlier commits do contain the original deployment's
hostnames and private (RFC1918) IPs. Those are not credentials and are meaningless outside
that LAN. Rewriting history to purge them is possible but disruptive; the alternative is to
accept them. **Recommendation: accept.** They disclose nothing exploitable.

---

## Corrections to the first pass of this audit

- **`config/systems.yaml` defines 69 systems, not 1.** The first pass counted top-level
  YAML keys, and the file is a single `systems:` key holding a 69-entry list. The README's
  "68 systems" was substantially right.
- The DAT concern was initially recorded as "terms unaddressed". The actual finding is
  sharper: the upstream project declares **no licence at all**.

---

## Verifying the repo is clean

```bash
# no secret ever committed, on any branch
git log --all --diff-filter=A --name-only --pretty=format: | sort -u \
  | grep -iE 'id_ed25519|\.env$|\.pem$|\.key$'        # expect no output

# no deployment-specific identity left in the tree
grep -rIn --exclude-dir=.git -E '<your-host>|<your-ip-prefix>' .

# the shipped endpoint example still matches the code that loads it
python3 - <<'PY'
import yaml, sys; sys.path.insert(0, "backend")
from app.services.push_launchbox import Endpoint
d = yaml.safe_load(open("config/endpoints.yaml.example"))
print({n: Endpoint(name=n, **c) for n, c in d["endpoints"].items()} and "example ok")
PY
```

The last one matters: `load_endpoints()` does `Endpoint(name=<key>, **cfg)`, so a stale
example raises `TypeError` for whoever copies it. It is worth re-running whenever the
`Endpoint` dataclass changes.
