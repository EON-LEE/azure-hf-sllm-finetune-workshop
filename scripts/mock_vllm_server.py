"""Mock vLLM OpenAI server: streams SSE with controllable TTFT and inter-token delay.

Used to exercise ffsft.serve.loadtest end-to-end without a GPU, so that a live
run against a real endpoint is testing the endpoint rather than the client.

It also reproduces the one behaviour that cost a whole bench run (VERIFIED 55):
the server this stands in for runs with `--reasoning-parser qwen3`, so a Qwen3
<think> block leaves as `delta.reasoning_content`, not `delta.content`. Thinking
is ON unless the request sends `chat_template_kwargs: {"enable_thinking": false}`
-- the same default the real server has, and the reason a client that sends
nothing measures a different mode than training used. A mock that only ever
emitted `content` could not catch that, which is precisely why it did not.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


TTFT_S = _env_float("MOCK_TTFT_S", 0.20)
ITL_S = _env_float("MOCK_ITL_S", 0.010)  # inter-token latency
N_TOKENS = 32
#: How many of the streamed tokens land in `reasoning_content` before the answer
#: starts. Deliberately larger than a short `max_tokens`: a request that is cut
#: off mid-thought is the case that scored as "no tokens streamed" for a client
#: counting only `content`, and it has to be reachable here to be demonstrable.
THINK_TOKENS = int(os.environ.get("MOCK_THINK_TOKENS", "12"))
#: Which field name the thinking block leaves under. The image behind
#: `ffsft-plc/green` streams `reasoning`: 4920 of 4921 SSE frames carried
#: `delta.reasoning` and none carried `delta.reasoning_content` (VERIFIED 68).
#: This mock emitted only the older spelling, so a client that handled just that
#: one looked correct here and counted zero against the deployment. Default to
#: what the deployment actually sends; the old name stays reachable so both
#: wires can be exercised.
THINK_FIELD = os.environ.get("MOCK_THINK_FIELD", "reasoning")


def wants_thinking(body: dict) -> bool:
    """Qwen3 thinks unless the caller explicitly turns it off.

    Absent and `{}` both mean "server default", which is thinking on -- they are
    not the same as `{"enable_thinking": false}`, and collapsing the three is
    what `bench_job.bench_env` is careful to avoid.
    """
    ctk = body.get("chat_template_kwargs")
    if isinstance(ctk, dict) and "enable_thinking" in ctk:
        return bool(ctk["enable_thinking"])
    return True


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

        # vLLM answers both wires, and so must this. The load generator streams
        # because TTFT is only observable on a stream; the smoke call in
        # docker/bench_entrypoint.sh does not, because a single well-formed JSON
        # reply is what proves the model decodes. A mock that only streamed made
        # the non-streaming half untestable without a GPU, which is the half
        # that runs first on the node.
        thinking = wants_thinking(body)
        n_think = min(THINK_TOKENS, max_tokens) if thinking else 0

        if not body.get("stream"):
            time.sleep(TTFT_S + ITL_S * max(0, max_tokens - 1))
            message = {
                "role": "assistant",
                "content": " ".join(f"t{i}" for i in range(n_think, max_tokens)),
            }
            if n_think:
                # vLLM puts the parsed block in its own field and leaves it out
                # of `content`. The smoke test in bench_entrypoint.sh reads
                # `content`, so a reply that is all thinking looks empty there
                # too -- same failure, different wire.
                message[THINK_FIELD] = " ".join(f"r{i}" for i in range(n_think))
            payload = json.dumps(
                {
                    "id": "x",
                    "object": "chat.completion",
                    "model": body.get("model", "mock"),
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": "length",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": max_tokens,
                        "total_tokens": 12 + max_tokens,
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
            return

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
            # One field per chunk, never both -- that is what the real parser
            # does, and it is why a client that reads only one of them can see a
            # fully-decoded response as silence.
            delta = {THINK_FIELD: f"r{i} "} if i < n_think else {"content": f"t{i} "}
            frame(
                {
                    "id": "x",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": delta}],
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
    print(
        f"mock vllm on http://127.0.0.1:{port}/v1  ttft={TTFT_S}s itl={ITL_S}s "
        f"think_tokens={THINK_TOKENS}",
        flush=True,
    )
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
