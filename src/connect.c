#ifndef _XOPEN_SOURCE
#define _XOPEN_SOURCE 700
#endif

#include "connect.h"

#include <errno.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

int connect_to_server(const char *host, const char *port)
{
    struct addrinfo hints, *res = NULL, *rp;
    int fd = -1, rc, saved = 0;

    memset(&hints, 0, sizeof hints);
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;

    rc = getaddrinfo(host, port, &hints, &res);
    if (rc != 0) {
        fprintf(stderr, "bcurl: cannot resolve '%s': %s\n", host, gai_strerror(rc));
        return -1;
    }
    for (rp = res; rp != NULL; rp = rp->ai_next) {
        fd = socket(rp->ai_family, rp->ai_socktype, rp->ai_protocol);
        if (fd < 0) {
            saved = errno;
            continue;
        }
        if (connect(fd, rp->ai_addr, rp->ai_addrlen) == 0)
            break;
        saved = errno;
        close(fd);
        fd = -1;
    }
    freeaddrinfo(res);

    if (fd < 0) {
        fprintf(stderr, "bcurl: cannot connect to %s:%s: %s\n", host, port,
                strerror(saved ? saved : ECONNREFUSED));
        return -1;
    }
    {
        int on = 1;
        struct timeval tv;
        tv.tv_sec = CLIENT_TIMEOUT_SECONDS;
        tv.tv_usec = 0;
        setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &on, sizeof on);
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
    }
    return fd;
}
