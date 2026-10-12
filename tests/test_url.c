/*
 * Unit tests for the command-line target parser and the hexdump formatter.
 * No sockets, no server needed.
 */
#define _XOPEN_SOURCE 700
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "hexdump.h"
#include "url.h"

static int checks, failed;

#define CHECK(cond)                                                          \
    do {                                                                     \
        checks++;                                                            \
        if (!(cond)) {                                                       \
            failed++;                                                        \
            printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);         \
        }                                                                    \
    } while (0)

static void expect_ok(const char *in, const char *host, const char *port, const char *path)
{
    Target t;
    UrlStatus s = parse_target(in, &t);
    CHECK(s == URL_OK);
    if (s == URL_OK) {
        CHECK(strcmp(t.host, host) == 0);
        CHECK(strcmp(t.port, port) == 0);
        CHECK(strcmp(t.path, path) == 0);
    }
}

static void expect_err(const char *in, UrlStatus want)
{
    Target t;
    UrlStatus s = parse_target(in, &t);
    CHECK(s == want);
    if (s != want)
        printf("       input '%s' gave %d, wanted %d\n", in, (int)s, (int)want);
}

static void test_valid_targets(void)
{
    expect_ok("localhost:9000/index.html", "localhost", "9000", "/index.html");
    expect_ok("127.0.0.1:80/a/b/c.txt", "127.0.0.1", "80", "/a/b/c.txt");
    expect_ok("localhost:9000", "localhost", "9000", "/");
    expect_ok("localhost:9000/", "localhost", "9000", "/");
    expect_ok("bhtp://example.org:65535/x", "example.org", "65535", "/x");
    expect_ok("[::1]:9000/index.html", "::1", "9000", "/index.html");
    expect_ok("[fe80::1]:1/", "fe80::1", "1", "/");
    /* the path is NOT normalised: the server judges it */
    expect_ok("localhost:9000/../../etc/passwd", "localhost", "9000", "/../../etc/passwd");
    expect_ok("localhost:9000//a//b/./c", "localhost", "9000", "//a//b/./c");
    expect_ok("localhost:9000/%2e%2e/x", "localhost", "9000", "/%2e%2e/x");
    expect_ok("localhost:9000/a b", "localhost", "9000", "/a b");
    expect_ok("localhost:9000/a:b/c", "localhost", "9000", "/a:b/c");
}

static void test_invalid_targets(void)
{
    expect_err("", URL_EMPTY);
    expect_err(":9000/x", URL_NO_HOST);
    expect_err("localhost/index.html", URL_NO_PORT);
    expect_err("localhost", URL_NO_PORT);
    expect_err("localhost:/x", URL_BAD_PORT);
    expect_err("localhost:0/x", URL_BAD_PORT);
    expect_err("localhost:65536/x", URL_BAD_PORT);
    expect_err("localhost:99999999999999999999/x", URL_BAD_PORT);
    expect_err("localhost:90a0/x", URL_BAD_PORT);
    expect_err("localhost:-1/x", URL_BAD_PORT);
    expect_err("[::1/x", URL_BAD_HOST);
    expect_err("[]:9000/x", URL_BAD_HOST);
    expect_err("[::1]9000/x", URL_BAD_HOST);
    expect_err("localhost:9000/a\tb", URL_BAD_PATH);
    expect_err("localhost:9000/a\nb", URL_BAD_PATH);
    expect_err("localhost:9000/a\x7f", URL_BAD_PATH);
}

static void test_length_limits(void)
{
    static char big[MAX_PATH_LENGTH + 100];
    Target t;
    size_t i;

    strcpy(big, "h:1/");
    for (i = 4; i < 4 + MAX_PATH_LENGTH - 1; i++)
        big[i] = 'a';
    big[i] = '\0';                              /* path = "/" + 4095 a = 4096 */
    CHECK(parse_target(big, &t) == URL_OK);
    CHECK(strlen(t.path) == MAX_PATH_LENGTH);
    big[i] = 'a';
    big[i + 1] = '\0';                          /* 4097 */
    CHECK(parse_target(big, &t) == URL_TOO_LONG);

    {
        char host[400];
        memset(host, 'h', sizeof host);
        host[300] = '\0';
        strcat(host, ":9000/x");
        CHECK(parse_target(host, &t) == URL_TOO_LONG);
    }
}

