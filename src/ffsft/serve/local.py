"""An OpenAI-compatible server that runs the model in this process, on CPU.

Why this exists
---------------
Every hosted serving pattern in this repo is unreachable on the subscription it
was built against, and for two independent reasons that no amount of code can
route around:

* dedicated GPU quota is 0, so managed online endpoints cannot be created (§22);
* the workspace datastore has `publicNetworkAccess=Disabled` and no private
  endpoint, so no model asset can be registered, which blocks batch and AKS
  deployments too (§24).

That leaves the `local_vllm` pattern, and vLLM needs a GPU. This module is its
CPU sibling: same wire protocol, same load-test client, no accelerator and no
Azure. It exists so the serving and load-testing half of this asset can be
*run* rather than described -- on a laptop, in CI, or on any box with 4 GB free.

What it is not
--------------
Not a production server. One model, one process, no batching, no paged
attention, no streaming, greedy decoding on CPU. Throughput will be on the
order of single-digit tokens per second. The point is a correct endpoint, not a
fast one; correctness is what the load-test harness needs in order to prove
*itself*.
"""

from __future__ import annotations

import argparse
import logging
import time
import uuid

from pydantic import BaseModel, Field

log = logging.getLogger("ffsft.serve.local")

#: Kept small on purpose. CPU decoding is slow enough that a large default
#: turns a smoke test into a coffee break, and the load test overrides it.
DEFAULT_MAX_TOKENS = 64


class ChatRequest(BaseModel):
    """The subset of the chat-completions request this server honours.

    Deliberately permissive about unknown fields: real OpenAI clients send
    things like `top_p`, `n` and `user`, and rejecting them would fail requests
    that this server could perfectly well answer.
    """

    model: str
    messages: list[dict]
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = 0.0
    stream: bool = False
    stop: list[str] | None = Field(default=None)

    model_config = {"extra": "allow"}


def render_prompt(messages: list[dict]) -> str:
    """Flatten a chat conversation into a plain prompt.

    Only used when the tokenizer ships no chat template. The trailing
    ``assistant:`` matters: without it a base model treats the transcript as
    unfinished user text and continues *that* instead of replying.
    """
    if not messages:
        raise ValueError("a chat request needs at least one message")
    lines = [f"{m.get('role', 'user')}: {m.get('content', '')}" for m in messages]
    lines.append("assistant:")
    return "\n".join(lines)


def build_completion(
    model: str, text: str, *, prompt_tokens: int, completion_tokens: int
) -> dict:
    """Wrap generated text in the envelope an OpenAI client expects.

    `usage` is not decoration. `ffsft.serve.loadtest` divides elapsed time by
    `completion_tokens` to get tokens/sec, so omitting it turns a load test
    into a division error rather than a measurement.
    """
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


#: What `loadtest._one_request` breaks its read loop on. Exact bytes matter:
#: the client tests `payload == "[DONE]"` after stripping the `data:` prefix.
SSE_DONE = "data: [DONE]\n\n"


def sse_chunk(model: str, delta: str) -> str:
    """One streamed token, in the frame shape an OpenAI client parses.

    The harness measures TTFT from the first frame whose
    `choices[0].delta.content` is non-empty, so that path has to exist exactly
    here and nowhere else.
    """
    import json

    body = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
    }
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"


def sse_usage(model: str, *, prompt_tokens: int, completion_tokens: int) -> str:
    """The final accounting frame requested by `stream_options.include_usage`.

    `choices` is empty on purpose. The client counts a token for every frame
    with delta content, so a usage frame that also carried content would be
    counted twice.
    """
    import json

    body = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"


