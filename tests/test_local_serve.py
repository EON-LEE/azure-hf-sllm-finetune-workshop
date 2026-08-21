"""The only serving surface this subscription leaves open is the local one.

§22 zeroed dedicated GPU quota, which rules out managed online endpoints, and
§24 showed the workspace datastore is unreachable, which rules out batch and AKS
as well because both deploy a registered model asset. `local_vllm` is what is
left, and vLLM wants a GPU that this machine does not have either.

So the local pattern needs a second engine: plain `transformers` on CPU. It is
slow and it is not what anyone should serve production traffic with, but it is
an *OpenAI-compatible endpoint that actually answers*, which is the one thing
`ffsft-loadtest` needs and has never had. A harness that has never run against
a live server is a harness with unknown behaviour.

These tests cover the request/response shaping, which is where wire-protocol
bugs live, without starting a server or loading a model.
"""

from __future__ import annotations

import pytest

from ffsft.serve.local import (
    ChatRequest,
    build_completion,
    render_prompt,
)


def test_render_prompt_keeps_turn_order():
    text = render_prompt(
        [
            {"role": "system", "content": "너는 한국어 비서다"},
            {"role": "user", "content": "안녕"},
        ]
    )
    assert text.index("너는 한국어 비서다") < text.index("안녕")


def test_render_prompt_labels_each_role():
    text = render_prompt([{"role": "user", "content": "안녕"}])
    assert "user" in text
    assert "안녕" in text


def test_render_prompt_invites_the_assistant_to_speak():
    """Without a trailing assistant cue the model continues the user's turn."""
    assert render_prompt([{"role": "user", "content": "hi"}]).rstrip().endswith("assistant:")


def test_render_prompt_rejects_an_empty_conversation():
    with pytest.raises(ValueError):
        render_prompt([])


def test_completion_uses_the_openai_envelope():
    body = build_completion("qwen3-0.6b", "안녕하세요", prompt_tokens=7, completion_tokens=3)
    assert body["object"] == "chat.completion"
    assert body["model"] == "qwen3-0.6b"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["message"]["content"] == "안녕하세요"
    assert body["choices"][0]["finish_reason"] == "stop"


def test_completion_reports_usage_the_load_test_reads():
    """`loadtest` divides by completion_tokens; a missing field is a crash."""
    body = build_completion("m", "hi", prompt_tokens=7, completion_tokens=3)
    assert body["usage"] == {
        "prompt_tokens": 7,
        "completion_tokens": 3,
        "total_tokens": 10,
    }


def test_completion_ids_are_unique():
    a = build_completion("m", "x", prompt_tokens=1, completion_tokens=1)
    b = build_completion("m", "x", prompt_tokens=1, completion_tokens=1)
    assert a["id"] != b["id"]


def test_chat_request_defaults_match_the_openai_client():
    req = ChatRequest(model="m", messages=[{"role": "user", "content": "hi"}])
    assert req.max_tokens == 64
    assert req.stream is False


def test_chat_request_accepts_the_fields_loadtest_sends():
    req = ChatRequest(
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=32,
        temperature=0.0,
    )
    assert req.max_tokens == 32
    assert req.temperature == 0.0


# ---------------------------------------------------------------------------
# Streaming. Added after the first live load test returned HTTP 200 twelve
# times and the harness scored all twelve as failures:
#
#     ok=0 fail=4 | errors: {'no tokens streamed': 4}
#
# `loadtest._one_request` sends `stream: true` and measures TTFT from the first
# SSE frame carrying `choices[0].delta.content`. A server that answers with one
# whole JSON body is not load-testable at all -- TTFT is undefined for it -- so
# the endpoint has to speak SSE for the harness to say anything about it.
# ---------------------------------------------------------------------------

from ffsft.serve.local import SSE_DONE, sse_chunk, sse_usage  # noqa: E402


def _payload(frame: str) -> dict:
    import json

    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    return json.loads(frame[6:].strip())


def test_chunk_is_a_terminated_sse_frame():
    frame = sse_chunk("m", "안녕")
    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")


def test_chunk_carries_delta_content_where_the_harness_looks():
    body = _payload(sse_chunk("m", "안녕"))
    assert body["choices"][0]["delta"]["content"] == "안녕"


def test_chunk_declares_the_streaming_object_type():
    assert _payload(sse_chunk("m", "x"))["object"] == "chat.completion.chunk"


def test_usage_frame_reports_completion_tokens():
    body = _payload(sse_usage("m", prompt_tokens=5, completion_tokens=9))
    assert body["usage"]["completion_tokens"] == 9
    assert body["usage"]["total_tokens"] == 14


def test_usage_frame_has_no_choices_to_miscount():
    """A usage frame carrying a delta would inflate the token count."""
    assert _payload(sse_usage("m", prompt_tokens=1, completion_tokens=1))["choices"] == []


def test_done_sentinel_is_exactly_what_the_client_breaks_on():
    assert SSE_DONE == "data: [DONE]\n\n"
