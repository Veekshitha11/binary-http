# BHTP/1 client - `bcurl` (Veekshitha)

Track 2 of the course project **"HTTP, in binary"**: a command-line client for a binary
protocol of our own design, written in C11 with plain POSIX sockets. Its partner is Nistha's
server, `bserve` (`nistha-binary-http`). **The only thing that crosses between the two
projects is [SPEC.md](SPEC.md)** (the same file in both repositories), so this client was also
tested against a second server written from the spec alone (`tools/refserver.py`).

## Quick start

```sh
git clone <your-repo-url> veekshitha-binary-http && cd veekshitha-binary-http
make                      # builds ./bcurl
make test                 # unit + integration tests; ends with "ALL TESTS PASSED"
python3 tools/refserver.py fixtures/www 9000 &     # a spec-only server (or Nistha's ./bserve ./www 9000)
./bcurl -v localhost:9000/index.html
```

## 1. Project overview

`bcurl [-v] host:port/path` builds a binary REQUEST frame, sends it over **one** TCP connection, reads
the RESPONSE and DATA frames, writes the body to **stdout**, and exits non-zero for 4xx/5xx. With `-v` it
hex-dumps every frame in both directions to **stderr** (so the body on stdout stays clean for `cmp` or
redirection). Extra paths on the command line are fetched over the *same* connection; the client is
built so that it cannot open a second one.

## 2. Assignment requirements (Track 2 - the client)

```
$ ./bcurl -v localhost:9000/index.html
build the binary request frame     read the response, body to stdout     -v hexdumps every frame
exit non-zero on 4xx / 5xx         and never open a second connection
```

The part the assignment calls "the actual project" is the protocol: a fixed-size frame header whose
field widths are defended (HTTP/2 chose 24/8/8/31; see *Design decisions* and `VIVA.md`), ten numbered
header names with the rest length-prefixed, and **one line that may not be skipped: a receiver meeting
a frame type it does not know must skip it cleanly.** Hand-in: the spec (`SPEC.md`), the program, and an annotated hexdump
of one complete request and response (`examples/`).

## 3. Protocol overview

