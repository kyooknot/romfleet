# Atari Jaguar CD — storage & hashing findings

RA console **77** ("Atari Jaguar CD"). A **separate system** on RetroAchievements, LaunchBox,
*and* Batocera — Batocera added a dedicated `jaguarcd` system (`/userdata/roms/jaguarcd`), so it
is **not** merged with cartridge Jaguar (console 17). RomFleet folder: `jaguarcd`.

## Emulators (this drives every storage decision)

| Emulator | RetroAchievements | Notes |
|---|---|---|
| **Virtual Jaguar (BizHawk core)** | ✅ **the only RA-supported Jaguar CD emulator** | earns achievements |
| BigPEmu | ❌ no RA support yet (dev left scripting hooks) | Batocera's `jaguarcd` emulator |
| Virtual Jaguar (libretro) | ❌ | cannot emulate Jaguar CD at all |

So: **BizHawk earns achievements; Batocera plays via BigPEmu.** A library serving both must use a
format both accept.

## Format compatibility — why we store RAW (no compression)

| Format | BizHawk (RA) | Batocera / BigPEmu | RA-hashable |
|---|---|---|---|
| `.cue` + `.bin` | ✅ | ✅ | ✅ |
| `.cdi` | ✅ | ✅ | ✅ |
| `.7z` / `.zip` | ✅ | ❌ | (extract) |
| `.bigpimg` | ❌ | ✅ (BigPEmu-only) | ❌ (changes identity) |
| `.chd` | ❌ | ❌ | n/a |

**Jaguar CD is the one CD system with no viable compression.** CHD works in neither emulator;
BizHawk compresses via 7z, BigPEmu via `.bigpimg`, and those are mutually exclusive. The only
cross-compatible storage is the **uncompressed** `.cue`/`.bin` + `.cdi`. (System is tiny — the
whole set is ~1.4 GB — so raw costs nothing.)

### Folder structure
```
jaguarcd/
├── <game>.cdi                 # single-file homebrew images at the root
├── <game>/                    # multi-track games each in their own subfolder
│   ├── <game>.cue             #   (BizHawk/BigPEmu launch the .cue; .bins travel with it)
│   └── <game> (Track N).bin
```
Ingest indexes the launchable `.cue`/`.cdi` only (extensions = `.cue`, `.cdi`); `.bin` files are
cue dependencies, not indexed separately.

## Hashing (`hash_method: jaguarcd` → `JaguarCDHasher`)

RA's Jaguar CD hash is custom (rcheevos `rc_hash_jaguar_cd`): a header block in sector 1 locates
the boot code, which is hashed; if that equals the shared homebrew bootloader hash
`254487b59ab21bc005338e85cbf9fd2f`, it instead hashes the real game code in track 2 (KART header).

- `.cue`/`.bin` → `rahash 77` (the installed binary handles these).
- `.cdi` **v6** → the installed `rahash` binary **fails** (predates v6 CDI support), so
  `JaguarCDHasher` falls back to a pure-Python port, `_hash_cdi_v6_jaguar` in
  `backend/app/hashers/__init__.py`, which mirrors rcheevos. Older CDI → `rahash 77`.
- No `rahash` rebuild was needed — the Python path covers the gap.

## Result (2026-07)

All **18** of the user's games matched RA's 18-game set (10 `.cue`/`.bin` + 8 `.cdi`), **100%**,
0 unmatched. Stored raw in canonical `roms/jaguarcd/`; source master copy retired per the
per-system migration policy. The RA Jaguar CD library is homebrew/aftermarket-heavy.
