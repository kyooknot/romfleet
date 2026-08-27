# FBNeo Arcade Audit — v1.0.0.03 (build 2025-11-19)

_Audit of the local arcade collection vs the FBNeo core installed on the LaunchBox endpoint. 2026-07-16._

## Installed core (LaunchBox endpoint)
- `<LaunchBox>\Emulators\RetroArch\cores\fbneo_libretro.dll` — 56 MB, built **2025-11-19**, reports **v1.0.0.03**.
- It's the **libretro/FBNeo** fork build (not standalone finalburnneo/FBNeo, whose last tag v1.0.0.2 is 2021). DAT state = commit `01e29d50` (2025-11-18); header self-IDs as "FinalBurn Neo v1.0.0.03 Arcade Games" — exact match.

## 1. What we have that's truly FBNeo
- `arcade/` **503 zips → 492 in the FBNeo DAT**, 11 not (won't run in FBNeo).
- `naomi/` (10), `naomi2/` (4), `atomiswave/` (6) → **0 in FBNeo** — FBNeo doesn't emulate that hardware. Need **Flycast** (or Demul/Redream), NOT FBNeo.

**11 arcade/ files NOT in FBNeo** (MAME-only or malformed hack filenames): `hotd2` (NAOMI), `gunsur2` (NAOMI), `mslug6` (Atomiswave), `arcadedkongotr`, `arcadedkongx11`, `arcadekof98cb`, `arcadetimesup`, `kofnw`, and 3 with descriptive filenames+spaces that can never match a romname: `mslug5ex Metal Slug 5 (Extend Ver.zip`, `sfz3mix (Street Fighter Zero 3 Mix v0.zip`, `umk3p Ultimate Mortal Kombat 3 Plus (Hack, Beta 2).zip`.

## 2. Version match — 24/25 sample aligned to this build
Read each zip's chip CRC32s vs the DAT. **24 full matches, 1 mismatch.** The collection is current.
- Only mismatch: **`gradius3j`** = an OLDER dump (sound ROMs `945_s12/s13` → this build renamed to `945_312/313`; our CRCs now belong to the split clone `gradius3ja`). Fix: re-pull `gradius3j`, or rename to `gradius3ja.zip`.
- No systematic drift; expect only a small long tail of `gradius3j`-style renames.

## 3. FBNeo version installed
**v1.0.0.03, 2025-11-19 build** (see above). Our romsets match this version's DAT.

## 4. Adjustments needed
**A. Remove from FBNeo routing:** the 20 NAOMI/NAOMI2/Atomiswave zips (→ Flycast) + the 11 MAME-only/malformed arcade zips.

**B. Missing BIOS — HARD BLOCKER (currently absent everywhere):** our Neo Geo/PGM sets are **split** (game chips only) and won't boot without:
- **`neogeo.zip`** — required for all **98** Neo Geo games. Without it, zero Neo Geo games run.
- **`pgm.zip`** — required for PGM games (`kov`, `dmnfrnt`, `ket`, `espgal`).
- CPS3 clones need their **parent** present: `sfiii3nr1`/`sfiii4n` need `sfiii3.zip` (we don't have it). Verify split Neo Geo clones have parents (e.g. `aof2a`→`aof2`).

**C. Re-dump:** `gradius3j` (only one found in sample; a full CRC pass recommended).

**D. RA-hashing implication:** RA arcade identity is `MD5(romname)` — filename-only, content-agnostic. So achievements resolve for any of the 492 regardless of dump version (even `gradius3j`). But **RA identity ≠ runnability**: a game runs only if its chips match the v1.0.0.03 DAT AND its BIOS/parent zips are present. Practical blockers are the missing BIOS and the CRC renames — not the hash layer.

## Sub-system classification (492 in-DAT arcade games)
| Sub-system | Count | Routing target (RA console) |
|---|---:|---|
| Other/generic arcade | 204 | Arcade (27) |
| **Neo Geo** | **98** | **Neo Geo (142)** — un-mix from arcade/ |
| CPS2 | 35 | Arcade/CPS2 (27) |
| Taito | 33 | Arcade (27) |
| Sega (System 16/18/OutRun…) | 32 | Arcade (27) |
| Konami | 32 | Arcade (27) |
| CPS1 | 21 | Arcade/CPS1 (27) |
| Cave | 17 | Arcade (27) |
| CPS3 | 7 | Arcade/CPS3 (27) |
| Psikyo | 6 | Arcade (27) |
| PGM | 4 | Arcade (27) |
| Toaplan | 3 | Arcade (27) |

Per-bucket romname lists saved on the server at `/tmp/fbneo_audit/bucket_*.txt`. The 98 Neo Geo romnames (to move out of `arcade/`, they're RA console 142) are listed there and in the audit thread.

**BIOS/device sets:** FBNeo defines 13 (`neogeo, pgm, cchip, decocass, isgsm, midssio, namcoc69/70/75, nmk004, skns, ym2608, bubsys`) — none present in our folders.

## Routing plan (RomFleet)
- **Neo Geo** → separate `neogeo` system (RA console 142, separate gate). Move the 98 Neo Geo zips from `arcade/` into `neogeo/`.
- **NAOMI / NAOMI 2 / Atomiswave** → already separate folders; keep as separate LB/ES systems on Flycast. They share RA console 27, so RomFleet needs a **library-split** (md5-of-romname set per folder) so each folder only claims its slice of the console-27 gate.
- **Arcade (generic)** → the remaining ~394 under `arcade` (console 27). Optional finer LB/ES splits (CPS1/2/3, Sega, Konami…) per the table.
- Requires the RA gates for console 27 (Arcade) and 142 (Neo Geo) to be synced first (currently rate-limited).
