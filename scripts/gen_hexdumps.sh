#!/usr/bin/env bash
# Regenerate examples/request.hexdump and examples/response.hexdump from REAL
# traffic: it runs a server, runs `bcurl -vv`, and annotates the frames that
# bcurl dumped. tools/make_examples.py refuses to write anything if the frames
# do not match the spec or the file that was served.
#
#   scripts/gen_hexdumps.sh                     # server = tools/refserver.py
#   scripts/gen_hexdumps.sh /path/to/bserve     # server = any BHTP/1 server binary
#
# The checked-in examples were captured against Nistha's `bserve`. With the
# reference server the only difference is the value of the `server` header.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-9000}"
make -s bcurl

TMP="$(mktemp -d)"
SRV=""
cleanup() { [ -n "$SRV" ] && kill "$SRV" 2>/dev/null || true; rm -rf "$TMP"; }
trap cleanup EXIT

mkdir "$TMP/www"
cp fixtures/www/index.html "$TMP/www/index.html"
touch -d '2026-01-01 00:00:00 UTC' "$TMP/www/index.html"   # fixed last-modified => reproducible bytes

if [ -n "${1:-}" ]; then
    "$1" "$TMP/www" "$PORT" 2>"$TMP/server.log" &
else
    python3 tools/refserver.py "$TMP/www" "$PORT" 2>"$TMP/server.log" &
fi
SRV=$!

for _ in $(seq 1 50); do
    grep -q "serving" "$TMP/server.log" 2>/dev/null && break
    sleep 0.1
done
grep -q "serving" "$TMP/server.log" || { echo "server did not start:"; cat "$TMP/server.log"; exit 1; }

./bcurl -vv "localhost:$PORT/index.html" >"$TMP/body" 2>"$TMP/stderr.txt"
cmp "$TMP/body" "$TMP/www/index.html"     # the body bcurl wrote is the served file

python3 tools/make_examples.py \
    --from-bcurl "$TMP/stderr.txt" \
    --served-file "$TMP/www/index.html" \
    --path /index.html \
    --command "./bcurl -vv localhost:$PORT/index.html" \
    --out examples
