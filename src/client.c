#ifndef _XOPEN_SOURCE
#define _XOPEN_SOURCE 700
#endif

#include "client.h"

#include <ctype.h>
#include <errno.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "connect.h"
#include "frame.h"
#include "hexdump.h"
#include "io.h"

/* ------------------------------------------------------------------ */

const char *status_reason(unsigned status)
{
    switch (status) {
    case 200: return "OK";
    case 400: return "Bad Request";
    case 404: return "Not Found";
    case 500: return "Internal Server Error";
    default:  return "";
    }
}

void client_init(Client *c, int verbose)
{
    memset(c, 0, sizeof *c);
    c->fd = -1;
    c->verbose = verbose;
}

int client_add_header(Client *c, const char *name, const char *value)
{
    CustomHeader *h;
    Header probe;
    size_t i, n = strlen(name), v = strlen(value);

    if (c->custom_count >= MAX_CUSTOM_HEADERS || n == 0 ||
        n > MAX_HEADER_NAME_LENGTH || v > MAX_HEADER_VALUE_LENGTH)
        return -1;
    h = &c->custom[c->custom_count];
    for (i = 0; i < n; i++)
        h->name[i] = (char)tolower((unsigned char)name[i]);
    h->name[n] = '\0';
    memcpy(h->value, value, v + 1);
    /* reject now (exit 64) what the protocol could never encode later */
    if (header_set(&probe, h->name, h->value, v) != 0)
        return -1;
    c->custom_count++;
    return 0;
}

/* -v notes go to stderr so that stdout carries nothing but the body. */
static void note(const Client *c, const char *fmt, ...)
{
    va_list ap;
    if (c->verbose == 0)
        return;
    fputs("* ", stderr);
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
}

int client_connect(Client *c, const char *host, const char *port)
{
    if (c->fd >= 0 || c->connections > 0) {
        fprintf(stderr, "bcurl: internal error: a second connection was requested\n");
        return -1;                    /* the one-connection rule is enforced here */
    }
    c->fd = connect_to_server(host, port);
    if (c->fd < 0)
        return -1;
    c->connections = 1;
    snprintf(c->host_header, sizeof c->host_header, "%s:%s", host, port);
    note(c, "connected to %s port %s (TCP connection #1, the only one)", host, port);
    return 0;
}

void client_close(Client *c)
{
    if (c->fd >= 0) {
        close(c->fd);
        c->fd = -1;
    }
}

/* ------------------------------------------------------------------ */
/* sending                                                             */

static int send_request(Client *c, const char *path)
{
    Request rq;
    Header *h;
    uint8_t payload[MAX_FRAME_PAYLOAD], frame[FRAME_HEADER_SIZE + MAX_FRAME_PAYLOAD];
    size_t len = 0, total;
    unsigned i;
    BhtpError e;

    memset(&rq, 0, sizeof rq);
    rq.method = METHOD_GET;
    rq.path = (const uint8_t *)path;
    rq.path_length = (uint16_t)strlen(path);

    /* The five static-name headers we always send, then the custom ones. */
#define ADD(name, val)                                                       \
    do {                                                                     \
        h = &rq.headers[rq.header_count];                                    \
        if (header_set(h, (name), (val), strlen(val)) != 0)                  \
            return -1;                                                       \
        rq.header_count++;                                                   \
    } while (0)
    ADD("host", c->host_header);
    ADD("user-agent", CLIENT_NAME);
    ADD("accept", "*/*");
    ADD("accept-encoding", "identity");
    ADD("connection", "keep-alive");
#undef ADD
    for (i = 0; i < c->custom_count; i++) {
        if (rq.header_count >= MAX_HEADERS) {
            fprintf(stderr, "bcurl: too many headers (limit %d)\n", MAX_HEADERS);
            return -1;
        }
        h = &rq.headers[rq.header_count];
        if (header_set(h, c->custom[i].name, c->custom[i].value,
                       strlen(c->custom[i].value)) != 0) {
            fprintf(stderr, "bcurl: invalid header '%s' (names are a-z 0-9 '-')\n",
                    c->custom[i].name);
            return -1;
        }
        rq.header_count++;
    }

    e = encode_request(&rq, payload, sizeof payload, &len);
    if (e != BHTP_OK) {
        fprintf(stderr, "bcurl: cannot encode request: %s\n", bhtp_error_text(e));
        return -1;
    }
    total = build_frame(frame, sizeof frame, FRAME_REQUEST, 0, payload, len);
    if (total == 0)
        return -1;

    c->tx_frames++;
    if (c->verbose)                         /* dump the very bytes we send */
        hexdump_frame(stderr, "TX", c->tx_frames, frame, frame + FRAME_HEADER_SIZE,
                      len, NULL, c->verbose > 1);
    if (io_write_all(c->fd, frame, total) != 0) {
        fprintf(stderr, "bcurl: send failed: %s\n", strerror(errno));
        return -1;
    }
    c->requests++;
    return 0;
}

