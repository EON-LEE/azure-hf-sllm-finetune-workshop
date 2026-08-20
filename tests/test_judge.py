"""Tests for the LLM-as-judge scorer -- written before src/ffsft/eval/judge.py.

Korean open-ended quality is not measurable by string overlap, so the eval layer
needs a judge model. The fragile part of any judge harness is not calling the
API, it is *parsing what the judge said*: models are asked for a number and
reply with a paragraph, a bracketed score, a fraction, or a number that appears
several times. Every case below came from a real judge output format.

These tests define the contract first; judge.py is written to satisfy them.
"""

from __future__ import annotations

import pytest

from ffsft.eval.judge import (
    JudgeResult,
    aggregate,
    build_judge_prompt,
    format_report,
    parse_score,
)


class TestParseScore:
    """The judge returns prose. We need a number, or an honest failure."""

    def test_bare_number(self):
        assert parse_score("8") == 8.0

    def test_korean_label(self):
        assert parse_score("점수: 7") == 7.0

    def test_english_label(self):
        assert parse_score("Score: 9") == 9.0

    def test_double_bracket_format(self):
        # The MT-Bench convention, which most Korean judge prompts inherit.
        assert parse_score("설명이 충분합니다. [[6]]") == 6.0

    def test_fraction_format(self):
        assert parse_score("Score: 8/10") == 8.0

    def test_decimal_score(self):
        assert parse_score("점수: 7.5") == 7.5

    def test_takes_final_score_when_judge_reasons_first(self):
        # Judges routinely mention the scale ("1에서 10까지") or an intermediate
        # opinion before committing. The concluding score is the real one, so
        # the last match must win rather than the first.
        text = (
            "1에서 10까지의 척도로 평가하겠습니다.\n"
            "답변은 정확하지만 근거가 부족합니다.\n"
            "최종 점수: 6"
        )
        assert parse_score(text) == 6.0

    def test_prefers_bracketed_score_over_incidental_numbers(self):
        text = "질문에 3개의 항목이 있고 답변은 2개만 다룹니다. [[4]]"
        assert parse_score(text) == 4.0

    def test_rejects_out_of_range_high(self):
        with pytest.raises(ValueError):
            parse_score("점수: 100")

    def test_rejects_out_of_range_low(self):
        with pytest.raises(ValueError):
            parse_score("점수: 0")

    def test_rejects_text_with_no_number(self):
        with pytest.raises(ValueError):
            parse_score("판단할 수 없습니다.")

    def test_custom_scale(self):
        assert parse_score("점수: 4", max_score=5) == 4.0
        with pytest.raises(ValueError):
            parse_score("점수: 9", max_score=5)


class TestBuildJudgePrompt:
    def test_includes_question_and_answer(self):
        prompt = build_judge_prompt(question="한국의 수도는?", answer="서울입니다.")
        assert "한국의 수도는?" in prompt
        assert "서울입니다." in prompt

    def test_includes_reference_when_given(self):
        prompt = build_judge_prompt(
            question="q", answer="a", reference="정답은 서울이다"
        )
        assert "정답은 서울이다" in prompt

    def test_omits_reference_section_when_absent(self):
        # A judge shown an empty "reference" block tends to penalise the answer
        # for not matching nothing. The section must disappear entirely.
        prompt = build_judge_prompt(question="q", answer="a")
        assert "참고 답안" not in prompt

    def test_states_the_scale_it_will_be_parsed_with(self):
        prompt = build_judge_prompt(question="q", answer="a", max_score=5)
        assert "5" in prompt

    def test_asks_for_the_bracketed_format_the_parser_prefers(self):
        assert "[[" in build_judge_prompt(question="q", answer="a")


class TestAggregate:
    def test_mean_of_single_category(self):
        results = [
            JudgeResult(key="q1", category="추론", score=8.0),
            JudgeResult(key="q2", category="추론", score=6.0),
        ]
        agg = aggregate(results)
        assert agg["추론"] == pytest.approx(7.0)
        assert agg["overall"] == pytest.approx(7.0)

    def test_overall_weights_every_question_equally_not_every_category(self):
        # Two questions in one category and one in another must not make the
        # lone question worth half the benchmark.
        results = [
            JudgeResult(key="a", category="수학", score=10.0),
            JudgeResult(key="b", category="수학", score=10.0),
            JudgeResult(key="c", category="글쓰기", score=1.0),
        ]
        agg = aggregate(results)
        assert agg["overall"] == pytest.approx(7.0)

    def test_excludes_failed_judgements_from_the_mean(self):
        # A judge that failed to answer must not be silently scored 0, which
        # would understate the model. It is dropped and counted separately.
        results = [
            JudgeResult(key="a", category="추론", score=8.0),
            JudgeResult(key="b", category="추론", score=None, error="parse failed"),
        ]
        agg = aggregate(results)
        assert agg["추론"] == pytest.approx(8.0)
        assert agg["n_failed"] == 1
        assert agg["n_scored"] == 1

    def test_empty_results_do_not_divide_by_zero(self):
        agg = aggregate([])
        assert agg["overall"] == 0.0
        assert agg["n_scored"] == 0


class TestFormatReport:
    def test_shows_each_category_and_overall(self):
        results = [
            JudgeResult(key="a", category="추론", score=8.0),
            JudgeResult(key="b", category="글쓰기", score=6.0),
        ]
        out = format_report(aggregate(results))
        assert "추론" in out
        assert "글쓰기" in out
        assert "overall" in out.lower()

    def test_surfaces_failures_rather_than_hiding_them(self):
        results = [JudgeResult(key="a", category="추론", score=None, error="timeout")]
        out = format_report(aggregate(results))
        assert "1" in out
        assert "fail" in out.lower()
