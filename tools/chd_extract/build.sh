#!/usr/bin/env bash
# Build `chd_extract` — a libchdr-based CHD -> raw-image extractor that supports the
# zstd CHD codec (added to chdman ~0.264). The RomFleet host (LXC 100, Debian 12
# bookworm) ships chdman 0.251, which errors "Unknown compression type" on zstd CHDs;
# `_rahash_chd` falls back to this tool for raw/DVD-type CHDs (PS2/GameCube/Wii).
#
# Installs to /usr/local/bin/chd_extract. Re-run after an OS/toolchain change.
# Requires: git, cmake, gcc. (apt-get install -y cmake)
set -euo pipefail

WORK="${WORK:-/tmp/libchdr}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

command -v cmake >/dev/null || { echo "installing cmake..."; apt-get update -qq && apt-get install -y cmake; }

rm -rf "$WORK"
git clone --depth 1 https://github.com/rtissera/libchdr.git "$WORK"
cmake -S "$WORK" -B "$WORK/build" -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF
cmake --build "$WORK/build" -j"$(nproc)"

gcc -O2 -I"$WORK/include" "$SRC_DIR/extract_chd.c" \
  "$WORK/build/libchdr-static.a" \
  "$WORK"/build/deps/zstd-*/libzstd.a \
  "$WORK"/build/deps/lzma-*/libchdr-lzma.a \
  "$WORK"/build/deps/miniz-*/libminiz.a \
  -lm -o /usr/local/bin/chd_extract

echo "installed: $(/usr/local/bin/chd_extract 2>&1 | head -1 || true) -> /usr/local/bin/chd_extract"
