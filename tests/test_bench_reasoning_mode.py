"""The client half of the serving mode has to match the server half.

`bench_job.bench_env` reuses `serving_env`, so the vLLM under test is flagged
exactly as a deployment would flag it -- including `--reasoning-parser qwen3`.
That parser routes a Qwen3 `<think>` block into `reasoning_content` instead of
`content`. Nothing wired the other half: the load-test client never sent
`chat_template_kwargs`, so the server thought by default, and the client counted
only `content`.

The result was a full table of numbers that looked like a measurement.
`plum_wall_318nsvlvt6` reported 24 ok / 40 failed at *every* concurrency level
-- identical at 1 and at 32, because 64 requests cycle through 8 prompts and the
split was 3 prompts whose thinking closed within `max_tokens` against 5 whose
did not. A load-dependent failure cannot be flat across a 32x range; that
flatness was the tell.

It also measured the wrong model. `train/qlora.py` renders the training prompts
with `enable_thinking=false` and says in as many words that inference must
match, so a sweep that leaves thinking on is not a slower measurement of the
tuned model -- it is a measurement of a different one.

These tests pin both halves and the shell that carries the value between them.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ffsft.serve.bench_job import BenchSpec, bench_env
from ffsft.serve.loadtest import _one_request

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCH_ENTRYPOINT = REPO_ROOT / "docker" / "bench_entrypoint.sh"


# -- a stand-in for httpx.AsyncClient.stream ----------------------------


class _FakeResponse:
    def __init__(self, lines: list[str], status: int = 200):
        self.status_code = status
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return b""


class _FakeStream:
    def __init__(self, client, lines, status):
        self._client = client
        self._lines = lines
        self._status = status

    async def __aenter__(self):
        return _FakeResponse(self._lines, self._status)

    async def __aexit__(self, *exc):
        return False


class FakeClient:
    """Records the body it was handed and replays a canned SSE stream."""

    def __init__(self, lines: list[str], status: int = 200):
        self.lines = lines
        self.status = status
        self.bodies: list[dict] = []

    def stream(self, method, url, *, json=None, headers=None, timeout=None):
        self.bodies.append(json)
        return _FakeStream(self, self.lines, self.status)


def sse(*deltas: dict) -> list[str]:
    frames = [
        "data: " + json.dumps({"choices": [{"delta": d}]}) for d in deltas
    ]
    return [*frames, "data: [DONE]"]


def call(client, **kw):
    return asyncio.run(
        _one_request(
            client,
            "http://x/v1",
            "ffsft",
            "안녕하세요",
            kw.pop("max_tokens", 128),
            0.0,
            {},
            30.0,
            kw.pop("chat_template_kwargs", None),
        )
    )


# -- the request body ---------------------------------------------------


def test_chat_template_kwargs_reach_the_request_body():
    client = FakeClient(sse({"content": "네"}))
    call(client, chat_template_kwargs={"enable_thinking": False})
    assert client.bodies[0]["chat_template_kwargs"] == {"enable_thinking": False}


def test_no_kwargs_means_the_key_is_absent_not_empty():
    """`{}` and "unset" are not the same request to vLLM."""
    client = FakeClient(sse({"content": "네"}))
    call(client, chat_template_kwargs=None)
    assert "chat_template_kwargs" not in client.bodies[0]


def test_the_caller_cannot_mutate_the_body_afterwards():
    shared = {"enable_thinking": False}
    client = FakeClient(sse({"content": "네"}))
    call(client, chat_template_kwargs=shared)
    shared["enable_thinking"] = True
    assert client.bodies[0]["chat_template_kwargs"] == {"enable_thinking": False}


# -- what counts as a token ---------------------------------------------


def test_reasoning_content_counts_as_a_streamed_token():
    """The exact regression that scored 40 of 64 requests as failures."""
    client = FakeClient(sse({"reasoning_content": "생각"}, {"reasoning_content": "중"}))
    res = call(client)
    assert res.ok, res.error
    assert res.output_tokens == 2


def test_a_response_that_never_leaves_thinking_is_still_a_success():
    client = FakeClient(sse(*({"reasoning_content": "x"} for _ in range(128))))
    res = call(client)
    assert res.ok
    assert res.error == ""


def test_ttft_is_measured_from_the_first_token_of_either_kind():
    client = FakeClient(sse({"reasoning_content": "생각"}, {"content": "답"}))
    res = call(client)
    assert res.ok
    assert res.ttft_s > 0.0
    assert res.output_tokens == 2


def test_reasoning_counts_too_because_that_is_the_field_the_real_server_sends():
    """`reasoning`, not `reasoning_content`, is what ffsft-plc/green streams.

    Measured on the live endpoint: of 4921 SSE frames, 4920 carried
    `delta.reasoning` and zero carried `delta.reasoning_content`. Every test
    above passes against the bundled mock, which emits `reasoning_content` --
    so the suite was green while the meter scored the deployment at 0.
    """
    client = FakeClient(sse({"reasoning": "생각"}, {"reasoning": "중"}))
    res = call(client)
    assert res.ok, res.error
    assert res.output_tokens == 2


def test_a_thinking_stream_that_switches_field_name_midway_is_counted_once_each():
    # Both spellings in one stream must not double-count a single delta.
    client = FakeClient(sse({"reasoning": "a"}, {"reasoning_content": "b"}, {"content": "c"}))
    res = call(client)
    assert res.ok
    assert res.output_tokens == 3


def test_a_stream_with_no_deltas_at_all_is_still_a_failure():
    """Widening the meter must not turn a genuinely empty response green."""
    client = FakeClient(sse({}, {"role": "assistant"}))
    res = call(client)
    assert not res.ok
    assert res.error == "no tokens streamed"


# -- the job environment ------------------------------------------------


class _Spec:
    def __init__(self, ctk):
        self.chat_template_kwargs = ctk
        self.reasoning_parser = "qwen3"
        self.multimodal = False
        self.hf_id = "Qwen/Qwen3.8-27B"
        self.trust_remote_code = False
        self.mamba_cache_mode = None
        self.served_model_name = "ffsft"


@pytest.fixture
def spec():
    return BenchSpec(model_asset="qwen3_8-27b-ko-merged:1")


def test_bench_env_carries_the_registry_mode_to_the_client(spec, monkeypatch):
    monkeypatch.setattr("ffsft.serve.bench_job.serving_env", lambda *a, **k: {})
    env = bench_env(spec, _Spec({"enable_thinking": False}))
    assert json.loads(env["BENCH_CHAT_TEMPLATE_KWARGS"]) == {"enable_thinking": False}


def test_bench_env_omits_the_variable_when_the_registry_declares_nothing(spec, monkeypatch):
    monkeypatch.setattr("ffsft.serve.bench_job.serving_env", lambda *a, **k: {})
    env = bench_env(spec, _Spec({}))
    assert "BENCH_CHAT_TEMPLATE_KWARGS" not in env


def test_bench_env_omits_the_variable_when_there_is_no_model_spec(spec, monkeypatch):
    monkeypatch.setattr("ffsft.serve.bench_job.serving_env", lambda *a, **k: {})
    env = bench_env(spec, None)
    assert "BENCH_CHAT_TEMPLATE_KWARGS" not in env


# -- the shell that carries it ------------------------------------------


def test_the_entrypoint_forwards_the_flag_only_when_it_is_set():
    source = BENCH_ENTRYPOINT.read_text(encoding="utf-8")
    # `${VAR:+--flag "$VAR"}` expands to nothing when unset, which is what keeps
    # an absent registry setting from becoming `--chat-template-kwargs ""`.
    assert '${CHAT_TEMPLATE_KWARGS:+--chat-template-kwargs "${CHAT_TEMPLATE_KWARGS}"}' in source
    assert 'CHAT_TEMPLATE_KWARGS="${BENCH_CHAT_TEMPLATE_KWARGS:-}"' in source


def test_the_smoke_test_asks_in_the_same_mode_as_the_sweep():
    source = BENCH_ENTRYPOINT.read_text(encoding="utf-8")
    smoke = source.split('RUN_PHASE="smoke"', 1)[1].split('RUN_PHASE="sweeping"', 1)[0]
    assert "chat_template_kwargs" in smoke
