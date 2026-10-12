#!/usr/bin/env python3
"""
refserver.py - a complete BHTP/1 server in Python, written from SPEC.md alone.

It exists so that bcurl can be demonstrated and tested against a server that
shares no code with it ("a client that only works against your own server is
an implementation, not a protocol"). It is deliberately small and slow.

    python3 tools/refserver.py <root_directory> <port>      (port 0 = pick one)
"""
import email.utils
import os
import socket
import struct
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests"))
import bhtp  # noqa: E402

CTYPES = {".html": "text/html", ".htm": "text/html", ".txt": "text/plain", ".css": "text/css",
          ".js": "application/javascript", ".json": "application/json", ".png": "image/png",
          ".jpg": "image/jpeg", ".gif": "image/gif", ".svg": "image/svg+xml"}


def resolve(root, raw):
    """SPEC path rules. Returns (status, filename-or-None)."""
    if not raw.startswith(b"/"):
        return 400, None
    if any(c < 0x20 or c == 0x7F or c == 0x5C for c in raw):
        return 400, None
    parts = []
    for seg in raw.split(b"/"):
        if seg in (b"", b"."):
            continue
        if seg == b"..":
            return 400, None
        parts.append(seg)
    path = os.path.join(root, *[p.decode("utf-8", "surrogateescape") for p in parts]) if parts else root
    try:
        real = os.path.realpath(path)
        if os.path.isdir(real):
            real = os.path.realpath(os.path.join(real, "index.html"))
        if not (real == root or real.startswith(root + os.sep)) or not os.path.isfile(real):
            return 404, None
        if raw.endswith(b"/") and not os.path.isdir(path):
            return 404, None
    except OSError:
        return 404, None
    return 200, real


class Session(threading.Thread):
    def __init__(self, sock, root, n):
        threading.Thread.__init__(self, daemon=True)
        self.sock, self.root, self.n = sock, root, n

    def log(self, msg):
        sys.stderr.write("[conn=%d] %s\n" % (self.n, msg))
        sys.stderr.flush()

    def recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                return None if not buf else b"\0TRUNC"
            buf += chunk
        return buf

    def status_only(self, status):
        h = [("server", "refserver/1"), ("content-length", "0"), ("connection", "keep-alive")]
        self.sock.sendall(bhtp.frame(bhtp.RESPONSE, bhtp.encode_response(status, h), bhtp.END_STREAM))

    def run(self):
        self.log("accepted")
        try:
            while True:
                head = self.recv_exact(bhtp.HEADER_SIZE)
                if head is None or head == b"\0TRUNC":
                    break
                if head[:4] != bhtp.MAGIC or head[4] != bhtp.VERSION:
                    self.sock.sendall(bhtp.frame(bhtp.ERROR, struct.pack(">H", 400) + b"bad header"))
                    break
                _, ftype, flags, length = bhtp.decode_header_block(head)
                if ftype not in (1, 2, 3, 4):
                    remaining = length                      # unknown type: skip exactly `length` bytes
                    while remaining:
                        chunk = self.sock.recv(min(remaining, 65536))
                        if not chunk:
                            return
                        remaining -= len(chunk)
                    continue
                if length > bhtp.MAX_PAYLOAD:
                    self.sock.sendall(bhtp.frame(bhtp.ERROR, struct.pack(">H", 400) + b"frame too large"))
                    break
                payload = self.recv_exact(length) if length else b""
                if payload == b"\0TRUNC" or payload is None:
                    break
                if ftype != bhtp.REQUEST or flags & ~0 and flags:
                    self.status_only(400)
                    continue
                try:
                    method, path, headers = bhtp.decode_request(payload)
                    if method != bhtp.GET:
                        raise bhtp.ProtocolError("method")
                    if not path:
                        raise bhtp.ProtocolError("empty path")
                except Exception:
                    self.status_only(400)
                    continue
                status, real = resolve(self.root, path)
                self.log("GET %r -> %d" % (path, status))
                if status != 200:
                    self.status_only(status)
                    continue
                with open(real, "rb") as f:
                    body = f.read()
                ext = os.path.splitext(real)[1].lower()
                h = [("content-type", CTYPES.get(ext, "application/octet-stream")),
                     ("content-length", str(len(body))),
                     ("last-modified", email.utils.formatdate(os.path.getmtime(real), usegmt=True)),
                     ("server", "refserver/1"), ("cache-control", "no-cache"),
                     ("connection", "keep-alive")]
                if not body:
                    self.sock.sendall(bhtp.frame(bhtp.RESPONSE, bhtp.encode_response(200, h), bhtp.END_STREAM))
                    continue
                out = [bhtp.frame(bhtp.RESPONSE, bhtp.encode_response(200, h))]
                for i in range(0, len(body), 16384):
                    last = i + 16384 >= len(body)
                    out.append(bhtp.frame(bhtp.DATA, body[i:i + 16384], bhtp.END_STREAM if last else 0))
                self.sock.sendall(b"".join(out))
        except (OSError, bhtp.ProtocolError):
            pass
        finally:
            self.log("closed")
            try:
                self.sock.close()
            except OSError:
                pass


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: refserver.py <root_directory> <port>")
    root = os.path.realpath(sys.argv[1])
    if not os.path.isdir(root):
        sys.exit("refserver: %s is not a directory" % root)
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("", int(sys.argv[2])))
    srv.listen(16)
    sys.stderr.write("refserver: serving %s on port %d\n" % (root, srv.getsockname()[1]))
    sys.stderr.flush()
    n = 0
    while True:
        sock, _ = srv.accept()
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        n += 1
        Session(sock, root, n).start()


if __name__ == "__main__":
    main()
