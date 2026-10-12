# BHTP/1 client (bcurl) -- build, test and verification targets.
#
#   make              build ./bcurl
#   make test         unit tests + integration tests (needs gcc and python3)
#   make test-unit    C unit tests only (needs only a C compiler)
#   make asan         everything again under AddressSanitizer/UBSan
#   make clean        remove everything that was built

CC      ?= cc
CFLAGS  ?= -O2 -g
override CFLAGS += -std=c11 -D_XOPEN_SOURCE=700 -Wall -Wextra -Wpedantic -Iinclude
SAN     = -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer

PROTO_SRC  = src/protocol.c src/frame.c src/io.c
CLIENT_SRC = src/bcurl.c src/client.c src/connect.c src/url.c src/hexdump.c $(PROTO_SRC)
HEADERS    = $(wildcard include/*.h)

all: bcurl

bcurl: $(CLIENT_SRC) $(HEADERS)
	$(CC) $(CFLAGS) -o $@ $(CLIENT_SRC)

build:
	mkdir -p build

build/test_protocol: tests/test_protocol.c src/protocol.c $(HEADERS) | build
	$(CC) $(CFLAGS) -o $@ tests/test_protocol.c src/protocol.c

build/test_frame: tests/test_frame.c $(PROTO_SRC) $(HEADERS) | build
	$(CC) $(CFLAGS) -o $@ tests/test_frame.c $(PROTO_SRC)

build/test_shortio: tests/test_shortio.c $(PROTO_SRC) $(HEADERS) | build
	$(CC) $(CFLAGS) -o $@ tests/test_shortio.c $(PROTO_SRC) -Wl,--wrap=read -Wl,--wrap=write

build/test_url: tests/test_url.c src/url.c src/hexdump.c src/protocol.c $(HEADERS) | build
	$(CC) $(CFLAGS) -o $@ tests/test_url.c src/url.c src/hexdump.c src/protocol.c

test-unit: build/test_protocol build/test_frame build/test_shortio build/test_url
	./build/test_protocol
	./build/test_frame
	./build/test_shortio
	./build/test_url

test-integration: bcurl
	python3 tests/test_client.py
	python3 tests/test_examples.py

test: test-unit test-integration
	@echo
	@echo "==== ALL TESTS PASSED ===="

asan: | build
	$(CC) $(CFLAGS) $(SAN) -o build/test_protocol_asan tests/test_protocol.c src/protocol.c
	$(CC) $(CFLAGS) $(SAN) -o build/test_frame_asan tests/test_frame.c $(PROTO_SRC)
	$(CC) $(CFLAGS) $(SAN) -o build/test_shortio_asan tests/test_shortio.c $(PROTO_SRC) -Wl,--wrap=read -Wl,--wrap=write
	$(CC) $(CFLAGS) $(SAN) -o build/test_url_asan tests/test_url.c src/url.c src/hexdump.c src/protocol.c
	$(CC) $(CFLAGS) $(SAN) -o build/bcurl_asan $(CLIENT_SRC)
	./build/test_protocol_asan
	./build/test_frame_asan
	./build/test_shortio_asan
	./build/test_url_asan
	BCURL=./build/bcurl_asan python3 tests/test_client.py
	BCURL=./build/bcurl_asan python3 tests/test_examples.py
	@echo
	@echo "==== ALL TESTS PASSED UNDER ASAN/UBSAN ===="

clean:
	rm -rf build bcurl

.PHONY: all test test-unit test-integration asan clean