/* ------------------------------------------------------------------ */
/* receiving                                                           */

/* Keeps the start of a skipped frame's payload so -v can show it. */
typedef struct {
    uint8_t data[HEXDUMP_DEFAULT_LIMIT];
    size_t  n;
} Capture;

static void capture_sink(void *ctx, const uint8_t *chunk, size_t n)
{
    Capture *cap = ctx;
    size_t room = sizeof cap->data - cap->n;
    if (room > 0) {
        size_t k = n < room ? n : room;
        memcpy(cap->data + cap->n, chunk, k);
        cap->n += k;
    }
}

/* Parse a decimal content-length strictly (digits only, no overflow). */
static int parse_length(const Header *h, unsigned long long *out)
{
    unsigned long long v = 0;
    size_t i;

    if (h == NULL || h->value_length == 0 || h->value_length > 18)
        return 0;
    for (i = 0; i < h->value_length; i++) {
        if (h->value[i] < '0' || h->value[i] > '9')
            return 0;
        v = v * 10 + (unsigned long long)(h->value[i] - '0');
    }
    *out = v;
    return 1;
}

static void print_response_summary(const Client *c, const Response *r)
{
    unsigned i;
    if (c->verbose == 0)
        return;
    fprintf(stderr, "< status %u %s\n", r->status_code, status_reason(r->status_code));
    for (i = 0; i < r->header_count; i++) {
        const Header *h = &r->headers[i];
        fprintf(stderr, "< %s: %.*s    (%s)\n", h->name, (int)h->value_length,
                (const char *)h->value,
                h->name_id ? "static name, 1 byte" : "literal name");
    }
}

static FetchKind fail(Client *c, const char *fmt, ...)
{
    va_list ap;
    fputs("bcurl: ", stderr);
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
    client_close(c);
    return FETCH_FAILED;
}

