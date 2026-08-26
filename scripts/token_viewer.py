"""Watch tokens arrive, one chunk at a time, in a browser.

Why this exists
---------------
`ffsft-loadtest` reduces a stream to numbers -- TTFT, TPOT, percentiles. That is
the right shape for a sweep and the wrong shape for the question "what is
actually coming down the wire". JOURNAL 55 is what happens when nobody looks:
the server routed a Qwen3 <think> block into `delta.reasoning` (older vLLM:
`delta.reasoning_content`), the
client counted only `delta.content`, and 40 of every 64 fully-decoded responses
were scored as "no tokens streamed" -- at every concurrency level, for a whole
bench run, without a single error in the log.

So this page shows the wire itself: every SSE delta as its own chip, coloured by
which field carried it, with the gap since the previous one. Flip the thinking
toggle and the chips change colour -- that is the entire bug, visible in one
click, and the "old meter" counter next to it goes to zero while tokens are
still plainly arriving.

Running it
----------
    uv run python scripts/token_viewer.py

With no --upstream it starts scripts/mock_vllm_server.py itself, so there is
nothing to install and no GPU involved. Point it at something real with:

    uv run python scripts/token_viewer.py --upstream https://host/v1 --api-key ...

The page and the proxy share an origin, which is the only reason the browser
will talk to an upstream that sends no CORS headers.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
PAGE = HERE / "token_viewer.html"

#: Filled in by main() before the server starts.
UPSTREAM = "http://127.0.0.1:8111/v1"
API_KEY: str | None = None
# The bundled mock serves "local"; a real deployment serves whatever
# SERVED_MODEL_NAME was set to (ours is "ffsft"). Hardcoding either one in
# the page makes the other 404, so it is resolved from the upstream.
MODEL = "local"


def _spawn_mock(port: int) -> subprocess.Popen[bytes]:
    """Start the mock server as a child so one command is enough to see anything.

    Its stdout is inherited rather than piped: if it fails to bind, the reason
    should land in the same terminal as everything else instead of in a buffer
    nobody reads.
    """
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, str(HERE / "mock_vllm_server.py"), str(port)],
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    # The mock binds before it prints, but the browser can beat both. A short
    # readiness poll is cheaper than explaining a first-request failure.
    deadline = time.time() + 10
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"mock server exited immediately (code {proc.returncode})")
        try:
            with urllib.request.urlopen(  # noqa: S310
                f"http://127.0.0.1:{port}/v1/health", timeout=0.5
            ):
                return proc
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    raise SystemExit("mock server did not become healthy within 10s")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # the interesting log is the page itself
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/upstream":
            body = json.dumps({"upstream": UPSTREAM, "model": MODEL}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        # Two upstream paths, because they answer different questions.
        #
        #   /chat/completions -- what a client actually gets. The server applies
        #       the chat template and, on `green`, the qwen3 reasoning parser.
        #   /completions      -- what the model actually emitted. No template and
        #       no parser, so `<think>`/`</think>` survive verbatim. This is the
        #       only path on which the reasoning tokens are visible at all: the
        #       parser buffers them until `</think>` and drops the buffer if the
        #       tag never arrives, which is measured to be the common case here
        #       (700 tokens billed, both fields empty -- JOURNAL S68).
        if self.path.endswith("/chat/completions"):
            route = "/chat/completions"
        elif self.path.endswith("/completions"):
            route = "/completions"
        else:
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        payload = self.rfile.read(length) or b"{}"

        req = urllib.request.Request(  # noqa: S310
            f"{UPSTREAM.rstrip('/')}{route}",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if API_KEY:
            req.add_header("Authorization", f"Bearer {API_KEY}")
        # Addresses one deployment directly, independent of the traffic split,
        # so blue and green can be compared without shifting traffic.
        dep = self.headers.get("X-Deployment")
        if dep:
            req.add_header("azureml-model-deployment", dep)

        try:
            upstream = urllib.request.urlopen(req, timeout=600)  # noqa: S310
        except urllib.error.HTTPError as e:
            detail = e.read()[:2000].decode("utf-8", "replace")
            self._fail(e.code, f"upstream {e.code}: {detail}")
            return
        except (urllib.error.URLError, OSError) as e:
            self._fail(502, f"cannot reach {UPSTREAM}: {e}")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            # Line at a time, flushed immediately. Buffering here would smear the
            # inter-token gaps the page exists to display -- the numbers would
            # then describe this proxy rather than the server.
            for line in upstream:
                self._chunk(line)
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            upstream.close()
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def _fail(self, code: int, message: str) -> None:
        body = json.dumps({"error": message}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def resolve_model(upstream: str, api_key: str | None, fallback: str = "local") -> str:
    """Ask the upstream what it serves, rather than guessing.

    A wrong name fails as "upstream 424 ... The model 'local' does not exist",
    which reads like a proxy problem and is really a one-word mismatch.
    """
    req = urllib.request.Request(f"{upstream.rstrip('/')}/models")  # noqa: S310
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
            data = json.load(r).get("data") or []
        return data[0].get("id") or fallback
    except (urllib.error.URLError, OSError, ValueError, IndexError, KeyError):
        # Not fatal: the mock may not be up yet, and --model still overrides.
        return fallback


def main(argv: list[str] | None = None) -> int:
    global UPSTREAM, API_KEY, MODEL
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8112)
    ap.add_argument(
        "--upstream",
        default=None,
        help="OpenAI-compatible base URL ending in /v1. Omit to start the "
        "bundled mock server, which needs no GPU.",
    )
    ap.add_argument("--mock-port", type=int, default=8111)
    ap.add_argument(
        "--model",
        default=None,
        help="Served model name. Omit to ask the upstream's /models.",
    )
    ap.add_argument(
        "--api-key",
        default=os.environ.get("FFSFT_ENDPOINT_KEY"),
        help="Bearer token for --upstream. Defaults to $FFSFT_ENDPOINT_KEY.",
    )
    args = ap.parse_args(argv)

    mock = None
    if args.upstream:
        UPSTREAM = args.upstream
    else:
        mock = _spawn_mock(args.mock_port)
        UPSTREAM = f"http://127.0.0.1:{args.mock_port}/v1"
    API_KEY = args.api_key
    MODEL = args.model or resolve_model(UPSTREAM, API_KEY)

    # Without this, a SIGTERM skips the finally below and leaves the mock
    # server bound to its port -- the next run then fails to start for a
    # reason that has nothing to do with the change being tested.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"token viewer  http://127.0.0.1:{args.port}", flush=True)
    print(f"upstream      {UPSTREAM}", flush=True)
    print(f"model         {MODEL}", flush=True)
    print("ctrl-c to stop", flush=True)
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        srv.shutdown()
        if mock is not None:
            mock.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
