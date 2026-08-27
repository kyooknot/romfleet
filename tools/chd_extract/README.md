# chd_extract — zstd-capable CHD extractor

`chdman` 0.251 (the Debian 12 bookworm package on RomFleet's LXC 100) predates the
**zstd** CHD codec (added ~chdman 0.264). It fails with `Unknown compression type` on
zstd-compressed CHDs. Our PS2 source set is ~78% zstd, so most PS2 discs were silently
unhashable — PS2 onboarding stalled at 150 games instead of ~600+.

`chd_extract` is a ~50-line tool linked against **libchdr** (which bundles zstd/lzma/zlib
and links the host's own glibc, unlike the trixie chdman .deb which needs GLIBC_2.38).
It decompresses all hunks and concatenates them to `logicalbytes` — the raw disc image.

## Scope
- ✅ **raw/DVD-type CHDs** (PS2, GameCube, Wii): concatenated hunks == the disc image.
- ❌ **CD-frame CHDs** (`cdzs`/`cdzl` codecs): need de-interleaving (strip 96-byte subcode,
  emit cue/bin) this tool does NOT do. The ~89 `cdzs` PS2 CD games need chdman ≥0.264 or
  an extended extractor.

## Wiring
`backend/app/hashers/__init__.py::_rahash_chd` calls `chdman info` first; if it returns
nonzero (can't decode the codec) it falls back to `_rahash_chd_libchdr`, which runs
`chd_extract <chd> <img>` then `rahash <console_id> <img>`. Validated: Gran Turismo 4
(USA) extracts to gate MD5 `b7e2e47572f4163a216ea85eed3c6c63` (exact match).

## Build
    sudo bash build.sh      # clones libchdr, builds, installs /usr/local/bin/chd_extract