class LocalEngine:
    """Loads a base model, optionally applies a LoRA adapter, and generates."""

    def __init__(self, model_id: str, adapter: str | None = None, dtype: str = "float32"):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        log.info("loading %s on cpu (dtype=%s)", model_id, dtype)
        self.model_id = model_id
        self.tokenizer = AutoTokenizer.from_pretrained(model_id)
        self.model = AutoModelForCausalLM.from_pretrained(
            model_id, dtype=getattr(torch, dtype), device_map="cpu"
        )
        if adapter:
            from peft import PeftModel

            log.info("applying adapter %s", adapter)
            self.model = PeftModel.from_pretrained(self.model, adapter)
        self.model.eval()
        log.info("ready")

    def _prompt(self, messages: list[dict]) -> str:
        template = getattr(self.tokenizer, "chat_template", None)
        if template:
            return self.tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
        return render_prompt(messages)

    def stream(self, req: ChatRequest):
        """Yield SSE frames as the model decodes, one frame per token.

        `TextIteratorStreamer` hands decoded text back from a worker thread
        while `generate` is still running, which is what makes time-to-first-
        token a real measurement rather than the total latency in disguise.
        """
        import threading

        import torch
        from transformers import TextIteratorStreamer

        prompt = self._prompt(req.messages)
        enc = self.tokenizer(prompt, return_tensors="pt")
        n_in = int(enc["input_ids"].shape[-1])
        model_name = req.model or self.model_id

        streamer = TextIteratorStreamer(
            self.tokenizer, skip_prompt=True, skip_special_tokens=True
        )
        kwargs = dict(
            **enc,
            max_new_tokens=req.max_tokens,
            do_sample=req.temperature > 0,
            temperature=req.temperature if req.temperature > 0 else None,
            pad_token_id=self.tokenizer.eos_token_id,
            streamer=streamer,
        )
        worker = threading.Thread(
            target=lambda: torch.no_grad()(self.model.generate)(**kwargs), daemon=True
        )
        worker.start()

        produced = 0
        for piece in streamer:
            if not piece:
                continue
            produced += 1
            yield sse_chunk(model_name, piece)
        worker.join()
        yield sse_usage(model_name, prompt_tokens=n_in, completion_tokens=produced)
        yield SSE_DONE

    def generate(self, req: ChatRequest) -> dict:
        import torch

        prompt = self._prompt(req.messages)
        enc = self.tokenizer(prompt, return_tensors="pt")
        n_in = int(enc["input_ids"].shape[-1])
        with torch.no_grad():
            out = self.model.generate(
                **enc,
                max_new_tokens=req.max_tokens,
                # Greedy unless asked otherwise: a load test wants the latency
                # distribution of the model, not of the sampler.
                do_sample=req.temperature > 0,
                temperature=req.temperature if req.temperature > 0 else None,
                pad_token_id=self.tokenizer.eos_token_id,
            )
        new = out[0][n_in:]
        text = self.tokenizer.decode(new, skip_special_tokens=True)
        return build_completion(
            req.model or self.model_id,
            text,
            prompt_tokens=n_in,
            completion_tokens=int(new.shape[-1]),
        )


def build_app(engine: LocalEngine):
    """Mount the two routes an OpenAI client and a health probe need."""
    from fastapi import FastAPI

    app = FastAPI(title="ffsft local serve")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "model": engine.model_id}

    @app.get("/v1/models")
    def models() -> dict:
        return {"object": "list", "data": [{"id": engine.model_id, "object": "model"}]}

    @app.post("/v1/chat/completions")
    def chat(req: ChatRequest):
        if req.stream:
            from fastapi.responses import StreamingResponse

            return StreamingResponse(
                engine.stream(req),
                media_type="text/event-stream",
                # Without this a proxy is free to buffer the whole stream and
                # hand it over at once, which would report TTFT == total time.
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return engine.generate(req)

    return app


def main() -> int:
    import uvicorn

    ap = argparse.ArgumentParser(description="Serve a model locally on CPU, OpenAI-compatible")
    ap.add_argument("--model", required=True, help="HF model id or local path")
    ap.add_argument("--adapter", default=None, help="Optional LoRA adapter path")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--dtype", default="float32", help="float32 is the safe CPU choice")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = build_app(LocalEngine(args.model, args.adapter, args.dtype))
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
