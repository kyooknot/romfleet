/* CHD (cdzs/cdzl/cdfl/cdlz CD-frame codecs) -> raw single-track .bin extractor,
 * using libchdr (the same zstd-capable build that powers `chd_extract`).
 *
 * CD-type CHDs store CD_FRAMES_PER_HUNK (8) frames per hunk, each frame being
 * CD_FRAME_SIZE (2448) bytes = a 2352-byte raw sector + a 96-byte subcode block.
 * `chd_extract`'s flat hunk concatenation therefore interleaves subcode into the
 * sector stream and produces garbage for these CHDs. This tool decompresses each
 * hunk via chd_read(), then for every frame writes only the leading 2352-byte
 * sector payload (dropping the subcode), sequentially, for the first data track
 * (per CDROM_TRACK_METADATA2/1_TAG "FRAMES:" count, to also drop CD_TRACK_PADDING
 * trailing filler frames).
 *
 * The result is a plain headerless raw .bin. rcheevos' *default* cdreader
 * (cdreader_open_bin_track in src/rhash/cdreader.c) auto-detects sector size and
 * header offset by scanning for the sync pattern + "CD001" ISO9660 marker, and
 * supports track <= 1 directly from such a file with NO .cue needed — which is
 * exactly what rc_hash_ps2/rc_hash_psx (rc_cd_open_track(iterator, 1)) request.
 * So `rahash <console_id> disc.bin` reads this file correctly, unmodified.
 *
 * Verified: extracted hashes for several `cdzs`/`cdlz` PS2 discs match RA's
 * `ra_games` preferred_hash exactly (see docs/ps2-cdzs-hashing.md).
 *
 * Usage: extract_chd_cd <in.chd> <out.bin>
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <libchdr/chd.h>
#include <libchdr/cdrom.h>

int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "usage: %s <in.chd> <out.bin>\n", argv[0]); return 2; }

    chd_file *chd = NULL;
    chd_error e = chd_open(argv[1], CHD_OPEN_READ, NULL, &chd);
    if (e != CHDERR_NONE) { fprintf(stderr, "chd_open failed: %s\n", chd_error_string(e)); return 1; }

    const chd_header *h = chd_get_header(chd);
    uint32_t hunkbytes = h->hunkbytes;
    uint32_t totalhunks = h->totalhunks;

    if (hunkbytes % CD_FRAME_SIZE != 0) {
        fprintf(stderr, "not a CD-frame CHD (hunkbytes=%u not a multiple of %d)\n", hunkbytes, CD_FRAME_SIZE);
        chd_close(chd);
        return 1;
    }
    uint32_t frames_per_hunk = hunkbytes / CD_FRAME_SIZE;

    /* parse the first track's frame count from metadata (track index 0 in libchdr's
     * metadata enumeration == "TRACK:1" in the CUE-style tag text) so trailing
     * CD_TRACK_PADDING filler frames are excluded from the output. */
    uint64_t track1_frames = 0;
    {
        char buf[256];
        uint32_t resultlen, resulttag; uint8_t flags;
        int trk, frames;
        char type[32], subtype[32], pgtype[32], pgsub[32];
        e = chd_get_metadata(chd, CDROM_TRACK_METADATA2_TAG, 0, buf, sizeof(buf), &resultlen, &resulttag, &flags);
        if (e == CHDERR_NONE) {
            int pregap = 0, postgap = 0;
            sscanf(buf, "TRACK:%d TYPE:%31s SUBTYPE:%31s FRAMES:%d PREGAP:%d PGTYPE:%31s PGSUB:%31s POSTGAP:%d",
                   &trk, type, subtype, &frames, &pregap, pgtype, pgsub, &postgap);
            track1_frames = (uint64_t)frames;
            fprintf(stderr, "track 1: type=%s frames=%d\n", type, frames);
        } else {
            e = chd_get_metadata(chd, CDROM_TRACK_METADATA_TAG, 0, buf, sizeof(buf), &resultlen, &resulttag, &flags);
            if (e == CHDERR_NONE) {
                sscanf(buf, "TRACK:%d TYPE:%31s SUBTYPE:%31s FRAMES:%d", &trk, type, subtype, &frames);
                track1_frames = (uint64_t)frames;
                fprintf(stderr, "track 1 (v1 meta): type=%s frames=%d\n", type, frames);
            } else {
                fprintf(stderr, "warning: no track metadata found, will extract all hunks\n");
            }
        }
    }

    FILE *out = fopen(argv[2], "wb");
    if (!out) { perror("fopen out"); chd_close(chd); return 1; }

    uint8_t *hunkbuf = malloc(hunkbytes);
    if (!hunkbuf) { fprintf(stderr, "oom\n"); fclose(out); chd_close(chd); return 1; }

    uint64_t frames_written = 0;
    uint64_t frames_target = track1_frames ? track1_frames : (uint64_t)totalhunks * frames_per_hunk;

    for (uint32_t hunk = 0; hunk < totalhunks && frames_written < frames_target; hunk++) {
        e = chd_read(chd, hunk, hunkbuf);
        if (e != CHDERR_NONE) {
            fprintf(stderr, "chd_read hunk %u/%u failed: %s\n", hunk, totalhunks, chd_error_string(e));
            free(hunkbuf); fclose(out); chd_close(chd);
            return 1;
        }
        for (uint32_t f = 0; f < frames_per_hunk && frames_written < frames_target; f++) {
            uint8_t *frame = hunkbuf + (size_t)f * CD_FRAME_SIZE;
            if (fwrite(frame, 1, CD_MAX_SECTOR_DATA, out) != (size_t)CD_MAX_SECTOR_DATA) {
                perror("fwrite");
                free(hunkbuf); fclose(out); chd_close(chd);
                return 1;
            }
            frames_written++;
        }
    }

    free(hunkbuf);
    fclose(out);
    chd_close(chd);
    fprintf(stderr, "ok: wrote %llu sectors (%llu bytes) to %s\n",
            (unsigned long long)frames_written, (unsigned long long)frames_written * CD_MAX_SECTOR_DATA, argv[2]);
    return 0;
}
