/* Minimal CHD -> raw image extractor using libchdr (zstd-capable).
 * For raw/DVD-type CHDs (PS2), the concatenated decompressed hunks == the ISO.
 * Usage: extract_chd <in.chd> <out.img>
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <libchdr/chd.h>

int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "usage: %s <in.chd> <out.img>\n", argv[0]); return 2; }
    chd_file *chd = NULL;
    chd_error e = chd_open(argv[1], CHD_OPEN_READ, NULL, &chd);
    if (e != CHDERR_NONE) { fprintf(stderr, "chd_open failed: %s\n", chd_error_string(e)); return 1; }

    const chd_header *h = chd_get_header(chd);
    uint32_t hb = h->hunkbytes, th = h->totalhunks;
    uint64_t logical = h->logicalbytes;

    FILE *out = fopen(argv[2], "wb");
    if (!out) { perror("fopen out"); chd_close(chd); return 1; }

    uint8_t *buf = malloc(hb);
    if (!buf) { fprintf(stderr, "oom\n"); fclose(out); chd_close(chd); return 1; }

    uint64_t written = 0;
    for (uint32_t i = 0; i < th && written < logical; i++) {
        e = chd_read(chd, i, buf);
        if (e != CHDERR_NONE) {
            fprintf(stderr, "chd_read hunk %u/%u failed: %s\n", i, th, chd_error_string(e));
            free(buf); fclose(out); chd_close(chd); return 1;
        }
        uint64_t remain = logical - written;
        size_t n = (remain < (uint64_t)hb) ? (size_t)remain : (size_t)hb;
        if (fwrite(buf, 1, n, out) != n) { perror("fwrite"); free(buf); fclose(out); chd_close(chd); return 1; }
        written += n;
    }
    free(buf); fclose(out); chd_close(chd);
    fprintf(stderr, "ok: wrote %llu bytes (%u hunks x %u)\n", (unsigned long long)written, th, hb);
    return 0;
}
