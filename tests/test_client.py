#!/usr/bin/env python3
"""
Integration tests for bcurl.

bcurl is run as a real subprocess against a scriptable MOCK SERVER written in
Python from SPEC.md alone (tests/bhtp.py). The mock decides exactly which
frames to send, how to chop them into TCP segments and when to hang up, so the
client can be tested against behaviour a friendly server would never show.

    python3 tests/test_client.py            # uses ./bcurl
    BCURL=./build/bcurl_asan python3 tests/test_client.py
"""
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import bhtp  # noqa: E402

BCURL = os.environ.get("BCURL", os.path.join(ROOT, "bcurl"))
FIXTURES = os.path.join(ROOT, "fixtures", "www")


# ---------------------------------------------------------------------------
# frame builders
# ---------------------------------------------------------------------------

def response_frames(status, body=b"", chunk=bhtp.MAX_PAYLOAD, headers=None, clen=None):
    """RESPONSE (+ DATA...) exactly as a correct server would send them."""
    if headers is None:
        headers = [("content-type", "text/plain"),
                   ("content-length", str(len(body) if clen is None else clen)),
                   ("server", "mock/1"), ("connection", "keep-alive")]
    payload = bhtp.encode_response(status, headers)
    if not body:
        return [bhtp.frame(bhtp.RESPONSE, payload, bhtp.END_STREAM)]
    out = [bhtp.frame(bhtp.RESPONSE, payload)]
    for i in range(0, len(body), chunk):
        last = i + chunk >= len(body)
        out.append(bhtp.frame(bhtp.DATA, body[i:i + chunk], bhtp.END_STREAM if last else 0))
    return out


def unknown_frame(ftype=0x7F, payload=b"future extension", flags=0):
    return bhtp.frame(ftype, payload, flags)


# ---------------------------------------------------------------------------
# the mock server
# ---------------------------------------------------------------------------

class Request(object):
    def __init__(self, method, path, headers, raw):
        self.method, self.path, self.headers, self.raw = method, path, headers, raw


class ConnRecord(object):
    def __init__(self):
        self.requests = []     # Request objects, in order
        self.received = b""    # every byte the client sent on this connection
        self.sent = b""        # every byte we sent back
        self.closed_by_client = False


class Mock(object):
    """handler(conn, request, n) -> list of byte strings to send, or an object
    from the helpers below (Close / Raw). n counts requests on this connection."""

    def __init__(self, handler, split=None, delay=0.0):
        self.handler, self.split, self.delay = handler, split, delay
        self.records = []
        self.lock = threading.Lock()
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.port = self.listener.getsockname()[1]
        self.stop = False
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()

    def _accept_loop(self):
        while not self.stop:
            try:
                sock, _ = self.listener.accept()
            except OSError:
                return
            rec = ConnRecord()
            with self.lock:
                self.records.append(rec)
            threading.Thread(target=self._serve, args=(sock, rec), daemon=True).start()

    def _recv_exact(self, sock, buf, n):
        while len(buf) < n:
            chunk = sock.recv(65536)
            if not chunk:
                return None
            buf += chunk
        return buf

    def _serve(self, sock, rec):
        sock.settimeout(10)
        buf = b""
        n = 0
        try:
            while True:
                buf = self._recv_exact(sock, buf, bhtp.HEADER_SIZE)
                if buf is None:
                    rec.closed_by_client = True
                    return
                _, ftype, flags, length = bhtp.decode_header_block(buf[:12])
                buf = self._recv_exact(sock, buf, 12 + length)
                if buf is None:
                    rec.closed_by_client = True
                    return
                raw, buf = buf[:12 + length], buf[12 + length:]
                rec.received += raw
                if ftype != bhtp.REQUEST:
                    continue
                method, path, headers = bhtp.decode_request(raw[12:])
                req = Request(method, path, headers, raw)
                rec.requests.append(req)
                n += 1
                out = self.handler(rec, req, n)
                if out is CLOSE:
                    sock.close()
                    return
                if isinstance(out, Hang):
                    time.sleep(out.seconds)
                    continue
                self._send(sock, rec, out)
                if getattr(out, "close_after", False):
                    sock.close()
                    return
        except (OSError, socket.timeout):
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _send(self, sock, rec, frames):
        data = b"".join(frames)
        rec.sent += data
        if self.split == "bytes":
            for i in range(len(data)):
                sock.sendall(data[i:i + 1])
                if self.delay:
                    time.sleep(self.delay)
        elif isinstance(self.split, int):
            sock.sendall(data[:self.split])
            time.sleep(self.delay or 0.02)
            sock.sendall(data[self.split:])
        elif self.split == "each":           # one segment per frame
            for f in frames:
                sock.sendall(f)
                time.sleep(self.delay or 0.005)
        else:                                # everything in one write
            sock.sendall(data)

    def close(self):
        self.stop = True
        self.listener.close()


