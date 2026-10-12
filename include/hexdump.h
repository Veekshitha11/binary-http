/*
 * hexdump.h - the -v output: a readable hex dump of every frame.
 */
#ifndef HEXDUMP_H
#define HEXDUMP_H

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#include "protocol.h"

/* Without -vv a frame is cut after this many bytes (header included). */
#define HEXDUMP_DEFAULT_LIMIT 256

/* "0000: 42 48 54 50 01 01 00 00  00 00 00 45 ...  |BHTP....|" rows of 16. */
void hexdump_bytes(FILE *out, const uint8_t *data, size_t len, size_t limit);

/*
 * Print one frame:
 *
 *   TX FRAME #1  REQUEST  flags=0x0000  payload=69  total=81
 *   0000: 42 48 54 50 ...
 *
 * dir is "TX" (client -> server) or "RX" (server -> client). `raw` is the 12
 * header bytes exactly as they were sent/received; `payload` holds
 * `stored` bytes (which can be fewer than the header says for a skipped
 * frame). `note` (may be NULL) is appended to the title line.
 */
void hexdump_frame(FILE *out, const char *dir, unsigned seq,
                   const uint8_t raw[FRAME_HEADER_SIZE],
                   const uint8_t *payload, size_t stored,
                   const char *note, int full);

#endif
