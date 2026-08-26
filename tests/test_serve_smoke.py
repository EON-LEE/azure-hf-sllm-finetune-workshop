"""A 200 is not evidence. These pin what reading the body has to catch.

The field-name test is the one that matters most: an earlier version of this
check read only `reasoning_content`, and the server sends `reasoning`. It saw
an empty string and reported a model that had produced 1737 characters of
reasoning. The bug survived because the mock server shared the typo.
"""

from __future__ import annotations

import io
import json

from ffsft.serve.smoke import THINKING_FIELDS, Reply, main, parse_reply


def body(message, finish="stop", usage=None):
    out = {"choices": [{"message": message, "finish_reason": finish}]}
    if usage is not None:
        out["usage"] = usage
    return out


# --------------------------------------------------------------------------
# the field name
# --------------------------------------------------------------------------


def test_thinking_is_read_from_reasoning_because_that_is_what_the_server_sends():
    """Measured on ffsft-plc/green: 4920 of 4921 SSE frames carried `reasoning`
    and not one carried `reasoning_content`."""
    got = parse_reply(body({"content": "답", "reasoning": "생각" * 10}))
    assert got.thinking == "생각" * 10
    assert got.thinking_field == "reasoning"


def test_the_older_spelling_still_counts_so_an_older_image_is_not_misread():
    got = parse_reply(body({"content": "답", "reasoning_content": "생각"}))
    assert got.thinking == "생각"
    assert got.thinking_field == "reasoning_content"


def test_the_newer_spelling_wins_when_a_server_sends_both():
    got = parse_reply(body({"content": "답", "reasoning": "새", "reasoning_content": "옛"}))
    assert got.thinking_field == THINKING_FIELDS[0] == "reasoning"
    assert got.thinking == "새"


def test_no_thinking_at_all_names_no_field_rather_than_guessing_one():
    got = parse_reply(body({"content": "답"}))
    assert got.thinking == ""
    assert got.thinking_field is None


# --------------------------------------------------------------------------
# the leak
# --------------------------------------------------------------------------


def test_a_trace_left_in_content_is_a_failure_even_though_the_reply_is_long():
    """No reasoning parser configured: every caller downstream gets scratch work."""
    got = parse_reply(body({"content": "<think>음...</think> 서울은 수도야."}))
    assert got.trace_leaked
    assert not got.ok
    assert "REASONING_PARSER" in got.summary()


def test_a_bare_closing_tag_counts_because_the_template_can_pre_close_the_block():
    assert parse_reply(body({"content": "</think> 답"})).trace_leaked


def test_a_clean_split_between_the_two_fields_is_what_passes():
    got = parse_reply(body({"content": "서울은 수도야.", "reasoning": "질문은 도시..."}))
    assert not got.trace_leaked
    assert got.ok


# --------------------------------------------------------------------------
# truncation
# --------------------------------------------------------------------------


def test_thinking_that_ate_the_whole_budget_is_named_as_such():
    """Not an empty model. A budget that ran out before the answer began --
    4908 completion tokens was measured on one hard question."""
    got = parse_reply(body({"content": "", "reasoning": "생" * 4000}, finish="length"))
    assert not got.ok
    assert "max_tokens" in got.summary()


def test_an_empty_reply_that_stopped_normally_is_still_a_failure():
    got = parse_reply(body({"content": "   "}))
    assert not got.ok
    assert "empty reply" in got.summary()


def test_usage_is_carried_through_so_the_budget_can_be_reported():
    got = parse_reply(body({"content": "답"}, usage={"completion_tokens": 295}))
    assert got.completion_tokens == 295


def test_a_response_without_usage_reports_none_rather_than_zero():
    """Zero would read as a measurement. None reads as 'the server did not say'."""
    assert parse_reply(body({"content": "답"})).completion_tokens is None


# --------------------------------------------------------------------------
# the CLI
# --------------------------------------------------------------------------


def run(monkeypatch, payload, argv=None):
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    return main(argv or [])


def test_a_good_reply_exits_zero(monkeypatch, capsys):
    code = run(monkeypatch, json.dumps(body({"content": "서울은 수도야.", "reasoning": "음"})))
    assert code == 0
    assert "reasoning" in capsys.readouterr().out


def test_a_leaked_trace_exits_nonzero_so_a_script_can_gate_on_it(monkeypatch):
    assert run(monkeypatch, json.dumps(body({"content": "<think>x</think>답"}))) == 1


def test_an_unparseable_body_says_so_instead_of_raising(monkeypatch, capsys):
    assert run(monkeypatch, "<html>502 Bad Gateway</html>") == 2
    assert "unparseable" in capsys.readouterr().out


def test_a_server_error_object_is_reported_as_an_error_not_an_empty_reply(monkeypatch, capsys):
    payload = json.dumps({"error": {"message": "model not found", "type": "NotFoundError"}})
    assert run(monkeypatch, payload) == 2
    assert "server error" in capsys.readouterr().out


def test_the_budget_line_appears_only_when_max_tokens_was_given(monkeypatch, capsys):
    payload = json.dumps(body({"content": "답"}, usage={"completion_tokens": 200}))
    run(monkeypatch, payload, ["--max-tokens", "400"])
    assert "200/400 (50%)" in capsys.readouterr().out


def test_reply_is_frozen_so_a_summary_cannot_edit_what_it_reports():
    got = parse_reply(body({"content": "답"}))
    assert isinstance(got, Reply)
    try:
        got.content = "다른 답"  # type: ignore[misc]
    except Exception as exc:
        assert "frozen" in str(exc) or "cannot assign" in str(exc)
    else:  # pragma: no cover - would mean the dataclass stopped being frozen
        raise AssertionError("Reply is no longer frozen")
