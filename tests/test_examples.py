#!/usr/bin/env python3
"""
test_examples.py - the documented hexdumps must be what bcurl really sends
and understands.

examples/request.hexdump and examples/response.hexdump are checked like this:

  * the request in the file equals the frame SPEC.md says bcurl builds
    (built here by the independent tests/bhtp.py, not by the C code)
  * a one-shot server replays the response bytes from the file into a real
    bcurl process; bcurl must print them in its -vv dump unchanged and write
    exactly the file body to stdout
  * the annotation tables in the files are exactly what the spec-only decoder
    (tools/make_examples.py) produces from the same bytes

    python3 tests/test_examples.py
"""
import os
import re
import socket
import subprocess
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "tools"))
import bhtp  # noqa: E402
import make_examples  # noqa: E402
from test_client import parse_dump, run  # noqa: E402

EXAMPLES = os.path.join(REPO, "examples")

# ---- parsing of the checked-in example files -------------------------------

def read_example_frames(path):
    """[(label, bytes, annotation-table-lines)] from examples/*.hexdump."""
    frames, cur, in_table = [], None, False
    with open(path) as f:
        for line in f.read().splitlines():
            m = re.match(r"^([TR]X) FRAME #(\d+)\s+(\w+)", line)
            if m:
                cur = {"label": m.group(0), "bytes": b"", "table": []}
                frames.append(cur)
                in_table = False
                continue
            if cur is None:
                continue
            m = re.match(r"^\s*[0-9a-f]{4}: ((?:[0-9a-f]{2} ?| )+?) +\|", line)
            if m and not in_table:
                cur["bytes"] += bytes.fromhex(m.group(1).replace(" ", ""))
                continue
            if line.strip().startswith("Offset"):
                in_table = True
            if in_table:
                cur["table"].append(line.strip())
    for fr in frames:                      # blank lines after a table are just spacing
        while fr["table"] and fr["table"][-1] == "":
            fr["table"].pop()
    return frames


def table_lines(rows):
    return [l.strip() for l in make_examples.render_table(rows)]


class TestExamples(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.req = read_example_frames(os.path.join(EXAMPLES, "request.hexdump"))
        cls.resp = read_example_frames(os.path.join(EXAMPLES, "response.hexdump"))

    def test_example_files_contain_one_request_and_a_response_with_data(self):
        self.assertEqual(len(self.req), 1)
        self.assertEqual([f["label"].split()[-1] for f in self.resp], ["RESPONSE", "DATA"])

    def test_example_request_is_the_frame_the_spec_says_bcurl_builds(self):
        want = bhtp.request_frame("/index.html", [
            ("host", "localhost:9000"), ("user-agent", "bcurl/1.0"), ("accept", "*/*"),
            ("accept-encoding", "identity"), ("connection", "keep-alive")])
        self.assertEqual(self.req[0]["bytes"], want)

    def test_bcurl_sends_that_request_and_understands_that_response(self):
        response = b"".join(f["bytes"] for f in self.resp)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        seen = {}

        def serve():
            conn, _ = listener.accept()
            data = b""
            while len(data) < 12 or len(data) < 12 + int.from_bytes(data[8:12], "big"):
                data += conn.recv(4096)
            seen["request"] = data
            conn.sendall(response)
            conn.recv(1)                     # wait for the client to hang up
            conn.close()
        t = threading.Thread(target=serve, daemon=True)
        t.start()

        rc, out, err = run(["-vv", "127.0.0.1:%d/index.html" % port])
        t.join(5)
        listener.close()
        self.assertEqual(rc, 0, err)

        # what bcurl sent: the example request, with only the port in the host header changed
        want = bhtp.request_frame("/index.html", [
            ("host", "127.0.0.1:%d" % port), ("user-agent", "bcurl/1.0"), ("accept", "*/*"),
            ("accept-encoding", "identity"), ("connection", "keep-alive")])
        self.assertEqual(seen["request"], want)
        # what bcurl dumped: the example response, frame by frame, unchanged
        rx = [b for d, _, b in parse_dump(err) if d == "RX"]
        self.assertEqual(rx, [f["bytes"] for f in self.resp])
        # what bcurl wrote to stdout: exactly the DATA bytes
        info, _ = make_examples.decode_frame(self.resp[1]["bytes"])
        self.assertEqual(out, info["body"])

    def test_annotation_tables_match_what_the_bytes_decode_to(self):
        for f in self.req + self.resp:
            info, rows = make_examples.decode_frame(f["bytes"])
            self.assertEqual(f["table"], table_lines(rows), f["label"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
