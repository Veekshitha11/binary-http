/*
 * client.h - a BHTP/1 client session: ONE connection, any number of
 * sequential requests on it.
 */
#ifndef CLIENT_H
#define CLIENT_H

#include <stdio.h>

#include "protocol.h"

#define CLIENT_NAME "bcurl/1.0"
#define MAX_CUSTOM_HEADERS 16

typedef struct {
    char name[MAX_HEADER_NAME_LENGTH + 1];
    char value[MAX_HEADER_VALUE_LENGTH + 1];
} CustomHeader;

typedef struct {
    int          fd;                /* the one and only connection, -1 if none */
    int          verbose;           /* 0 quiet, 1 -v, 2 -vv (no truncation)    */
    unsigned     connections;       /* TCP connections opened: never above 1   */
    unsigned     requests;          /* requests sent on it                     */
    unsigned     tx_frames, rx_frames;
    char         host_header[300];  /* "host:port" sent in the host header     */
    CustomHeader custom[MAX_CUSTOM_HEADERS];
    unsigned     custom_count;
} Client;

typedef enum {
    FETCH_OK = 0,        /* a complete response arrived (any status)           */
    FETCH_SERVER_ERROR,  /* the server sent an ERROR frame (status in result)  */
    FETCH_FAILED         /* transport or protocol failure; session is unusable */
} FetchKind;

typedef struct {
    uint16_t           status;
    unsigned long long body_bytes;
    unsigned           data_frames;
    unsigned           skipped_frames;   /* unknown frame types that were skipped */
} FetchResult;

void client_init(Client *c, int verbose);
/* Register a custom request header; returns 0 or -1 if it is not valid. */
int  client_add_header(Client *c, const char *name, const char *value);
/* Open the one TCP connection. Returns 0 or -1. */
int  client_connect(Client *c, const char *host, const char *port);
/* GET `path` on the open connection; the body is written to `body_out`. */
FetchKind client_fetch(Client *c, const char *path, FILE *body_out, FetchResult *res);
void client_close(Client *c);

const char *status_reason(unsigned status);

#endif
