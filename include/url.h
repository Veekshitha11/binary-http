/*
 * url.h - parsing of the command-line target "host:port/path".
 */
#ifndef URL_H
#define URL_H

#include "protocol.h"

typedef struct {
    char host[256];
    char port[8];
    char path[MAX_PATH_LENGTH + 1];
} Target;

typedef enum {
    URL_OK = 0,
    URL_EMPTY,        /* empty argument                                      */
    URL_NO_HOST,      /* nothing before the ':'                              */
    URL_NO_PORT,      /* no ':port' part                                     */
    URL_BAD_PORT,     /* port is not a number in 1..65535                    */
    URL_BAD_HOST,     /* unterminated [ipv6] or characters after it          */
    URL_BAD_PATH,     /* path contains control characters                    */
    URL_TOO_LONG      /* host or path longer than the protocol allows        */
} UrlStatus;

const char *url_status_text(UrlStatus s);

/*
 * Accepts   host:port/path   host:port   [v6addr]:port/path
 * with an optional "bhtp://" prefix. A missing path means "/". The path is
 * kept byte-for-byte as typed (no normalisation, no percent-decoding): it is
 * the server's job to judge it.
 */
UrlStatus parse_target(const char *text, Target *t);

/* A bare path ("/b.html") for an additional request on the same connection. */
UrlStatus parse_path_only(const char *text, Target *t);

#endif
