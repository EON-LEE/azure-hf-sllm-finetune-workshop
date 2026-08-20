"""End-to-end check that the load test measures what it claims to measure.

Every other test in this suite exercises the summarising arithmetic on
synthetic RequestResult objects. That leaves the part most likely to be wrong
untested: whether TTFT and TPOT taken from a real SSE stream over a real socket
actually correspond to the server's first-token delay and inter-token delay.

The mock server here streams with a known TTFT and a known inter-token latency,
so the measured numbers can be compared against ground truth. Tolerances are
loose because this runs on a shared CI-style machine, but they are tight enough
to catch the failure modes that matter: measuring TTFT from the wrong instant,
counting SSE frames instead of tokens, or dividing by the wrong denominator.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ffsft.serve.loadtest import sweep

TTFT_S = 0.20
ITL_S = 0.01
TOKENS = 20


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        n = int(body.get("max_tokens") or TOKENS)
        want_usage = bool((body.get("stream_options") or {}).get("include_usage"))

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def frame(obj):
            data = f"data: {json.dumps(obj)}\n\n".encode()
            self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()

        time.sleep(TTFT_S)
        for i in range(n):
            if i:
                time.sleep(ITL_S)
            frame({"choices": [{"index": 0, "delta": {"content": f"t{i} "}}]})
        if want_usage:
            frame({"choices": [], "usage": {"completion_tokens": n}})
        done = b"data: [DONE]\n\n"
        self.wfile.write(f"{len(done):X}\r\n".encode() + done + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


@pytest.fixture(scope="module")
def mock_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


@pytest.fixture(scope="module")
def results(mock_server):
    return asyncio.run(sweep(mock_server, "ffsft", [1, 4], 4, max_tokens=TOKENS, timeout=30.0))


def test_every_request_succeeds(results):
    for level in results:
        assert level.failed == 0, level.errors
        assert level.succeeded == 4


def test_measured_ttft_matches_the_servers_first_token_delay(results):
    """Catches TTFT being timed from the wrong instant."""
    for level in results:
        assert TTFT_S - 0.05 <= level.ttft_p50 <= TTFT_S + 0.25, level.ttft_p50


def test_measured_tpot_matches_the_servers_inter_token_delay(results):
    """Catches dividing total time by the wrong token count."""
    for level in results:
        assert ITL_S - 0.005 <= level.tpot_p50 <= ITL_S + 0.02, level.tpot_p50


def test_token_count_comes_from_usage_not_frame_count(results):
    for level in results:
        assert level.output_tokens == TOKENS * level.succeeded


def test_throughput_rises_with_concurrency(results):
    by_conc = {r.concurrency: r for r in results}
    assert by_conc[4].output_tok_per_s > by_conc[1].output_tok_per_s
