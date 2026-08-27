# RomFleet — application image.
#
# ⚠️ The four external binaries are the whole reason this file is non-trivial. RomFleet
#    shells out to rahash / chdman / 7z / chd_extract; pip supplies none of them, and
#    without rahash the app runs perfectly and matches nothing. Baking them in is the main
#    advantage of the container path over bare metal.
#
# Build:  docker build -t romfleet .
# Run:    see docker-compose.yml — the app alone is not enough (Postgres + ES + Redis).

# ── Stage 1: compile chd_extract against libchdr ──────────────────────────────
# Needed for zstd-compressed CHDs, which chdman < 0.264 cannot read. Built here so the
# runtime image carries no toolchain.
FROM debian:trixie-slim AS chdbuild
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY tools/chd_extract/ /build/chd_extract/
RUN git clone --depth 1 https://github.com/rtissera/libchdr.git /build/libchdr \
 && cmake -S /build/libchdr -B /build/libchdr/build \
          -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
 && cmake --build /build/libchdr/build -j"$(nproc)" \
 && gcc -O2 -I/build/libchdr/include /build/chd_extract/extract_chd.c \
        /build/libchdr/build/libchdr-static.a \
        /build/libchdr/build/deps/zstd-*/libzstd.a \
        /build/libchdr/build/deps/lzma-*/libchdr-lzma.a \
        /build/libchdr/build/deps/miniz-*/libminiz.a \
        -lm -o /usr/local/bin/chd_extract

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim-trixie

# RAHasher release to bake in. Pinned deliberately: "latest" would make two builds of the
# same commit differ. Bump intentionally.
ARG RAHASHER_VERSION=1.8.4

RUN apt-get update && apt-get install -y --no-install-recommends \
        mame-tools p7zip-full curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# rahash — RAHasher from RALibretro. NOTE the lowercase install name: every call site
# invokes `rahash`, while the archive contains bin64/RAHasher. A correct binary under the
# wrong name fails exactly like a missing one.
RUN curl -fsSL -o /tmp/rahasher.zip \
      "https://github.com/RetroAchievements/RALibretro/releases/download/${RAHASHER_VERSION}/RAHasher-x64-Linux-${RAHASHER_VERSION}.zip" \
 && unzip -j -o /tmp/rahasher.zip 'bin64/RAHasher' -d /tmp \
 && install -m 755 /tmp/RAHasher /usr/local/bin/rahash \
 && rm -f /tmp/rahasher.zip /tmp/RAHasher

COPY --from=chdbuild /usr/local/bin/chd_extract /usr/local/bin/chd_extract

# Fail the BUILD if any tool is missing, rather than discovering it during an ingest that
# silently matches nothing.
RUN for t in rahash chdman 7z chd_extract; do \
      command -v "$t" >/dev/null || { echo "MISSING TOOL: $t"; exit 1; }; \
    done && echo "all four external tools present"

WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt
COPY . /app

RUN useradd -r -u 10001 -d /app romfleet \
 && install -d -o romfleet -g romfleet /var/lib/romfleet
USER romfleet

WORKDIR /app/backend
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