BHTP/1 (Binary Hypertext Transfer Protocol, version 1) is a small sequential
request/response protocol over TCP. A client sends one **REQUEST** frame; the server
answers with one **RESPONSE** frame (status + headers) followed by zero or more **DATA**
frames (the file's bytes). A server that sees a broken byte stream says so with one
**ERROR** frame and closes. Everything is binary and big-endian. The complete,
implementation-independent definition is [SPEC.md](SPEC.md); this README only summarises it.

## 4. Architecture

```
            bcurl (this repo)                               bserve (Nistha's repo)
   +------------------------------+                 +------------------------------+
   | bcurl.c    command line, exit|                 | server.c   accept loop, one  |
   |            status            |     TCP         |            process per conn  |
   | client.c   ONE connection,   | <-------------> | pathres.c  path -> file      |
   |            sequential fetch  |   BHTP/1 frames |                              |
   | hexdump.c  -v output         |   (SPEC.md)     |                              |
   | url.c      host:port/path    |                 |                              |
   +--------------+---------------+                 +--------------+---------------+
   | frame.c  read/write a frame  |                 | frame.c      (same layer)    |
   | protocol.c  encode/decode    |                 | protocol.c                   |
   | io.c   read_exact/write_all  |                 | io.c                         |
   +------------------------------+                 +------------------------------+
```

| File | Responsibility |
|---|---|
| `src/bcurl.c` | command line (`-v`, `-vv`, `-H`), target validation, exit status |
| `src/client.c` | the session: one connection, build REQUEST, read RESPONSE/DATA, checks, body to stdout |
| `src/connect.c` | `getaddrinfo` + `connect` (IPv4/IPv6), timeouts |
| `src/url.c` | `host:port/path`, `[v6]:port/path`, `bhtp://` prefix |
| `src/hexdump.c` | the `TX FRAME` / `RX FRAME` dumps |
| `src/frame.c` | read one whole frame (header, validation, skip unknown), write one frame |
| `src/protocol.c` | pure encode/decode of header, REQUEST, RESPONSE, ERROR and header blocks (no I/O) |
| `src/io.c` | `io_read_exact`, `io_write_all`: partial transfers, EINTR, EOF vs truncation |

## 5. Client/server flow

1. Parse and validate **every** target before connecting (a typo costs no connection).
2. `connect()` once. `client_connect` refuses a second call: the one-connection rule is enforced in code.
3. For each path: build REQUEST (`host`, `user-agent`, `accept`, `accept-encoding`, `connection` + any `-H` headers),
   dump it if `-v`, send it, then read frames until `END_STREAM`.
4. Unknown frame types are skipped; the body goes to stdout as DATA frames arrive; the totals are checked against `content-length`.
5. Status >= 400 -> remember exit status 1 and continue with the next path (the connection is still good). A transport or protocol failure stops everything (exit 2).

## 6. Frame format

Every frame is a **fixed 12-byte header** followed by `payload_length` bytes:

```
Offset  Size  Field           Value
0       4     magic           "BHTP" (42 48 54 50)
4       1     version         1
5       1     type            1 REQUEST, 2 RESPONSE, 3 DATA, 4 ERROR (anything else: unknown)
6       2     flags           bit 0 = END_STREAM; other bits reserved (must be 0)
8       4     payload_length  bytes that follow the header (known types: at most 16 384)
```

| Type | Sender | Payload |
|---|---|---|
| REQUEST (1) | client | `method u8 (1 = GET)`, `path_length u16`, `path`, header block |
| RESPONSE (2) | server | `status u16` (200/400/404/500), header block. `END_STREAM` = "no DATA follows" |
| DATA (3) | server | raw body bytes (0–16 384). `END_STREAM` on the last one |
| ERROR (4) | server | `status u16` + UTF-8 reason; then the server closes |

The receiver finds the end of a frame only from `payload_length`: read exactly 12 bytes,
decode the length, validate it, read exactly that many bytes, process, repeat. A client
knows a response is complete when it sees `END_STREAM`.

## 7. Header encoding

Headers follow the first two ideas of HPACK, and nothing else (no dynamic table, no Huffman):

1. **Static table** - the ten names the protocol actually sends are numbered, so
   `content-type` costs 1 byte instead of 13.
2. **Length-prefixed literals** - any other name is sent as `0`, a 1-byte length, and the name.

```
header block := count:u8 (max 32)  { tag:u8  [name_len:u8 name]  value_len:u16  value }*
                tag 1..10 = static name | tag 0 = literal name follows | tag 11..255 = reserved (malformed)
```

| Tag | Name | Tag | Name |
|---|---|---|---|
| 1 | `host` | 6 | `accept` |
| 2 | `user-agent` | 7 | `server` |
| 3 | `content-type` | 8 | `connection` |
| 4 | `content-length` | 9 | `last-modified` |
| 5 | `accept-encoding` | 10 | `cache-control` |

All ten are really sent: the client sends tags 1, 2, 5, 6, 8 and the server sends tags 3, 4, 7, 8, 9, 10.
Limits: 32 headers per block, literal names 1-255 bytes of `[a-z0-9-]`, values at most 4096 bytes.

Custom headers: `./bcurl -H 'x-trace: 42' ...` is sent as a *literal* name (tag 0); `-H 'accept: text/html'` reuses the static tag.

## 8. Persistent connections

One TCP connection carries any number of exchanges:

```
client                                  server
  | --- REQUEST #1 ------------------->  |
  | <-- RESPONSE #1 (+ DATA ...) -------  |
  | --- REQUEST #2 ------------------->  |      same TCP connection,
  | <-- RESPONSE #2 (+ DATA ...) -------  |      no second connect()
  | --- REQUEST #3 ------------------->  |
  | <-- RESPONSE #3 (+ DATA ...) -------  |
  | --- FIN (client closes) ----------->  |
```

* **Opened** by the client's single `connect()`.
* **Stays open** after every response, including 400, 404 and 500 responses.
* **Closes** when the client closes (the server sees EOF on a frame boundary), after a request
  carrying `connection: close`, after a fatal protocol error (ERROR frame first), when the
  connection is idle for 300 s, or when the peer vanishes in the middle of a frame.
* Any number of requests and responses per connection; exchanges are strictly sequential.

`bcurl localhost:9000/a.html /b.txt /c.txt` fetches three files over one connection; `-v` ends with
`* done: 3 request(s) completed over 1 TCP connection(s)`. A different server on the command line is refused *before* connecting.

## 9. Unknown frame handling

A receiver that meets a frame type it does not know **must not fail**. It reads the 12-byte
header, takes `payload_length`, **skips exactly that many bytes**, and continues with the next
frame. That is what lets a future version 2 add frame types without breaking version-1 peers.
It works for any length (the bytes are streamed through a 4 KiB buffer, never allocated), any
flags, and at any position, including between the RESPONSE and DATA frames of one answer. The
server sends no reply to a skipped frame.

In `bcurl` this is `frame_read()` returning `FR_SKIPPED`; with `-v` the skipped frame is still dumped, labelled
`[unknown frame type, SKIPPED]`, so you can see it was skipped and not lost.

## 10. Error handling

| Layer | Examples | What happens |
|---|---|---|
| **A. Transport** | EOF mid-frame, reset, timeout | connection closed; `bcurl` exits 2 |
| **B. Protocol, fatal** (framing can no longer be trusted) | bad magic, version != 1, known type with length > 16 384 | server sends `ERROR 400`, closes |
| **C. Application, recoverable** (framing intact) | malformed REQUEST payload, undefined flag bits, bad path, method != GET | `RESPONSE 400`, **connection stays open** |
| **C. Application** | file missing / directory without index / outside root | `RESPONSE 404`, stays open |
| **C. Application** | file exists but cannot be read | `RESPONSE 500`, stays open |

`bcurl` exit status: **0** every response below 400 - **1** some response was 4xx/5xx (or the
server sent an ERROR frame) - **2** transport or protocol failure - **64** bad command line.

How the client reacts to a misbehaving server (all tested): connection closed before the response is complete,
connection lost mid-frame, bad magic or version, frame over 16 384 bytes, undefined flag bits, DATA before RESPONSE,
a second RESPONSE, a REQUEST frame from the server, a body shorter or longer than `content-length`, a 30 s silence
(`timed out waiting for the server`) - each prints one clear message and exits 2.

## 11. Security and path handling

The *server* enforces path security (see the server repository's README and `SPEC.md` section 11); the client's part is to be
a faithful, hostile-input-safe peer:

* The path is sent **byte for byte as typed** (no normalisation, no percent-decoding), so you can test any server's traversal defences:
  `./bcurl localhost:9000/../../etc/passwd` really sends `/../../etc/passwd`.
* Paths with control characters, over-long hosts/paths and bad ports are rejected locally (exit 64) before any connection.
* Everything read from the network is bounded: payloads are limited to 16 384 bytes, unknown frames are streamed through a fixed buffer, header and string lengths are checked against the bytes that remain.

Server behaviour you can observe with this client:

The server serves only regular files under the root directory it was started with
(full rules: SPEC.md section 11). The path is an opaque byte string and is **not** percent-decoded.

| Request path | Result | Rule |
|---|---|---|
| `/` , `/sub/` , `/sub` | `index.html` of that directory, else 404 | directories map to `index.html` |
| `/../../etc/passwd` , `/sub/../../x` | **400** | any `..` segment is refused, even a harmless one |
| `/%2e%2e/%2e%2e/etc/passwd` | 404 | `%` is an ordinary byte: it names a directory that does not exist |
| `//etc/passwd` , `/etc/passwd` | 404 | absolute-looking paths are relative to the root: `<root>/etc/passwd` |
| `etc/passwd` , empty path | 400 | must begin with `/` |
| `/a//b` , `/a/./b` | same as `/a/b` | repeated slashes and `.` segments are dropped |
| bytes < 0x20, 0x7F, `\` | 400 | control characters and backslashes |
| symlink pointing outside the root | 404 | the canonical (`realpath`) result must lie inside the root |
| device, FIFO, socket | 404 | regular files only |

## 12. Requirements and dependencies

* A C11 compiler (`cc`/`gcc`/`clang`) and `make`.
* Python 3 for the integration tests and the reference server `tools/refserver.py` (standard library only; tested with 3.12).
* Linux or another POSIX system. Developed and tested on Ubuntu 24.04 with gcc 13. `make test-unit` links one test
  with GNU ld's `--wrap`, so the unit tests need GNU binutils (Linux); `make` itself needs only POSIX.
* No other libraries, no network access, no configuration files.

## 13. Build

```sh
make            # ./bcurl   (warnings are enabled: -Wall -Wextra -Wpedantic; there are none)
make clean      # remove ./bcurl and ./build
```

## 14. Run

```sh
./bcurl [-v|-vv] [-H 'name: value']... <host:port/path> [</path> ...]
```

| Option | Meaning |
|---|---|
| `-v` | hex-dump every frame sent (`TX`) and received (`RX`) on stderr (long frames are cut after 256 bytes) |
| `-vv` | the same, nothing cut |
| `-H 'name: value'` | add a request header (repeatable) |
| `host:port/path` | the target; `[::1]:9000/x` and a `bhtp://` prefix are accepted; a missing path means `/` |
| `/more /paths` | fetched over the **same** connection |

To have something to talk to, start either server:
`../nistha-binary-http/bserve ../nistha-binary-http/www 9000` (Nistha's) or `python3 tools/refserver.py fixtures/www 9000` (written from the spec alone).

## 15. Tests

```sh
make test          # everything below
make test-unit     # C unit tests only
make asan          # the same tests again, with AddressSanitizer + UBSan
BCURL_SLOW_TESTS=1 python3 tests/test_client.py     # also run the 30 s silent-server timeout test
```

| Suite | What it covers |
|---|---|
| `tests/test_protocol.c` | encode/decode round trips, every truncation, invalid tags/lengths/names, random-byte decoding |
| `tests/test_frame.c` | frame reading: valid, unknown skipped, bad magic/version, oversize, truncation, flags, EINTR |
| `tests/test_shortio.c` | forced short reads and writes |
| `tests/test_url.c` | target parsing and the hexdump formatter |
| `tests/test_client.py` | **integration**: runs the real `bcurl` against a scriptable mock server written from SPEC.md: exact request bytes, 2xx/4xx/5xx exit codes, every kind of server misbehaviour, unknown frames, TCP fragmentation (1 byte at a time, split at every offset, many frames in one read), one-connection proof, `-v` output, command line. Also runs against `tools/refserver.py` |
| `tests/test_examples.py` | the checked-in hexdumps are the frame the spec says `bcurl` builds, and a real `bcurl` understands the example response byte for byte |

## 16. Verification - "is it actually working?"

`scripts/verify.sh` runs the whole procedure below and prints PASS/FAIL per step. With no extra setup it
uses `./bcurl` and the reference server; to test the real pair: `BSERVE=../nistha-binary-http/bserve scripts/verify.sh`.
By hand: terminal 1 `python3 tools/refserver.py fixtures/www 9000` (or Nistha's `bserve`); terminal 2 the commands:

| Step | Command | Expected |
|---|---|---|
| 1 Build | `make` | no compiler errors or warnings |
| 2 Files | `ls fixtures/www` | `hello.txt index.html` (with Nistha's server: its `www/`, which also has `binary.bin`) |
| 3 Start | `python3 tools/refserver.py fixtures/www 9000` | `serving ... on port 9000` |
| 4 Basic | `./bcurl localhost:9000/index.html; echo $?` | the page, `0` |
| 5 Verbose | `./bcurl -v localhost:9000/hello.txt` | `TX FRAME #1 REQUEST`, `RX FRAME #1 RESPONSE`, `RX FRAME #2 DATA`, with hex lines |
| 6 404 | `./bcurl localhost:9000/nope.html; echo $?` | `-> 404 Not Found` and `1` |
| 7 One connection | `./bcurl -v localhost:9000/index.html /hello.txt /missing.txt 2>&1 >/dev/null \| grep '^\*'` | `* done: 3 request(s) completed over 1 TCP connection(s)` |
| 8 Unknown frame | `python3 tests/test_client.py -v TestUnknownFrames` | 10 tests OK: the server sends known / unknown / known, bcurl prints the body |
| 9 Malformed | `python3 tests/test_client.py -v TestFailures` | bad magic/version/length/flags, truncation... each exits 2 with a message |
| 10 Traversal | `./bcurl localhost:9000/../../etc/passwd; echo $?` | `-> 400 Bad Request`, `1`; the bytes sent were really `/../../etc/passwd` |
| 11 Binary | `./bcurl localhost:9000/binary.bin > /tmp/out.bin && cmp ../nistha-binary-http/www/binary.bin /tmp/out.bin && echo identical` | `identical` (needs Nistha's `bserve ../nistha-binary-http/www 9000`) |
| 12 Suite | `make test` | `==== ALL TESTS PASSED ====` |

**Proving "never a second connection":** (a) `-v` prints `TCP connection #1, the only one` and ends with
`done: N request(s) completed over 1 TCP connection(s)`; (b) the server log shows one `accepted` for N requests (run Nistha's `bserve`);
(c) `TestOneConnection` asserts it automatically against a mock that counts `accept()` calls, including
`test_failure_in_the_middle_stops_without_reconnecting` (after an error the client does *not* reconnect) and
`test_a_different_server_is_refused_before_connecting`.

**Binary correctness:** step 11 uses `cmp`; `test_binary_body_spanning_many_data_frames_is_byte_exact` does the same in the suite.

## 17. Example commands and output

Real output, captured from this client talking to Nistha's server:

```
$ ./bcurl localhost:9000/index.html
<!DOCTYPE html>
<html>
<head>
    <title>Nistha BHTP Server</title>
</head>
<body>
    <h1>Hello from Nistha's BHTP Server!</h1>
    <p>This page was served using the Binary HTTP-like Protocol.</p>
</body>
</html>
[exit=0]
```
```
$ ./bcurl -v localhost:9000/hello.txt
* connected to localhost port 9000 (TCP connection #1, the only one)
* GET /hello.txt  (request 1 on connection #1)
TX FRAME #1  REQUEST  flags=0x0000  payload=73  total=85
0000: 42 48 54 50 01 01 00 00  00 00 00 49 01 00 0a 2f  |BHTP.......I.../|
0010: 68 65 6c 6c 6f 2e 74 78  74 05 01 00 0e 6c 6f 63  |hello.txt....loc|
0020: 61 6c 68 6f 73 74 3a 39  30 30 30 02 00 09 62 63  |alhost:9000...bc|
0030: 75 72 6c 2f 31 2e 30 06  00 03 2a 2f 2a 05 00 08  |url/1.0...*/*...|
0040: 69 64 65 6e 74 69 74 79  08 00 0a 6b 65 65 70 2d  |identity...keep-|
0050: 61 6c 69 76 65                                    |alive|

RX FRAME #1  RESPONSE  flags=0x0000  payload=90  total=102
0000: 42 48 54 50 01 02 00 00  00 00 00 5a 00 c8 06 03  |BHTP.......Z....|
0010: 00 0a 74 65 78 74 2f 70  6c 61 69 6e 04 00 02 38  |..text/plain...8|
0020: 37 07 00 0a 62 73 65 72  76 65 2f 31 2e 30 09 00  |7...bserve/1.0..|
0030: 1d 46 72 69 2c 20 30 39  20 4f 63 74 20 32 30 32  |.Fri, 09 Oct 202|
0040: 36 20 30 34 3a 35 35 3a  34 39 20 47 4d 54 0a 00  |6 04:55:49 GMT..|
0050: 08 6e 6f 2d 63 61 63 68  65 08 00 0a 6b 65 65 70  |.no-cache...keep|
0060: 2d 61 6c 69 76 65                                 |-alive|

< status 200 OK
< content-type: text/plain    (static name, 1 byte)
< content-length: 87    (static name, 1 byte)
< server: bserve/1.0    (static name, 1 byte)
< last-modified: Fri, 09 Oct 2026 04:55:49 GMT    (static name, 1 byte)
< cache-control: no-cache    (static name, 1 byte)
< connection: keep-alive    (static name, 1 byte)
RX FRAME #2  DATA  flags=0x0001  payload=87  total=99
0000: 42 48 54 50 01 03 00 01  00 00 00 57 48 65 6c 6c  |BHTP.......WHell|
0010: 6f 20 66 72 6f 6d 20 4e  69 73 74 68 61 27 73 20  |o from Nistha's |
0020: 42 69 6e 61 72 79 20 48  54 54 50 20 53 65 72 76  |Binary HTTP Serv|
```
```
$ ./bcurl localhost:9000/nope.html
bcurl: GET /nope.html -> 404 Not Found
[exit=1]
```
```
$ ./bcurl localhost:9000/index.html /hello.txt /missing.txt
bcurl: GET /missing.txt -> 404 Not Found
* connected to localhost port 9000 (TCP connection #1, the only one)
* GET /index.html  (request 1 on connection #1)
* response complete: status 200, 213 body bytes in 1 DATA frame(s)
* GET /hello.txt  (request 2 on connection #1)
* response complete: status 200, 87 body bytes in 1 DATA frame(s)
* GET /missing.txt  (request 3 on connection #1)
* response complete: status 404, 0 body bytes in 0 DATA frame(s)
* done: 3 request(s) completed over 1 TCP connection(s); exit status 1
```
```
$ ./bcurl localhost:9000/../../etc/passwd
bcurl: GET /../../etc/passwd -> 400 Bad Request
[exit=1]
$ ./bcurl localhost:9000/%2e%2e/%2e%2e/etc/passwd
bcurl: GET /%2e%2e/%2e%2e/etc/passwd -> 404 Not Found
[exit=1]
$ ./bcurl localhost:9000/sub/../../x
bcurl: GET /sub/../../x -> 400 Bad Request
[exit=1]
$ ./bcurl localhost:9000//etc/passwd
bcurl: GET //etc/passwd -> 404 Not Found
[exit=1]
$ ./bcurl localhost:9000/sub/
<!DOCTYPE html>
<html><body><h1>Sub directory index</h1></body></html>
[exit=0]
$ ./bcurl localhost:9000/sub
<!DOCTYPE html>
<html><body><h1>Sub directory index</h1></body></html>
[exit=0]
```
```
$ ./bcurl localhost:1/x   (nothing listening)
bcurl: cannot connect to localhost:1: Connection refused
[exit=2]
$ ./bcurl
usage: ./bcurl [-v|-vv] [-H 'name: value']... <host:port/path> [</path> ...]

  -v     hex-dump every frame sent (TX) and received (RX) on stderr
[exit=64]
```
```
$ ./bcurl localhost:9000/binary.bin > /tmp/out.bin
[exit=0]
cmp: identical
f4af937b3105a601785c1fb39cfe97d1333b5205eb16b2db3a707a5305c52d58  binary.bin
f4af937b3105a601785c1fb39cfe97d1333b5205eb16b2db3a707a5305c52d58  out.bin
```

## 18. Hexdump example

`examples/request.hexdump` and `examples/response.hexdump` hold one complete, real exchange
(`GET /index.html`): the raw bytes as they crossed the TCP connection, followed by a table that
annotates every byte. They are **generated, never typed**: `scripts/gen_hexdumps.sh` runs the
real programs, and `tests/test_examples.py` replays the example response into a real `bcurl` and fails if it does not print and decode it unchanged, or if the annotations differ. An excerpt of the request:

```
TX FRAME #1  REQUEST  (86 bytes)
  0000: 42 48 54 50 01 01 00 00  00 00 00 4a 01 00 0b 2f  |BHTP.......J.../|
  ...
  Offset  Bytes                      Meaning
  0x0000  42 48 54 50                magic = "BHTP"
  0x0004  01                         version = 1
  0x0005  01                         type = 0x01  REQUEST
  0x0006  00 00                      flags = 0x0000  (none set)
  0x0008  00 00 00 4a                payload_length = 74
  0x000c  01                         method = 0x01  GET
  0x000d  00 0b                      path_length = 11
  0x000f  2f 69 6e 64 65 78 2e 68    path = "/index.html"
  0x001a  05                         header_count = 5
  0x001b  01                         header[0] tag = 1  -> static name "host"
```

(The `last-modified` value inside the response example depends on the file's modification time;
the generator pins it to 2026-01-01 so the bytes are reproducible.)

## 19. Repository structure

```
veekshitha-binary-http/
├── README.md  SPEC.md  VIVA.md  Makefile  .gitignore
├── include/    client.h  connect.h  frame.h  hexdump.h  io.h  protocol.h  url.h
├── src/        bcurl.c  client.c  connect.c  url.c  hexdump.c  protocol.c  frame.c  io.c
├── tests/      test_protocol.c  test_frame.c  test_shortio.c  test_url.c
│               test_client.py  test_examples.py  bhtp.py (Python peer written from SPEC.md)
├── tools/      refserver.py (spec-only server)  pyfetch.py (spec-only client)  make_examples.py
├── scripts/    verify.sh  gen_hexdumps.sh
├── examples/   request.hexdump  response.hexdump
└── fixtures/www/  index.html  hello.txt      (documents for the reference server)
```

## 20. Design decisions

| Decision | Why |
|---|---|
| 12-byte fixed header with magic and version | A fixed size means one `read_exact(12)` always works. Magic rejects non-BHTP peers (HTTP text, port scanners) on the very first frame; version gives a path to version 2. |
| `payload_length` is u32, accepted limit 16 384 | The field never has to change; the limit is a policy that can be raised later. 16 384 is the HTTP/2 default frame size and keeps every buffer on the stack. |
| `type` u8, `flags` u16 | 256 types and 16 flag bits leave room to grow at a cost of three bytes. |
| No stream/request ID (HTTP/2 spends 31 bits on it) | Exchanges are strictly sequential, so a response always belongs to the oldest request. An ID would be dead weight. |
| Big-endian | Network byte order: `00 00 00 4a` reads as 74 in a hexdump, and no byte swapping depends on the host CPU. |
| Body split into DATA frames with `END_STREAM` | Memory stays bounded for any file size; the client knows exactly when the response ends without closing the connection. |
| Static table of 10 + literals | HPACK's two cheapest ideas: the common names cost one byte, anything else still works. |
| Unknown frame types are skipped | The one rule that makes extension possible. |
| Malformed payload -> 400 and stay open; broken framing -> ERROR and close | If the length field was honoured the stream is still in sync, so there is no reason to drop a healthy connection. If it was not, nothing after it can be trusted. |
| Server forks one process per connection | The simplest thing that lets a persistent connection coexist with other clients; each process has its own stack, so a bug cannot corrupt another connection. |

Client-specific choices: `-v` output goes to **stderr** so stdout is exactly the body (pipe it, `cmp` it); the target is validated
before connecting; an error status does not stop a multi-path run because the connection is still healthy, but a transport or protocol
error does; the client checks `content-length` against the DATA bytes so a truncated body is an error, not a silent short file.

## 21. Limitations and future improvements

* GET only; one request at a time (no pipelining, no multiplexing - by design).
* A fixed 30 s timeout; no retry, no redirect, no `-o file`, no TLS.
* `-v` truncates frames after 256 bytes unless `-vv` is given.
* A single connection per run: to talk to two servers, run `bcurl` twice.
* Natural version 2: frame types the client does not know are skipped today, so servers can start sending new ones (e.g. `PING`, `SETTINGS`) without breaking it.

## 22. Requirements matrix

| Requirement | Implemented | Tested | How to verify |
|---|---|---|---|
| Builds the binary request frame | YES | YES | `TestRequestFrame.test_request_bytes_match_the_spec_exactly` |
| Reads the response, body to stdout | YES | YES | `TestResponses` (HTML, text, empty, binary over many DATA frames, byte-exact) |
| `-v` hexdumps every frame (TX/RX) | YES | YES | `TestVerbose.test_dumped_bytes_are_exactly_the_bytes_on_the_wire`, `test_every_frame_of_a_multi_request_session_is_dumped`; step 5 |
| stdout carries only the body | YES | YES | `TestVerbose.test_stdout_carries_only_the_body` |
| Exit non-zero on 4xx / 5xx | YES | YES | `test_every_4xx_and_5xx_exits_nonzero`, `test_404_exits_1_and_says_so`; step 6 |
| Exit 0 on 2xx | YES | YES | `test_every_2xx_and_3xx_exits_zero` |
| Transport / protocol failures exit 2, usage 64 | YES | YES | `TestFailures` (19 cases), `TestCommandLine` |
| **Never opens a second connection** | YES | YES | `TestOneConnection` (`test_three_requests_one_connection`, `test_fifty_requests_on_one_connection`, `test_failure_in_the_middle_stops_without_reconnecting`) |
| Several paths over one connection | YES | YES | step 7; `test_full_urls_for_the_same_server_share_the_connection` |
| Parses host, port, path (IPv4, IPv6, names) | YES | YES | `tests/test_url.c`, `TestRequestFrame.test_localhost_name_resolves_and_connects` |
| Works against a server it did not share code with | YES | YES | `TestReferenceServer` (`tools/refserver.py`); `BSERVE=../nistha-binary-http/bserve scripts/verify.sh` (all 27 checks pass) |
| Binary frames, fixed-size header, fields defended | YES | YES | `SPEC.md` s2-3; `make test-unit` (`test_frame`, `test_protocol`) |
| Header compression/indexing (10 static names + length-prefixed literals) | YES | YES | `SPEC.md` s6; `test_protocol`; ``test_client.py`` header tests |
| Unknown frame type is skipped, next frame still processed | YES | YES | step 8 below; `test_frame`, `test_shortio` (one-byte reads), ``test_client.py`` unknown-frame tests |
| TCP is a byte stream: partial reads/writes, split and glued frames, EINTR | YES | YES | `test_shortio`, `test_frame` (`test_eintr_is_retried`), ``test_client.py`` TCP tests |
| Frame validation: lengths, flags, header count, truncation, no unchecked allocation | YES | YES | `test_frame`, `test_protocol`, ``test_client.py`` protocol tests |
| Annotated hexdump of one real request and response | YES | YES | `examples/*.hexdump`; `python3 tests/test_examples.py` |
| Spec ("two pages, enough for a stranger") | YES | YES | `SPEC.md`; ``tests/bhtp.py` and `tools/refserver.py`` is a second implementation written from it alone |
| Memory safety | YES | YES | `make asan` (all tests under AddressSanitizer + UBSan) |
| Server side (404/400/500, path security, keep-alive) | in the partner repo | YES (partner) | `nistha-binary-http`; step 10 here shows the client side of it |

## 23. Demo for the professor (3-5 minutes)

1. **Server:** `cd ../nistha-binary-http && make && ./bserve ./www 9000` (leave the log visible), or `python3 tools/refserver.py fixtures/www 9000`.
2. **Spec:** open `SPEC.md` - the 12-byte header table and the "unknown frames" section.
3. **`-v`:** `./bcurl -v localhost:9000/hello.txt`. Point at `TX FRAME #1 REQUEST`, magic `42 48 54 50`, `payload=73`, then `RX FRAME #1 RESPONSE` and `RX FRAME #2 DATA flags=0x0001`, and the decoded headers with `(static name, 1 byte)`.
4. **Annotated bytes:** `less examples/request.hexdump`.
5. **404 and exit status:** `./bcurl localhost:9000/nope.html; echo $?` -> `404`, `1`.
6. **One connection:** `./bcurl -v localhost:9000/index.html /hello.txt /missing.txt` -> `* done: 3 request(s) completed over 1 TCP connection(s)`; the server log shows one `accepted`.
7. **Unknown frame:** `python3 tests/test_client.py -v TestUnknownFrames` - the server sends known / unknown / known and `bcurl` prints the page.
8. **Hostile server:** `python3 tests/test_client.py -v TestFailures` - each broken-server case fails cleanly with exit 2.
9. **Traversal:** `./bcurl localhost:9000/../../etc/passwd; echo $?` -> `400`, `1`.
10. **Tests:** `make test` -> `==== ALL TESTS PASSED ====`.
