/*
 * bcurl - BHTP/1 command-line client.
 *
 *   usage: bcurl [-v|-vv] [-H 'name: value']... host:port/path [/more/paths...]
 *
 * Opens ONE TCP connection, sends one REQUEST frame per path on it, writes the
 * response bodies to stdout. With -v every frame is hex-dumped to stderr.
 *
 * Exit status: 0 = every response was below 400
 *              1 = some response was 4xx/5xx (or the server sent an ERROR frame)
 *              2 = transport or protocol failure
 *             64 = bad command line
 */
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "client.h"
#include "hexdump.h"
#include "url.h"

#define EXIT_OK        0
#define EXIT_HTTP_FAIL 1
#define EXIT_NET_FAIL  2
#define EXIT_USAGE     64

static void usage(const char *prog)
{
    fprintf(stderr,
        "usage: %s [-v|-vv] [-H 'name: value']... <host:port/path> [</path> ...]\n"
        "\n"
        "  -v     hex-dump every frame sent (TX) and received (RX) on stderr\n"
        "  -vv    same, without cutting long frames after %d bytes\n"
        "  -H h   add a request header (static names use a 1-byte tag)\n"
        "\n"
        "Extra paths are fetched over the SAME connection:\n"
        "  %s localhost:9000/index.html /hello.txt /missing\n"
        "\n"
        "exit status: 0 ok, 1 a 4xx/5xx response, 2 network/protocol error, 64 usage\n",
        prog, HEXDUMP_DEFAULT_LIMIT, prog);
}

/* "Name: value" -> name, value (whitespace around the value trimmed). */
static int split_header(const char *arg, char *name, size_t ncap, const char **value)
{
    const char *colon = strchr(arg, ':');
    size_t n;

    if (colon == NULL || colon == arg)
        return -1;
    n = (size_t)(colon - arg);
    if (n >= ncap)
        return -1;
    memcpy(name, arg, n);
    name[n] = '\0';
    *value = colon + 1;
    while (**value == ' ' || **value == '\t')
        (*value)++;
    return 0;
}

int main(int argc, char **argv)
{
    Client c;
    Target first, t;
    FetchResult res;
    int verbose = 0, opt, i, exit_code = EXIT_OK;
    UrlStatus us;
    unsigned done = 0;

    client_init(&c, 0);

    while ((opt = getopt(argc, argv, "+vH:h")) != -1) {
        char name[MAX_HEADER_NAME_LENGTH + 1];
        const char *value;

        switch (opt) {
        case 'v':
            verbose++;
            break;
        case 'H':
            if (split_header(optarg, name, sizeof name, &value) != 0 ||
                client_add_header(&c, name, value) != 0) {
                fprintf(stderr, "bcurl: invalid header '%s' (expected 'name: value')\n", optarg);
                return EXIT_USAGE;
            }
            break;
        case 'h':
            usage(argv[0]);
            return EXIT_OK;
        default:
            usage(argv[0]);
            return EXIT_USAGE;
        }
    }
    c.verbose = verbose;
    if (optind >= argc) {
        usage(argv[0]);
        return EXIT_USAGE;
    }

    /* Validate every target BEFORE connecting, so a typo costs no connection. */
    us = parse_target(argv[optind], &first);
    if (us != URL_OK) {
        fprintf(stderr, "bcurl: bad target '%s': %s\n", argv[optind], url_status_text(us));
        return EXIT_USAGE;
    }
    for (i = optind + 1; i < argc; i++) {
        if (argv[i][0] == '/') {
            us = parse_path_only(argv[i], &t);
        } else {
            us = parse_target(argv[i], &t);
            if (us == URL_OK && (strcmp(t.host, first.host) != 0 || strcmp(t.port, first.port) != 0)) {
                fprintf(stderr, "bcurl: '%s' is a different server; bcurl uses a single "
                                "connection and will not open another\n", argv[i]);
                return EXIT_USAGE;
            }
        }
        if (us != URL_OK) {
            fprintf(stderr, "bcurl: bad target '%s': %s\n", argv[i], url_status_text(us));
            return EXIT_USAGE;
        }
    }

    signal(SIGPIPE, SIG_IGN);

    if (client_connect(&c, first.host, first.port) != 0)
        return EXIT_NET_FAIL;

    for (i = optind; i < argc; i++) {
        const char *path;
        FetchKind k;

        if (i == optind) {
            path = first.path;
        } else if (argv[i][0] == '/') {
            path = argv[i];
        } else {
            parse_target(argv[i], &t);
            path = t.path;
        }

        k = client_fetch(&c, path, stdout, &res);
        fflush(stdout);
        if (k == FETCH_FAILED) {
            exit_code = EXIT_NET_FAIL;
            break;
        }
        done++;
        if (k == FETCH_SERVER_ERROR) {
            exit_code = EXIT_HTTP_FAIL;
            break;
        }
        if (res.status >= 400) {
            fprintf(stderr, "bcurl: GET %s -> %u %s\n", path, res.status, status_reason(res.status));
            if (exit_code == EXIT_OK)
                exit_code = EXIT_HTTP_FAIL;
        }
    }

    if (verbose)
        fprintf(stderr, "* done: %u request(s) completed over %u TCP connection(s); exit status %d\n",
                done, c.connections, exit_code);
    client_close(&c);
    return exit_code;
}
