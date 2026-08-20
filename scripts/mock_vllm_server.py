"""Mock vLLM OpenAI server: streams SSE with controllable TTFT and inter-token delay.

Used to exercise ffsft.serve.loadtest end-to-end without a GPU, so that a live
run against a real endpoint is testing the endpoint rather than the client.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TTFT_S = 0.20
ITL_S = 0.010  # inter-token latency
N_TOKENS = 32


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # keep the output readable
        pass

    def do_GET(self):
        if self.path.endswith("/health"):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self.send_error(404)

    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        max_tokens = int(body.get("max_tokens") or N_TOKENS)
        want_usage = bool((body.get("stream_options") or {}).get("include_usage"))

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def frame(obj):
            data = f"data: {json.dumps(obj)}\n\n".encode()
            self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()

        time.sleep(TTFT_S)
        n = max_tokens
        for i in range(n):
            if i:
                time.sleep(ITL_S)
            frame(
                {
                    "id": "x",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {"content": f"t{i} "}}],
                }
            )
        if want_usage:
            frame(
                {
                    "id": "x",
                    "object": "chat.completion.chunk",
                    "choices": [],
                    "usage": {"prompt_tokens": 12, "completion_tokens": n, "total_tokens": 12 + n},
                }
            )
        done = b"data: [DONE]\n\n"
        self.wfile.write(f"{len(done):X}\r\n".encode() + done + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8111
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"mock vllm on http://127.0.0.1:{port}/v1  ttft={TTFT_S}s itl={ITL_S}s", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