static FetchKind receive_response(Client *c, FILE *out, FetchResult *res)
{
    int got_response = 0, have_length = 0;
    unsigned long long expected = 0;

    for (;;) {
        FrameHeader h;
        uint8_t raw[FRAME_HEADER_SIZE], payload[MAX_FRAME_PAYLOAD];
        Capture cap;
        FrameStatus st;

        cap.n = 0;
        st = frame_read(c->fd, &h, raw, payload, capture_sink, &cap);

        if (st == FR_EOF)
            return fail(c, "the server closed the connection before the response was complete");
        if (st == FR_TRUNC)
            return fail(c, "the connection was lost in the middle of a frame");
        if (st == FR_IOERR)
            return fail(c, "%s", (errno == EAGAIN || errno == EWOULDBLOCK)
                                  ? "timed out waiting for the server"
                                  : strerror(errno));
        if (st == FR_BAD_MAGIC || st == FR_BAD_VERSION || st == FR_TOO_BIG) {
            c->rx_frames++;
            if (c->verbose)
                hexdump_frame(stderr, "RX", c->rx_frames, raw, NULL, 0, "INVALID", c->verbose > 1);
            return fail(c, "the server sent an invalid frame: %s", frame_status_text(st));
        }

        c->rx_frames++;

        if (st == FR_SKIPPED) {                /* unknown type: forward compatibility */
            res->skipped_frames++;
            if (c->verbose) {
                hexdump_frame(stderr, "RX", c->rx_frames, raw, cap.data, cap.n,
                              "unknown frame type, SKIPPED", c->verbose > 1);
            }
            note(c, "skipped unknown frame type 0x%02x (%u payload bytes)", h.type,
                 (unsigned)h.payload_length);
            continue;
        }

        if (c->verbose)
            hexdump_frame(stderr, "RX", c->rx_frames, raw, payload, h.payload_length,
                          st == FR_BAD_FLAGS ? "BAD FLAGS" : NULL, c->verbose > 1);
        if (st == FR_BAD_FLAGS)
            return fail(c, "the server sent a frame with undefined flags (0x%04x)", h.flags);

        switch (h.type) {
        case FRAME_RESPONSE: {
            Response r;
            BhtpError e;

            if (got_response)
                return fail(c, "protocol error: a second RESPONSE frame in one response");
            e = decode_response(payload, h.payload_length, &r);
            if (e != BHTP_OK)
                return fail(c, "malformed RESPONSE frame: %s", bhtp_error_text(e));
            got_response = 1;
            res->status = r.status_code;
            have_length = parse_length(find_header(r.headers, r.header_count, "content-length"),
                                       &expected);
            print_response_summary(c, &r);
            if (h.flags & FLAG_END_STREAM) {
                if (have_length && expected != 0)
                    return fail(c, "content-length is %llu but the response ended without a body",
                                expected);
                return FETCH_OK;
            }
            break;
        }
        case FRAME_DATA:
            if (!got_response)
                return fail(c, "protocol error: DATA frame before the RESPONSE frame");
            res->body_bytes += h.payload_length;
            res->data_frames++;
            if (have_length && res->body_bytes > expected)
                return fail(c, "body is longer than content-length (%llu)", expected);
            if (h.payload_length > 0 && fwrite(payload, 1, h.payload_length, out) != h.payload_length)
                return fail(c, "cannot write to standard output: %s", strerror(errno));
            if (h.flags & FLAG_END_STREAM) {
                if (have_length && res->body_bytes != expected)
                    return fail(c, "body is %llu bytes but content-length says %llu",
                                res->body_bytes, expected);
                return FETCH_OK;
            }
            break;
        case FRAME_ERROR: {
            ErrorMessage m;
            if (decode_error(payload, h.payload_length, &m) != BHTP_OK)
                return fail(c, "malformed ERROR frame");
            res->status = m.status_code;
            fprintf(stderr, "bcurl: the server reported an error: %u %.*s\n", m.status_code,
                    (int)m.reason_length, (const char *)m.reason);
            client_close(c);                  /* an ERROR frame ends the connection */
            return FETCH_SERVER_ERROR;
        }
        default:                              /* REQUEST: servers never send these */
            return fail(c, "protocol error: the server sent a REQUEST frame");
        }
    }
}

FetchKind client_fetch(Client *c, const char *path, FILE *body_out, FetchResult *res)
{
    FetchKind k;

    memset(res, 0, sizeof *res);
    if (c->fd < 0)
        return FETCH_FAILED;
    note(c, "GET %s  (request %u on connection #1)", path, c->requests + 1);
    if (send_request(c, path) != 0) {
        client_close(c);
        return FETCH_FAILED;
    }
    k = receive_response(c, body_out, res);
    if (k == FETCH_OK)
        note(c, "response complete: status %u, %llu body bytes in %u DATA frame(s)",
             res->status, res->body_bytes, res->data_frames);
    return k;
}
