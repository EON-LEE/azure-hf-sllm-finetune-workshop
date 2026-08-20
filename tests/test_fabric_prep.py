"""Tests for the Fabric-side Korean corpus preparation -- written first.

This is the half of the asset that runs on Fabric's Spark pool, and the reason
it exists is economic: filtering and deduplicating a Korean corpus is pure CPU
work, and doing it inside the training job burns an A100 at $4.96/hr to run
what is effectively pandas. Fabric does it on cheap CPU nodes and hands Azure ML
a clean JSONL.

Because it runs on Spark, the logic is kept as plain functions here and the
notebook is a thin caller. Spark cannot be unit-tested cheaply; these functions
can, and they are where every actual bug lives.
"""

from __future__ import annotations

import pytest

from ffsft.data.fabric_prep import (
    build_chat_record,
    dedup_key,
    hangul_ratio,
    normalize_text,
    quality_reasons,
)


class TestHangulRatio:
    def test_pure_korean(self):
        assert hangul_ratio("안녕하세요") == pytest.approx(1.0)

    def test_pure_english(self):
        assert hangul_ratio("hello world") == 0.0

    def test_mixed(self):
        # "한국어" is 3 Hangul letters, "test" is 4 Latin: 3 of 7. Spaces and
        # punctuation are excluded from both sides, or the ratio would swing on
        # formatting alone.
        assert hangul_ratio("한국어 test") == pytest.approx(3 / 7)

    def test_ignores_whitespace_and_punctuation(self):
        assert hangul_ratio("안녕!!!   ???") == pytest.approx(1.0)

    def test_empty_string_is_zero_not_error(self):
        assert hangul_ratio("") == 0.0

    def test_digits_do_not_count_as_either(self):
        assert hangul_ratio("한글 123") == pytest.approx(1.0)


class TestNormalizeText:
    def test_collapses_runs_of_whitespace(self):
        assert normalize_text("안녕   하세요") == "안녕 하세요"

    def test_strips_leading_and_trailing(self):
        assert normalize_text("  안녕  ") == "안녕"

    def test_normalizes_newlines_but_keeps_paragraphs(self):
        # Losing paragraph structure would damage long-form Korean training
        # data, so a blank line survives while five do not.
        assert normalize_text("가\n\n\n\n나") == "가\n\n나"

    def test_applies_nfc_so_hangul_compares_equal(self):
        # macOS and some crawlers emit decomposed Hangul (NFD). Without NFC the
        # same word hashes differently and dedup silently stops working.
        decomposed = "\u1100\u1161"  # ㄱ + ㅏ
        assert normalize_text(decomposed) == "가"


class TestDedupKey:
    def test_identical_text_shares_a_key(self):
        assert dedup_key("안녕하세요") == dedup_key("안녕하세요")

    def test_whitespace_differences_share_a_key(self):
        assert dedup_key("안녕 하세요") == dedup_key("안녕    하세요")

    def test_unicode_form_differences_share_a_key(self):
        assert dedup_key("\u1100\u1161") == dedup_key("가")

    def test_different_text_differs(self):
        assert dedup_key("안녕하세요") != dedup_key("반갑습니다")

    def test_punctuation_only_difference_shares_a_key(self):
        # Crawled corpora repeat the same sentence with and without a trailing
        # period constantly; treating those as distinct inflates the dataset.
        assert dedup_key("안녕하세요.") == dedup_key("안녕하세요")


class TestQualityReasons:
    def test_clean_record_has_no_reasons(self):
        assert quality_reasons("한국어로 질문합니다", "한국어로 충분히 긴 답변을 드립니다") == []

    def test_flags_empty_instruction(self):
        assert "empty_instruction" in quality_reasons("", "답변입니다")

    def test_flags_empty_output(self):
        assert "empty_output" in quality_reasons("질문입니다", "")

    def test_flags_output_too_short(self):
        assert "output_too_short" in quality_reasons("질문입니다", "네", min_output_chars=10)

    def test_flags_low_korean_ratio(self):
        reasons = quality_reasons("what is this", "this is entirely english text here")
        assert "not_korean" in reasons

    def test_flags_output_echoing_the_instruction(self):
        # A frequent artefact of scraped instruction sets; training on it
        # teaches the model to repeat the prompt.
        text = "한국의 수도는 어디입니까"
        assert "output_echoes_instruction" in quality_reasons(text, text)

    def test_flags_excessive_repetition(self):
        assert "repetitive" in quality_reasons("질문입니다", "네 " * 200)

    def test_accumulates_multiple_reasons(self):
        assert len(quality_reasons("", "")) >= 2

    def test_threshold_is_configurable(self):
        mixed = "한국어 English mixed text 입니다 여기"
        assert "not_korean" not in quality_reasons("질문 입니다", mixed, min_hangul_ratio=0.1)
        assert "not_korean" in quality_reasons("질문 입니다", mixed, min_hangul_ratio=0.9)


class TestBuildChatRecord:
    def test_produces_openai_style_messages(self):
        record = build_chat_record(instruction="질문", output="답변")
        assert record["messages"] == [
            {"role": "user", "content": "질문"},
            {"role": "assistant", "content": "답변"},
        ]

    def test_prepends_system_prompt_when_given(self):
        record = build_chat_record(instruction="질문", output="답변", system="너는 친절하다")
        assert record["messages"][0] == {"role": "system", "content": "너는 친절하다"}
        assert len(record["messages"]) == 3

    def test_includes_input_in_the_user_turn(self):
        # Alpaca-style Korean sets carry a separate `input` field; dropping it
        # makes the answer unexplainable from the prompt.
        record = build_chat_record(instruction="요약하라", output="요약본", input_text="긴 본문")
        user = record["messages"][0]["content"]
        assert "요약하라" in user
        assert "긴 본문" in user

    def test_normalizes_text_so_output_matches_dedup(self):
        record = build_chat_record(instruction="  질문   입니다 ", output="답변")
        assert record["messages"][0]["content"] == "질문 입니다"

    def test_carries_source_for_provenance(self):
        record = build_chat_record(instruction="q", output="a", source="kullm-v2")
        assert record["source"] == "kullm-v2"