static void test_path_only(void)
{
    Target t;
    memset(&t, 0, sizeof t);
    CHECK(parse_path_only("/hello.txt", &t) == URL_OK);
    CHECK(strcmp(t.path, "/hello.txt") == 0);
    CHECK(parse_path_only("hello.txt", &t) == URL_BAD_PATH);
    CHECK(parse_path_only("", &t) == URL_BAD_PATH);
    CHECK(parse_path_only("/a\x01", &t) == URL_BAD_PATH);
}

/* ---- hexdump ------------------------------------------------------- */

static char *capture(void (*fn)(FILE *, void *), void *arg)
{
    char *buf = NULL;
    size_t len = 0;
    FILE *f = open_memstream(&buf, &len);
    fn(f, arg);
    fclose(f);
    return buf;
}

struct bytes { const uint8_t *p; size_t n, limit; };

static void do_bytes(FILE *f, void *a)
{
    struct bytes *b = a;
    hexdump_bytes(f, b->p, b->n, b->limit);
}

static void test_hexdump_bytes_format(void)
{
    uint8_t d[20];
    struct bytes b;
    char *out;
    size_t i;

    for (i = 0; i < sizeof d; i++)
        d[i] = (uint8_t)i;
    d[1] = 'A';
    b.p = d; b.n = sizeof d; b.limit = 0;
    out = capture(do_bytes, &b);
    CHECK(strstr(out, "0000: 00 41 02 03 04 05 06 07  08 09 0a 0b 0c 0d 0e 0f  |.A..............|\n") != NULL);
    CHECK(strstr(out, "0010: 10 11 12 13") != NULL);
    CHECK(strstr(out, "|....|\n") != NULL);
    free(out);

    b.n = 0;
    out = capture(do_bytes, &b);
    CHECK(out[0] == '\0');                       /* nothing to print */
    free(out);
}

static void test_hexdump_limit(void)
{
    uint8_t d[1000];
    struct bytes b;
    char *out;

    memset(d, 0xab, sizeof d);
    b.p = d; b.n = sizeof d; b.limit = 256;
    out = capture(do_bytes, &b);
    CHECK(strstr(out, "00f0: ab") != NULL);      /* 16th row, offset 0xf0 */
    CHECK(strstr(out, "0100:") == NULL);         /* nothing past 256 bytes */
    CHECK(strstr(out, "744 more bytes not shown") != NULL);
    free(out);
}

struct frame { uint8_t raw[FRAME_HEADER_SIZE]; const uint8_t *payload; size_t stored; const char *note; };

static void do_frame(FILE *f, void *a)
{
    struct frame *fr = a;
    hexdump_frame(f, "TX", 7, fr->raw, fr->payload, fr->stored, fr->note, 0);
}

static void test_hexdump_frame_title(void)
{
    struct frame fr;
    static const uint8_t pay[3] = { 0x01, 0x02, 0x03 };
    FrameHeader h;
    char *out;

    h.version = BHTP_VERSION; h.type = FRAME_REQUEST; h.flags = 0; h.payload_length = 3;
    encode_frame_header(fr.raw, &h);
    fr.payload = pay; fr.stored = 3; fr.note = NULL;
    out = capture(do_frame, &fr);
    CHECK(strstr(out, "TX FRAME #7  REQUEST  flags=0x0000  payload=3  total=15\n") != NULL);
    CHECK(strstr(out, "0000: 42 48 54 50 01 01 00 00  00 00 00 03 01 02 03") != NULL);
    free(out);

    h.type = 0x7f; h.flags = 0x8001; h.payload_length = 100;
    encode_frame_header(fr.raw, &h);
    fr.stored = 3; fr.note = "unknown frame type, SKIPPED";
    out = capture(do_frame, &fr);
    CHECK(strstr(out, "UNKNOWN(0x7f)  flags=0x8001  payload=100  total=112  [unknown frame type, SKIPPED]") != NULL);
    CHECK(strstr(out, "97 more payload bytes were skipped") != NULL);
    free(out);
}

int main(void)
{
    test_valid_targets();
    test_invalid_targets();
    test_length_limits();
    test_path_only();
    test_hexdump_bytes_format();
    test_hexdump_limit();
    test_hexdump_frame_title();
    printf("%d checks, %d failed\n", checks, failed);
    if (failed) {
        printf("SOME URL/HEXDUMP UNIT TESTS FAILED\n");
        return 1;
    }
    printf("ALL URL/HEXDUMP UNIT TESTS PASSED\n");
    return 0;
}
