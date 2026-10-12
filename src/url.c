#include "url.h"

#include <string.h>

const char *url_status_text(UrlStatus s)
{
    switch (s) {
    case URL_OK:       return "ok";
    case URL_EMPTY:    return "empty target";
    case URL_NO_HOST:  return "missing host";
    case URL_NO_PORT:  return "missing port (expected host:port/path)";
    case URL_BAD_PORT: return "port must be a number from 1 to 65535";
    case URL_BAD_HOST: return "malformed host";
    case URL_BAD_PATH: return "path contains control characters";
    case URL_TOO_LONG: return "host or path too long";
    }
    return "?";
}

static UrlStatus check_path(const char *p, size_t n)
{
    size_t i;
    if (n > MAX_PATH_LENGTH)
        return URL_TOO_LONG;
    for (i = 0; i < n; i++)
        if ((unsigned char)p[i] < 0x20 || (unsigned char)p[i] == 0x7f)
            return URL_BAD_PATH;
    return URL_OK;
}

UrlStatus parse_path_only(const char *text, Target *t)
{
    size_t n;
    UrlStatus s;

    if (text == NULL || text[0] != '/')
        return URL_BAD_PATH;
    n = strlen(text);
    s = check_path(text, n);
    if (s != URL_OK)
        return s;
    memcpy(t->path, text, n + 1);
    return URL_OK;
}

UrlStatus parse_target(const char *text, Target *t)
{
    static const char scheme[] = "bhtp://";
    const char *p, *host, *colon, *slash, *q;
    size_t hlen, plen;

    memset(t, 0, sizeof *t);
    if (text == NULL || text[0] == '\0')
        return URL_EMPTY;

    p = text;
    if (strncmp(p, scheme, sizeof scheme - 1) == 0)
        p += sizeof scheme - 1;

    if (*p == '[') {                         /* [::1]:9000/x */
        host = p + 1;
        q = strchr(host, ']');
        if (q == NULL || q == host || q[1] != ':')
            return URL_BAD_HOST;
        hlen = (size_t)(q - host);
        colon = q + 1;
    } else {
        host = p;
        colon = strchr(p, ':');
        slash = strchr(p, '/');
        if (colon == NULL || (slash != NULL && slash < colon))
            return URL_NO_PORT;
        hlen = (size_t)(colon - host);
        if (hlen == 0)
            return URL_NO_HOST;
    }
    if (hlen >= sizeof t->host)
        return URL_TOO_LONG;
    memcpy(t->host, host, hlen);
    t->host[hlen] = '\0';

    q = colon + 1;                           /* port: digits up to '/' or end */
    slash = strchr(q, '/');
    plen = slash != NULL ? (size_t)(slash - q) : strlen(q);
    if (plen == 0 || plen >= sizeof t->port)
        return URL_BAD_PORT;
    {
        size_t i;
        unsigned long v = 0;
        for (i = 0; i < plen; i++) {
            if (q[i] < '0' || q[i] > '9')
                return URL_BAD_PORT;
            v = v * 10 + (unsigned long)(q[i] - '0');
        }
        if (v < 1 || v > 65535)
            return URL_BAD_PORT;
    }
    memcpy(t->port, q, plen);
    t->port[plen] = '\0';

    if (slash == NULL) {
        strcpy(t->path, "/");
        return URL_OK;
    }
    {
        size_t n = strlen(slash);
        UrlStatus s = check_path(slash, n);
        if (s != URL_OK)
            return s;
        memcpy(t->path, slash, n + 1);
    }
    return URL_OK;
}
