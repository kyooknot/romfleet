#!/usr/bin/env bash
# Build `chd_extract_cd` — a libchdr-based CD-frame (cdzs/cdlz/cdzl/cdfl codec) CHD
# extractor. Companion to `chd_extract` (build.sh): that tool only handles raw/DVD-type
# CHDs where concatenated hunks == the disc image. CD-type CHDs interleave a 96-byte
# subcode block after every 2352-byte sector (CD_FRAME_SIZE=2448); this tool strips the
# subcode and writes a flat single-track .bin that rcheevos' default cdreader can read
# directly (no synthesized .cue needed — see extract_chd_cd.c for details).
#
# Installs to /usr/local/bin/chd_extract_cd. Re-run after an OS/toolchain change.
# Requires: git, cmake, gcc. (apt-get install -y cmake)
set -euo pipefail

WORK="${WORK:-/tmp/libchdr}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

command -v cmake >/dev/null || { echo "installing cmake..."; apt-get update -qq && apt-get install -y cmake; }

if [ ! -d "$WORK/build" ]; then
  rm -rf "$WORK"
  git clone --depth 1 https://github.com/rtissera/libchdr.git "$WORK"
  cmake -S "$WORK" -B "$WORK/build" -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF
  cmake --build "$WORK/build" -j"$(nproc)"
fi

gcc -O2 -I"$WORK/include" "$SRC_DIR/extract_chd_cd.c" \
  "$WORK/build/libchdr-static.a" \
  "$WORK"/build/deps/zstd-*/libzstd.a \
  "$WORK"/build/deps/lzma-*/libchdr-lzma.a \
  "$WORK"/build/deps/miniz-*/libminiz.a \
  -lm -o /usr/local/bin/chd_extract_cd

echo "installed: $(/usr/local/bin/chd_extract_cd 2>&1 | head -1 || true) -> /usr/local/bin/chd_extract_cd"
