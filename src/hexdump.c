#include "hexdump.h"

#include <stdint.h>
#include <string.h>

void hexdump_bytes(FILE *out, const uint8_t *data, size_t len, size_t limit)
{
    size_t shown = (limit != 0 && len > limit) ? limit : len;
    size_t off, i;

    for (off = 0; off < shown; off += 16) {
        fprintf(out, "%04zx: ", off);
        for (i = 0; i < 16; i++) {
            if (off + i < shown)
                fprintf(out, "%02x ", data[off + i]);
            else
                fputs("   ", out);
            if (i == 7)
                fputc(' ', out);
        }
        fputs(" |", out);
        for (i = 0; i < 16 && off + i < shown; i++) {
            uint8_t c = data[off + i];
            fputc(c >= 0x20 && c < 0x7f ? (int)c : '.', out);
        }
        fputs("|\n", out);
    }
    if (shown < len)
        fprintf(out, "      ... %zu more bytes not shown (use -vv to see everything)\n",
                len - shown);
}

void hexdump_frame(FILE *out, const char *dir, unsigned seq,
                   const uint8_t raw[FRAME_HEADER_SIZE],
                   const uint8_t *payload, size_t stored,
                   const char *note, int full)
{
    uint8_t buf[FRAME_HEADER_SIZE + MAX_FRAME_PAYLOAD];
    FrameHeader h;
    size_t total;

    if (stored > MAX_FRAME_PAYLOAD)
        stored = MAX_FRAME_PAYLOAD;
    h.type = raw[5];
    h.flags = read_u16(raw + 6);
    h.payload_length = read_u32(raw + 8);

    fprintf(out, "%s FRAME #%u  %s", dir, seq, frame_type_name(h.type));
    if (!frame_type_known(h.type))
        fprintf(out, "(0x%02x)", h.type);
    fprintf(out, "  flags=0x%04x  payload=%u  total=%u", h.flags,
            (unsigned)h.payload_length, (unsigned)(FRAME_HEADER_SIZE + h.payload_length));
    if (note != NULL && note[0] != '\0')
        fprintf(out, "  [%s]", note);
    fputc('\n', out);

    memcpy(buf, raw, FRAME_HEADER_SIZE);
    if (stored > 0)
        memcpy(buf + FRAME_HEADER_SIZE, payload, stored);
    total = FRAME_HEADER_SIZE + stored;
    hexdump_bytes(out, buf, total, full ? 0 : HEXDUMP_DEFAULT_LIMIT);
    if (stored < h.payload_length && (full || total <= HEXDUMP_DEFAULT_LIMIT))
        fprintf(out, "      ... %u more payload bytes were skipped, not stored\n",
                (unsigned)(h.payload_length - stored));
    fputc('\n', out);
}