class Hang(object):
    def __init__(self, seconds):
        self.seconds = seconds


class Frames(list):
    """A list of frames; close_after=True hangs up once they are sent."""
    close_after = False


CLOSE = object()


def closing(frames):
    f = Frames(frames)
    f.close_after = True
    return f


def read_file(path):
    with open(path, "rb") as f:
        return f.read()


def run(args, timeout=30):
    p = subprocess.run([BCURL] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    return p.returncode, p.stdout, p.stderr.decode("utf-8", "replace")


def parse_dump(stderr):
    """Extract every hex-dumped frame from -v output: [(dir, seq, bytes)]."""
    frames, cur = [], None
    for line in stderr.splitlines():
        m = re.match(r"^(TX|RX) FRAME #(\d+)", line)
        if m:
            cur = [m.group(1), int(m.group(2)), b""]
            frames.append(cur)
            continue
        m = re.match(r"^[0-9a-f]{4}: ((?:[0-9a-f]{2} ?| )+?) +\|", line)
        if m and cur is not None:
            cur[2] += bytes.fromhex(m.group(1).replace(" ", ""))
    return [tuple(f) for f in frames]


class MockCase(unittest.TestCase):
    def setUp(self):
        self.mocks = []

    def tearDown(self):
        for m in self.mocks:
            m.close()

    def mock(self, handler, **kw):
        m = Mock(handler, **kw)
        self.mocks.append(m)
        return m

    def serve_body(self, body, **kw):
        return self.mock(lambda rec, req, n: response_frames(200, body), **kw)

    def url(self, m, path="/index.html"):
        return "127.0.0.1:%d%s" % (m.port, path)


# ---------------------------------------------------------------------------
# the request frame the client builds
# ---------------------------------------------------------------------------

class TestRequestFrame(MockCase):
    def test_request_bytes_match_the_spec_exactly(self):
        m = self.serve_body(b"hi")
        rc, out, err = run([self.url(m, "/index.html")])
        self.assertEqual(rc, 0, err)
        want = bhtp.request_frame("/index.html", [
            ("host", "127.0.0.1:%d" % m.port), ("user-agent", "bcurl/1.0"),
            ("accept", "*/*"), ("accept-encoding", "identity"), ("connection", "keep-alive")])
        self.assertEqual(m.records[0].received, want)

    def test_static_header_names_use_one_byte_tags(self):
        m = self.serve_body(b"hi")
        run([self.url(m)])
        method, path, headers = bhtp.decode_request(m.records[0].requests[0].raw[12:])
        self.assertEqual(method, bhtp.GET)
        self.assertEqual(path, b"/index.html")
        tags = {name: tag for name, _, tag in headers}
        self.assertEqual(tags, {"host": 1, "user-agent": 2, "accept": 6,
                                "accept-encoding": 5, "connection": 8})

    def test_custom_header_is_sent_as_a_literal_name(self):
        m = self.serve_body(b"hi")
        rc, _, err = run(["-H", "X-Demo: hello world", self.url(m)])
        self.assertEqual(rc, 0, err)
        _, _, headers = bhtp.decode_request(m.records[0].requests[0].raw[12:])
        got = {n: (v, t) for n, v, t in headers}
        self.assertEqual(got["x-demo"], (b"hello world", 0))     # tag 0 = literal

    def test_custom_header_with_a_static_name_gets_a_tag(self):
        m = self.serve_body(b"hi")
        run(["-H", "cache-control:no-cache", self.url(m)])
        _, _, headers = bhtp.decode_request(m.records[0].requests[0].raw[12:])
        got = {n: (v, t) for n, v, t in headers}
        self.assertEqual(got["cache-control"], (b"no-cache", 10))

    def test_path_is_sent_verbatim_not_normalised(self):
        m = self.serve_body(b"x")
        for p in ["/../../etc/passwd", "//a//b/./c", "/%2e%2e/x", "/a b"]:
            run([self.url(m, p)])
        sent = [r.requests[0].path for r in m.records]
        self.assertEqual(sent, [b"/../../etc/passwd", b"//a//b/./c", b"/%2e%2e/x", b"/a b"])

    def test_url_without_path_requests_slash(self):
        m = self.serve_body(b"x")
        run(["127.0.0.1:%d" % m.port])
        self.assertEqual(m.records[0].requests[0].path, b"/")

    def test_localhost_name_resolves_and_connects(self):
        m = self.serve_body(b"via name")
        rc, out, err = run(["localhost:%d/index.html" % m.port])
        self.assertEqual((rc, out), (0, b"via name"), err)


# ---------------------------------------------------------------------------
# responses: bodies and exit status
# ---------------------------------------------------------------------------

class TestResponses(MockCase):
    def test_200_html_body_goes_to_stdout_exit_0(self):
        body = read_file(os.path.join(FIXTURES, "index.html"))
        m = self.serve_body(body)
        rc, out, err = run([self.url(m)])
        self.assertEqual((rc, out), (0, body))
        self.assertEqual(err, "")                      # quiet on success

    def test_200_text_file(self):
        body = read_file(os.path.join(FIXTURES, "hello.txt"))
        m = self.serve_body(body)
        rc, out, _ = run([self.url(m, "/hello.txt")])
        self.assertEqual((rc, out), (0, body))

    def test_empty_body_is_success_with_empty_stdout(self):
        m = self.serve_body(b"")
        rc, out, err = run([self.url(m)])
        self.assertEqual((rc, out), (0, b""), err)

    def test_binary_body_spanning_many_data_frames_is_byte_exact(self):
        body = bytes(range(256)) * 700 + b"\x00\xff\r\n\x00"      # ~175 KB, all byte values
        m = self.serve_body(body, )
        m.handler = lambda rec, req, n: response_frames(200, body, chunk=16384)
        rc, out, err = run([self.url(m, "/binary.bin")])
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, body)
        self.assertGreaterEqual(len(m.records[0].sent) // 16384, 10)

    def test_body_with_no_content_length_header_is_accepted(self):
        m = self.mock(lambda rec, req, n: response_frames(200, b"abc", headers=[("server", "x")]))
        rc, out, _ = run([self.url(m)])
        self.assertEqual((rc, out), (0, b"abc"))

    def test_404_exits_1_and_says_so(self):
        m = self.mock(lambda rec, req, n: response_frames(404, b""))
        rc, out, err = run([self.url(m, "/missing")])
        self.assertEqual(rc, 1)
        self.assertEqual(out, b"")
        self.assertIn("404", err)
        self.assertIn("/missing", err)

    def test_every_4xx_and_5xx_exits_nonzero(self):
        for status in (400, 403, 404, 405, 418, 499, 500, 501, 503, 599):
            m = self.mock(lambda rec, req, n, s=status: response_frames(s, b""))
            rc, _, err = run([self.url(m)])
            self.assertEqual(rc, 1, "status %d: %s" % (status, err))

    def test_every_2xx_and_3xx_exits_zero(self):
        for status in (200, 201, 204, 206, 301, 304):
            m = self.mock(lambda rec, req, n, s=status: response_frames(s, b""))
            rc, _, err = run([self.url(m)])
            self.assertEqual(rc, 0, "status %d: %s" % (status, err))

    def test_error_response_with_a_body_still_prints_the_body_and_fails(self):
        m = self.mock(lambda rec, req, n: response_frames(500, b"boom\n"))
        rc, out, _ = run([self.url(m)])
        self.assertEqual((rc, out), (1, b"boom\n"))

    def test_error_frame_exits_1_and_reports_the_reason(self):
        err_frame = bhtp.frame(bhtp.ERROR, struct.pack(">H", 400) + b"bad magic")
        m = self.mock(lambda rec, req, n: closing([err_frame]))
        rc, out, err = run([self.url(m)])
        self.assertEqual((rc, out), (1, b""))
        self.assertIn("400", err)
        self.assertIn("bad magic", err)


# ---------------------------------------------------------------------------
# network and protocol failures: exit status 2, never a crash or a hang
# ---------------------------------------------------------------------------

class TestFailures(MockCase):
    def assertFails(self, handler, expect_text=None, **kw):
        m = self.mock(handler, **kw)
        rc, out, err = run([self.url(m)], timeout=20)
        self.assertEqual(rc, 2, "stderr: %s" % err)
        if expect_text:
            self.assertIn(expect_text, err)
        return rc, out, err

    def test_connection_refused(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()                                      # nobody listens here now
        rc, _, err = run(["127.0.0.1:%d/x" % port])
        self.assertEqual(rc, 2)
        self.assertIn("cannot connect", err)

    def test_unresolvable_host(self):
        rc, _, err = run(["no-such-host.invalid:9000/x"])
        self.assertEqual(rc, 2)
        self.assertIn("resolve", err)

    def test_server_closes_without_answering(self):
        self.assertFails(lambda rec, req, n: CLOSE, "closed the connection")

    def test_server_closes_after_response_header_before_body(self):
        frames = response_frames(200, b"x" * 100)[:1]
        self.assertFails(lambda rec, req, n: closing(frames), "closed the connection")

    def test_server_dies_in_the_middle_of_a_frame(self):
        data = b"".join(response_frames(200, b"y" * 1000))
        cut = data[:len(data) - 400]
        self.assertFails(lambda rec, req, n: closing([cut]), "middle of a frame")

    def test_server_dies_in_the_middle_of_a_frame_header(self):
        self.assertFails(lambda rec, req, n: closing([b"BHTP\x01\x02"]), "middle of a frame")

    def test_server_speaks_http_instead(self):
        self.assertFails(lambda rec, req, n: closing([b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"]),
                         "invalid frame")

    def test_wrong_version_from_server(self):
        bad = bhtp.frame(bhtp.RESPONSE, bhtp.encode_response(200, []), version=2)
        self.assertFails(lambda rec, req, n: closing([bad]), "invalid frame")

    def test_known_frame_larger_than_the_maximum(self):
        bad = bhtp.frame(bhtp.DATA, b"", length=bhtp.MAX_PAYLOAD + 1)
        self.assertFails(lambda rec, req, n: closing(response_frames(200, b"a", clen=2)[:1] + [bad]),
                         "invalid frame")

    def test_undefined_flag_bits_from_server(self):
        bad = bhtp.frame(bhtp.RESPONSE, bhtp.encode_response(200, []), flags=0x0100)
        self.assertFails(lambda rec, req, n: closing([bad]), "undefined flags")

    def test_data_before_response(self):
        self.assertFails(lambda rec, req, n: closing([bhtp.frame(bhtp.DATA, b"x", bhtp.END_STREAM)]),
                         "before the RESPONSE")

    def test_two_responses_in_one_exchange(self):
        r = bhtp.frame(bhtp.RESPONSE, bhtp.encode_response(200, [("content-length", "1")]))
        self.assertFails(lambda rec, req, n: closing([r, r]), "second RESPONSE")

    def test_server_sends_a_request_frame(self):
        self.assertFails(lambda rec, req, n: closing([bhtp.frame(bhtp.REQUEST, b"\x01\x00\x01/\x00")]),
                         "REQUEST frame")

    def test_malformed_response_payload(self):
        for payload in (b"", b"\x00", b"\x00\xc8", b"\x00\xc8\x01", b"\x00\x63\x00",
                        b"\x00\xc8\x00\xff", b"\x00\xc8\x01\x0b\x00\x01a",
                        b"\x00\xc8\x01\x03\x00\x09abc"):
            m = self.mock(lambda rec, req, n, p=payload: closing([bhtp.frame(bhtp.RESPONSE, p)]))
            rc, _, err = run([self.url(m)])
            self.assertEqual(rc, 2, "payload %r: %s" % (payload, err))

    def test_body_shorter_than_content_length(self):
        self.assertFails(lambda rec, req, n: closing(response_frames(200, b"abc", clen=10)),
                         "content-length")

    def test_body_longer_than_content_length(self):
        self.assertFails(lambda rec, req, n: closing(response_frames(200, b"abcdef", clen=3)),
                         "content-length")

    def test_content_length_promised_but_response_ends_without_body(self):
        h = [("content-length", "5")]
        self.assertFails(lambda rec, req, n: closing(response_frames(200, b"", headers=h)),
                         "content-length")

    def test_malformed_error_frame(self):
        self.assertFails(lambda rec, req, n: closing([bhtp.frame(bhtp.ERROR, b"\x01")]),
                         "malformed ERROR")

    def test_silent_server_is_not_waited_for_forever(self):
        """A server that accepts the connection and never answers: bcurl must
        give up after its 30 s receive timeout (exit 2, "timed out"). The
        server here is a bare listening socket that is never read from, so
        nothing hangs up before the client's own timeout fires. It takes about
        30 s, so it only runs when BCURL_SLOW_TESTS=1 is set."""
        if not os.environ.get("BCURL_SLOW_TESTS"):
            self.skipTest("set BCURL_SLOW_TESTS=1 to wait for the 30 s client timeout")
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            started = time.time()
            rc, _, err = run(["127.0.0.1:%d/x" % listener.getsockname()[1]], timeout=90)
            waited = time.time() - started
        finally:
            listener.close()
        self.assertEqual(rc, 2, "stderr: %s" % err)
        self.assertIn("timed out", err)
        self.assertGreater(waited, 25, "gave up after only %.1f s" % waited)


# ---------------------------------------------------------------------------
# unknown frame types
# ---------------------------------------------------------------------------

class TestUnknownFrames(MockCase):
    def test_known_unknown_known(self):
        resp, data = response_frames(200, b"after the unknown frame")
        m = self.mock(lambda rec, req, n: [resp, unknown_frame(), data])
        rc, out, err = run([self.url(m)])
        self.assertEqual((rc, out), (0, b"after the unknown frame"), err)

    def test_unknown_frame_before_the_response(self):
        m = self.mock(lambda rec, req, n: [unknown_frame(0x55)] + response_frames(200, b"ok"))
        rc, out, _ = run([self.url(m)])
        self.assertEqual((rc, out), (0, b"ok"))

    def test_unknown_frame_after_the_final_data_frame_stays_in_the_stream(self):
        """It belongs before the NEXT response; the second request must still work."""
        def h(rec, req, n):
            if n == 1:
                return response_frames(200, b"one") + [unknown_frame(0x66)]
            return response_frames(200, b"two")
        m = self.mock(h)
        rc, out, err = run([self.url(m, "/a"), "/b"])
        self.assertEqual((rc, out), (0, b"onetwo"), err)
        self.assertEqual(len(m.records), 1)

    def test_many_unknown_types_and_lengths(self):
        frames = [unknown_frame(t, b"z" * (t % 7)) for t in (0, 5, 6, 0x10, 0x7F, 0x80, 0xFE, 0xFF)]
        frames.append(unknown_frame(0x42, b""))                     # empty payload
        resp, data = response_frames(200, b"payload")
        m = self.mock(lambda rec, req, n: [resp] + frames + [data])
        rc, out, err = run([self.url(m)])
        self.assertEqual((rc, out), (0, b"payload"), err)

    def test_unknown_frame_with_all_flag_bits_set(self):
        resp, data = response_frames(200, b"q")
        m = self.mock(lambda rec, req, n: [unknown_frame(0x70, b"abc", flags=0xFFFF), resp, data])
        rc, out, _ = run([self.url(m)])
        self.assertEqual((rc, out), (0, b"q"))

    def test_huge_unknown_frame_is_skipped(self):
        big = unknown_frame(0x77, b"\xaa" * (3 * 1024 * 1024))
        resp, data = response_frames(200, b"survived")
        m = self.mock(lambda rec, req, n: [big, resp, data])
        rc, out, err = run([self.url(m)])
        self.assertEqual((rc, out), (0, b"survived"), err)

    def test_unknown_frame_larger_than_the_known_maximum_is_still_skipped(self):
        big = unknown_frame(0x78, b"\x00" * (bhtp.MAX_PAYLOAD + 1))
        m = self.mock(lambda rec, req, n: [big] + response_frames(200, b"fine"))
        rc, out, _ = run([self.url(m)])
        self.assertEqual((rc, out), (0, b"fine"))

    def test_unknown_frame_dribbled_one_byte_at_a_time(self):
        m = self.mock(lambda rec, req, n: [unknown_frame(0x71, b"slow" * 5)] + response_frames(200, b"ok"),
                      split="bytes", delay=0.0005)
        rc, out, _ = run([self.url(m)])
        self.assertEqual((rc, out), (0, b"ok"))

    def test_skipped_frame_is_reported_in_verbose_mode(self):
        m = self.mock(lambda rec, req, n: [unknown_frame(0x7F)] + response_frames(200, b"ok"))
        rc, out, err = run(["-v", self.url(m)])
        self.assertEqual((rc, out), (0, b"ok"))
        self.assertIn("UNKNOWN(0x7f)", err)
        self.assertIn("SKIPPED", err)

    def test_unknown_frame_that_never_completes_is_a_failure_not_a_hang(self):
        cut = unknown_frame(0x79, b"k" * 100)[:60]
        m = self.mock(lambda rec, req, n: closing([cut]))
        rc, _, err = run([self.url(m)])
        self.assertEqual(rc, 2)
        self.assertIn("middle of a frame", err)


# ---------------------------------------------------------------------------
# TCP is a byte stream
# ---------------------------------------------------------------------------

class TestTcp(MockCase):
    BODY = b"0123456789" * 30

    def test_everything_in_one_write(self):
        m = self.serve_body(self.BODY)
        rc, out, _ = run([self.url(m)])
        self.assertEqual((rc, out), (0, self.BODY))

    def test_one_byte_at_a_time(self):
        m = self.serve_body(self.BODY, split="bytes", delay=0.0002)
        rc, out, err = run([self.url(m)])
        self.assertEqual((rc, out), (0, self.BODY), err)

    def test_response_split_at_every_offset(self):
        total = len(b"".join(response_frames(200, self.BODY)))
        offsets = list(range(1, 40)) + [total // 2, total - 13, total - 12, total - 1]
        for off in offsets:
            m = self.serve_body(self.BODY, split=off, delay=0.01)
            rc, out, err = run([self.url(m)])
            self.assertEqual((rc, out), (0, self.BODY), "split at %d: %s" % (off, err))
            m.close()

    def test_each_frame_in_its_own_segment(self):
        body = b"x" * 40000                           # 3 DATA frames
        m = self.serve_body(body, split="each")
        rc, out, _ = run([self.url(m)])
        self.assertEqual((rc, out), (0, body))

    def test_many_frames_in_one_read(self):
        body = b"abcdefghij" * 5000
        m = self.mock(lambda rec, req, n: response_frames(200, body, chunk=100))   # 500 frames
        rc, out, err = run([self.url(m)])
        self.assertEqual((rc, out), (0, body), err)

    def test_second_response_arrives_glued_to_the_first_read(self):
        """Two requests; the server answers the first one with an unknown frame
        glued to its tail. All of it arrives in one segment."""
        def h(rec, req, n):
            return response_frames(200, b"R%d" % n) + [unknown_frame()]
        m = self.mock(h)
        rc, out, _ = run([self.url(m, "/a"), "/b", "/c"])
        self.assertEqual((rc, out), (0, b"R1R2R3"))


# ---------------------------------------------------------------------------
# one connection, many requests
# ---------------------------------------------------------------------------

class TestOneConnection(MockCase):
    def echo(self, rec, req, n):
        return response_frames(200, b"[" + req.path + b"]")

    def test_three_requests_one_connection(self):
        m = self.mock(self.echo)
        rc, out, err = run([self.url(m, "/one"), "/two", "/three"])
        self.assertEqual((rc, out), (0, b"[/one][/two][/three]"), err)
        self.assertEqual(len(m.records), 1, "bcurl opened more than one TCP connection")
        self.assertEqual([r.path for r in m.records[0].requests], [b"/one", b"/two", b"/three"])

    def test_full_urls_for_the_same_server_share_the_connection(self):
        m = self.mock(self.echo)
        rc, out, _ = run([self.url(m, "/a"), self.url(m, "/b")])
        self.assertEqual((rc, out), (0, b"[/a][/b]"))
        self.assertEqual(len(m.records), 1)

    def test_a_different_server_is_refused_before_connecting(self):
        m = self.mock(self.echo)
        rc, _, err = run([self.url(m, "/a"), "127.0.0.1:%d/b" % (m.port + 1)])
        self.assertEqual(rc, 64)
        self.assertIn("single connection", err)
        self.assertEqual(len(m.records), 0, "no connection should have been made")

    def test_requests_are_sequential_one_response_before_the_next_request(self):
        seen = []

        def h(rec, req, n):
            seen.append((n, len(rec.requests)))
            return response_frames(200, b"x")
        m = self.mock(h)
        run([self.url(m, "/1"), "/2", "/3", "/4"])
        self.assertEqual(seen, [(1, 1), (2, 2), (3, 3), (4, 4)])

    def test_a_404_in_the_middle_does_not_stop_the_rest(self):
        def h(rec, req, n):
            return response_frames(404, b"") if req.path == b"/gone" else response_frames(200, b"ok:" + req.path)
        m = self.mock(h)
        rc, out, err = run([self.url(m, "/a"), "/gone", "/b"])
        self.assertEqual(rc, 1)
        self.assertEqual(out, b"ok:/aok:/b")
        self.assertEqual(len(m.records), 1)
        self.assertIn("/gone -> 404", err)

    def test_failure_in_the_middle_stops_without_reconnecting(self):
        def h(rec, req, n):
            return CLOSE if n == 2 else response_frames(200, b"fine")
        m = self.mock(h)
        rc, out, err = run([self.url(m, "/a"), "/b", "/c"])
        self.assertEqual(rc, 2)
        self.assertEqual(out, b"fine")
        self.assertEqual(len(m.records), 1, "bcurl must not reconnect after a failure")
        self.assertEqual(len(m.records[0].requests), 2)

    def test_server_error_frame_ends_the_session(self):
        err_frame = bhtp.frame(bhtp.ERROR, struct.pack(">H", 400) + b"frame too large")
        m = self.mock(lambda rec, req, n: closing([err_frame]))
        rc, _, _ = run([self.url(m, "/a"), "/b"])
        self.assertEqual(rc, 1)
        self.assertEqual(len(m.records), 1)
        self.assertEqual(len(m.records[0].requests), 1)

    def test_verbose_summary_states_the_connection_count(self):
        m = self.mock(self.echo)
        _, _, err = run(["-v", self.url(m, "/a"), "/b", "/c"])
        self.assertIn("3 request(s) completed over 1 TCP connection(s)", err)
        self.assertEqual(len(m.records), 1)

    def test_fifty_requests_on_one_connection(self):
        m = self.mock(self.echo)
        paths = ["/p%d" % i for i in range(50)]
        rc, out, _ = run([self.url(m, paths[0])] + paths[1:])
        self.assertEqual(rc, 0)
        self.assertEqual(out, b"".join(b"[" + p.encode() + b"]" for p in paths))
        self.assertEqual(len(m.records), 1)


# ---------------------------------------------------------------------------
# -v / -vv
# ---------------------------------------------------------------------------

class TestVerbose(MockCase):
    def test_stdout_carries_only_the_body(self):
        m = self.serve_body(b"BODY")
        rc, out, err = run(["-v", self.url(m)])
        self.assertEqual((rc, out), (0, b"BODY"))
        self.assertIn("TX FRAME", err)
        self.assertIn("RX FRAME", err)

    def test_tx_and_rx_are_labelled_and_numbered(self):
        m = self.serve_body(b"BODY")
        _, _, err = run(["-v", self.url(m)])
        frames = parse_dump(err)
        self.assertEqual([(d, s) for d, s, _ in frames], [("TX", 1), ("RX", 1), ("RX", 2)])
        self.assertIn("TX FRAME #1  REQUEST  flags=0x0000", err)
        self.assertIn("RX FRAME #1  RESPONSE  flags=0x0000", err)
        self.assertIn("RX FRAME #2  DATA  flags=0x0001", err)

    def test_dumped_bytes_are_exactly_the_bytes_on_the_wire(self):
        body = bytes(range(256)) * 3
        m = self.mock(lambda rec, req, n: response_frames(200, body, chunk=200))
        rc, out, err = run(["-vv", self.url(m, "/x")])
        self.assertEqual((rc, out), (0, body))
        frames = parse_dump(err)
        rec = m.records[0]
        self.assertEqual(b"".join(b for d, _, b in frames if d == "TX"), rec.received)
        self.assertEqual(b"".join(b for d, _, b in frames if d == "RX"), rec.sent)

    def test_every_frame_of_a_multi_request_session_is_dumped(self):
        m = self.mock(lambda rec, req, n: response_frames(200, b"r%d" % n))
        _, _, err = run(["-vv", self.url(m, "/a"), "/b", "/c"])
        frames = parse_dump(err)
        self.assertEqual([d for d, _, _ in frames], ["TX", "RX", "RX"] * 3)
        self.assertEqual([s for d, s, _ in frames if d == "TX"], [1, 2, 3])
        self.assertEqual([s for d, s, _ in frames if d == "RX"], [1, 2, 3, 4, 5, 6])

    def test_default_dump_is_cut_but_vv_is_complete(self):
        body = b"A" * 16000
        m = self.serve_body(body)
        _, _, err1 = run(["-v", self.url(m)])
        self.assertIn("more bytes not shown", err1)
        _, _, err2 = run(["-vv", self.url(m)])
        self.assertNotIn("not shown", err2)
        self.assertEqual(sum(len(b) for d, _, b in parse_dump(err2) if d == "RX"),
                         len(m.records[1].sent))

    def test_response_summary_lists_decoded_headers(self):
        m = self.mock(lambda rec, req, n: response_frames(200, b"hi", headers=[
            ("content-type", "text/plain"), ("content-length", "2"), ("x-custom", "yes")]))
        _, _, err = run(["-v", self.url(m)])
        self.assertIn("< status 200 OK", err)
        self.assertIn("< content-type: text/plain", err)
        self.assertIn("static name, 1 byte", err)
        self.assertIn("< x-custom: yes", err)
        self.assertIn("literal name", err)

    def test_404_is_dumped_too(self):
        m = self.mock(lambda rec, req, n: response_frames(404, b""))
        rc, _, err = run(["-v", self.url(m, "/nope")])
        self.assertEqual(rc, 1)
        self.assertIn("< status 404 Not Found", err)
        self.assertEqual([d for d, _, _ in parse_dump(err)], ["TX", "RX"])

    def test_quiet_by_default(self):
        m = self.serve_body(b"x")
        _, _, err = run([self.url(m)])
        self.assertEqual(err, "")

    def test_invalid_frame_from_server_is_dumped_before_failing(self):
        m = self.mock(lambda rec, req, n: closing([b"HTTP/1.1 200 OK\r\n\r\n"]))
        rc, _, err = run(["-v", self.url(m)])
        self.assertEqual(rc, 2)
        self.assertIn("INVALID", err)


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------

class TestCommandLine(MockCase):
    def test_no_arguments_prints_usage(self):
        rc, _, err = run([])
        self.assertEqual(rc, 64)
        self.assertIn("usage:", err)

    def test_help_exits_zero(self):
        rc, _, err = run(["-h"])
        self.assertEqual(rc, 0)
        self.assertIn("usage:", err)

    def test_bad_targets_are_rejected_without_connecting(self):
        for bad in ["localhost", "localhost/x", ":9000/x", "localhost:0/x", "localhost:70000/x",
                    "localhost:abc/x", "[::1/x"]:
            rc, _, err = run([bad])
            self.assertEqual(rc, 64, "%s -> %s" % (bad, err))
            self.assertIn("bad target", err)

    def test_bad_header_option_is_rejected_before_connecting(self):
        for bad in ["nocolon", ":novalue-name", "bad name: x", "UPPER_CASE: x", "x\x01y: v"]:
            m = self.serve_body(b"x")
            rc, _, err = run(["-H", bad, self.url(m)])
            self.assertEqual(rc, 64, "%r: %s" % (bad, err))
            self.assertIn("invalid header", err)
            self.assertEqual(len(m.records), 0, "no connection may be made for %r" % bad)

    def test_unknown_option(self):
        rc, _, _ = run(["-z", "localhost:9000/x"])
        self.assertEqual(rc, 64)

    def test_path_with_control_character_is_rejected(self):
        rc, _, err = run(["localhost:9000/a\tb"])
        self.assertEqual(rc, 64)

    def test_stdout_closed_early_is_not_a_crash(self):
        body = b"z" * 200000
        m = self.mock(lambda rec, req, n: response_frames(200, body))
        p = subprocess.Popen([BCURL, self.url(m)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p.stdout.read(10)
        p.stdout.close()
        p.wait(timeout=20)
        self.assertIn(p.returncode, (0, 2))                # clean exit, no signal
        p.stderr.close()


# ---------------------------------------------------------------------------
# interoperability with an independent reference implementation of the server
# ---------------------------------------------------------------------------

class TestReferenceServer(unittest.TestCase):
    """Runs tools/refserver.py (a complete spec-only server in Python) and talks
    to it with bcurl: a second, independent check that bcurl follows the spec."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        for name in ("index.html", "hello.txt"):
            with open(os.path.join(FIXTURES, name), "rb") as s, open(os.path.join(cls.tmp, name), "wb") as d:
                d.write(s.read())
        open(os.path.join(cls.tmp, "empty.txt"), "wb").close()
        cls.binary = os.urandom(300000) + bytes(range(256))
        with open(os.path.join(cls.tmp, "random.bin"), "wb") as f:
            f.write(cls.binary)
        cls.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "tools", "refserver.py"), cls.tmp, "0"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        line = cls.proc.stderr.readline()
        m = re.search(r"port (\d+)", line)
        if not m:
            raise RuntimeError("refserver did not start: %r" % line)
        cls.port = int(m.group(1))

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=5)
        cls.proc.stderr.close()
        cls.proc.stdout.close()

    def get(self, *paths, args=()):
        t = "localhost:%d%s" % (self.port, paths[0])
        return run(list(args) + [t] + list(paths[1:]))

    def test_html(self):
        rc, out, err = self.get("/index.html")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, read_file(os.path.join(self.tmp, "index.html")))

    def test_empty_binary_and_404(self):
        self.assertEqual(self.get("/empty.txt")[:2], (0, b""))
        rc, out, _ = self.get("/random.bin")
        self.assertEqual((rc, out), (0, self.binary))
        self.assertEqual(self.get("/missing")[0], 1)
        self.assertEqual(self.get("/../../etc/passwd")[0], 1)

    def test_three_requests_one_connection(self):
        rc, out, err = self.get("/hello.txt", "/index.html", "/empty.txt", args=["-v"])
        self.assertEqual(rc, 0, err)
        self.assertIn("3 request(s) completed over 1 TCP connection(s)", err)


if __name__ == "__main__":
    if not os.path.exists(BCURL):
        sys.exit("bcurl not found at %s - run 'make' first" % BCURL)
    unittest.main(verbosity=1)
