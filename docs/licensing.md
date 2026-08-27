# Licensing

**RomFleet is MIT.** See [`../LICENSE`](../LICENSE).

## Why MIT

The intended use is someone self-hosting this for their own collection, or handing it to a
friend. MIT does that with the least friction: they can run it, fork it, and change it
without owing anyone anything, and there is no obligation for the author to police.

The realistic alternative was **AGPL-3.0** — the choice several self-hosted media tools make
(RomM among them) to ensure a hosted fork stays open. It was not chosen because RomFleet is
not a service anyone is likely to resell, and copyleft here would mostly create obligations
for the friend it is being shared with.

Swapping is cheap while the contributor list is one person: replace `LICENSE`, note the
change, done. It gets expensive later, once other people hold copyright in the tree.

## What the licence does NOT cover

**Only the code in this repository.** Everything RomFleet talks to has its own terms, and
none of them are granted by MIT:

| Thing | Status |
|---|---|
| **DAT files** | Third-party data with their own terms. **None are vendored here** — see [`dat-providers.md`](dat-providers.md). Lost Level Archive in particular declares no licence at all, so redistributing it was never permitted. |
| **RetroAchievements API** | Requires your own API key and is subject to RA's terms. |
| **ScreenScraper / TheGamesDB / IGDB / LaunchBox GamesDB** | Each needs your own account/key; scraped metadata and artwork remain theirs. |
| **EmuMovies** | Accessed through a reverse-engineered API using *your* credentials. Their terms govern the media. |
| **ROMs, BIOS images, box art, video snaps** | Not distributed, not licensed, and not the author's to license. RomFleet indexes and organises files *you already have*. |
| **RAHasher, chdman, 7z, chd_extract** | External command-line tools the operator installs. **RAHasher is GPL-3.0** (from RALibretro), chdman is part of MAME. RomFleet **execs** them as separate processes and links against none of them, so their copyleft does not reach this code — but you are installing GPL software alongside it, which is worth knowing. |

MIT covers the tooling. It grants you nothing over the content the tooling points at.
