/*
 * connect.h - client-side TCP helper (no protocol knowledge here).
 */
#ifndef CONNECT_H
#define CONNECT_H

#define CLIENT_TIMEOUT_SECONDS 30

/*
 * Open ONE TCP connection to host:port. Every address the name resolves to is
 * tried in turn until one connects (e.g. "localhost" -> ::1, then 127.0.0.1);
 * only the first success is kept, so this still yields a single connection.
 * Returns the fd, or -1 with a message on stderr.
 */
int connect_to_server(const char *host, const char *port);

#endif
