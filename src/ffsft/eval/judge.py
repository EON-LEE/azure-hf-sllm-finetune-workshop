"""LLM-as-judge scoring for open-ended Korean generation.

Multiple-choice benchmarks (KMMLU, HAE-RAE) measure knowledge, and the harness
in run.py handles those. They say nothing about whether the model writes good
Korean, which is the entire point of fine-tuning for Korean. That needs a judge
model reading the answer.

The delicate part is not calling the API. It is parsing what the judge said: you
ask for a number from 1 to 10 and get back a paragraph that mentions "1에서
10까지", counts three items in the question, and finally commits to a score. So
`parse_score` prefers the bracketed form the prompt asks for, falls back to the
*last* labelled number, and raises rather than guessing -- a judgement that
cannot be parsed is recorded as a failure and excluded from the mean, because
silently scoring it 0 would understate the model.

Question data is deliberately *not* vendored into this repo. The obvious Korean
judge benchmark, LogicKor, publishes no licence and its repository is archived
read-only, so redistributing its questions here would be a licensing problem.
Point --questions at a JSONL you have the right to use.

    python -m ffsft.eval.judge \
        --questions data/ko_eval.jsonl \
        --endpoint https://<endpoint>/v1 --endpoint-key $KEY \
        --judge-model gpt-4.1-mini
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("ffsft.eval.judge")

DEFAULT_MAX_SCORE = 10

#: Tried in order. The bracketed form is what the prompt asks for, so it wins
#: over a labelled number, which in turn wins over a bare trailing number.
_SCORE_PATTERNS = [
    re.compile(r"\[\[\s*(\d+(?:\.\d+)?)\s*\]\]"),
    re.compile(
        r"(?:최종\s*)?(?:점수|평점|score|rating)\s*[:：]?\s*(\d+(?:\.\d+)?)",
        re.IGNORECASE,
    ),
]

_BARE_NUMBER = re.compile(r"(\d+(?:\.\d+)?)")


@dataclass
class JudgeResult:
    """One question's judgement. `score is None` means the judge failed."""

    key: str
    category: str
    score: float | None = None
    answer: str = ""
    judgement: str = ""
    error: str = ""


def parse_score(text: str, *, max_score: int = DEFAULT_MAX_SCORE) -> float:
    """Extract the judge's score, or raise if it did not give a usable one.

    Raising is the point. A judge that rambled without committing to a number
    has not scored the answer, and turning that into a 0 would quietly punish
    the model for the judge's failure.
    """
    if not text or not text.strip():
        raise ValueError("empty judge response")

    for pattern in _SCORE_PATTERNS:
        matches = pattern.findall(text)
        if matches:
            # Judges reason before concluding, and often restate the scale on
            # the way ("1에서 10까지 평가하면..."). The final match is the verdict.
            value = float(matches[-1])
            return _validate(value, max_score, text)

    # Last resort: the whole response is a number, as short judge prompts get.
    stripped = text.strip()
    if _BARE_NUMBER.fullmatch(stripped):
        return _validate(float(stripped), max_score, text)

    raise ValueError(f"no score found in judge response: {text[:120]!r}")


def _validate(value: float, max_score: int, text: str) -> float:
    if not (1 <= value <= max_score):
        raise ValueError(
            f"score {value} outside 1..{max_score} in judge response: {text[:120]!r}"
        )
    return value


def build_judge_prompt(
    *,
    question: str,
    answer: str,
    reference: str = "",
    max_score: int = DEFAULT_MAX_SCORE,
) -> str:
    """Compose the judge prompt in Korean, asking for the format we parse.

    The reference block is omitted entirely when there is no reference. A judge
    shown an empty "참고 답안" section tends to mark the answer down for failing
    to match nothing.
    """
    parts = [
        "당신은 한국어 답변의 품질을 평가하는 심사위원입니다.",
        "정확성, 논리성, 한국어 표현의 자연스러움을 함께 고려하여 평가하세요.",
        "",
        f"[질문]\n{question}",
        "",
        f"[답변]\n{answer}",
    ]
    if reference:
        parts += ["", f"[참고 답안]\n{reference}"]
    parts += [
        "",
        f"먼저 평가 근거를 2~3문장으로 쓰고, 마지막 줄에 1점부터 {max_score}점 사이의",
        "점수를 반드시 [[점수]] 형식으로 출력하세요. 예: [[7]]",
    ]
    return "\n".join(parts)


def aggregate(results: list[JudgeResult]) -> dict:
    """Mean score per category plus an overall mean, ignoring failures.

    `overall` is the mean over *questions*, not the mean of category means, so a
    category holding one question does not carry the same weight as one holding
    twenty.
    """
    scored = [r for r in results if r.score is not None]
    failed = [r for r in results if r.score is None]

    by_category: dict[str, list[float]] = {}
    for result in scored:
        by_category.setdefault(result.category, []).append(result.score)

    agg: dict = {name: sum(v) / len(v) for name, v in by_category.items()}
    agg["overall"] = sum(r.score for r in scored) / len(scored) if scored else 0.0
    agg["n_scored"] = len(scored)
    agg["n_failed"] = len(failed)
    return agg


def format_report(agg: dict) -> str:
    reserved = {"overall", "n_scored", "n_failed"}
    categories = sorted(k for k in agg if k not in reserved)

    lines = ["", f"{'CATEGORY':<24} {'SCORE':>8}", "-" * 34]
    for name in categories:
        lines.append(f"{name:<24} {agg[name]:>8.2f}")
    lines.append("-" * 34)
    lines.append(f"{'overall':<24} {agg['overall']:>8.2f}")
    lines.append(f"scored {agg['n_scored']}, failed {agg['n_failed']}")
    if agg["n_failed"]:
        lines.append(
            "failed judgements are excluded from the means above, not scored 0."
        )
    return "\n".join(lines)


def load_questions(path: str) -> list[dict]:
    """Read a JSONL of {id, category, question, reference?} records."""
    records = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _chat(client, model: str, prompt: str, *, max_tokens: int, temperature: float) -> str:
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return response.choices[0].message.content or ""


def run_judged_eval(
    questions: list[dict],
    *,
    answer_client,
    answer_model: str,
    judge_client,
    judge_model: str,
    max_score: int = DEFAULT_MAX_SCORE,
    max_new_tokens: int = 512,
) -> list[JudgeResult]:
    """Generate an answer per question, then have the judge score it.

    Both clients are passed in rather than constructed here so this runs against
    a local vLLM, a managed online endpoint or Foundry without changing code --
    and so the tests can drive it with fakes.
    """
    results: list[JudgeResult] = []

    for record in questions:
        key = str(record.get("id", record.get("key", len(results))))
        category = record.get("category", "uncategorised")
        question = record["question"]
        reference = record.get("reference", "")

        result = JudgeResult(key=key, category=category)
        try:
            # temperature=0 on generation keeps the eval comparable between runs;
            # a model that scores differently on each run cannot show a delta.
            result.answer = _chat(
                answer_client,
                answer_model,
                question,
                max_tokens=max_new_tokens,
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001 - one bad question must not end the run
            result.error = f"generation failed: {exc}"
            results.append(result)
            continue

        try:
            result.judgement = _chat(
                judge_client,
                judge_model,
                build_judge_prompt(
                    question=question,
                    answer=result.answer,
                    reference=reference,
                    max_score=max_score,
                ),
                max_tokens=512,
                temperature=0.0,
            )
            result.score = parse_score(result.judgement, max_score=max_score)
        except Exception as exc:  # noqa: BLE001
            result.error = f"judging failed: {exc}"

        results.append(result)

    return results


@dataclass
class _Args:
    questions: str = ""
    endpoint: str = ""
    endpoint_key: str = ""
    model: str = "ffsft"
    judge_base_url: str = ""
    judge_key: str = ""
    judge_model: str = "gpt-4.1-mini"
    max_score: int = DEFAULT_MAX_SCORE
    out: str = ""
    extra: dict = field(default_factory=dict)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="LLM-as-judge Korean evaluation")
    parser.add_argument("--questions", required=True, help="JSONL of questions")
    parser.add_argument("--endpoint", required=True, help="OpenAI-compatible base url")
    parser.add_argument("--endpoint-key", default="")
    parser.add_argument("--model", default="ffsft")
    parser.add_argument("--judge-base-url", default="", help="defaults to --endpoint")
    parser.add_argument("--judge-key", default="")
    parser.add_argument("--judge-model", default="gpt-4.1-mini")
    parser.add_argument("--max-score", type=int, default=DEFAULT_MAX_SCORE)
    parser.add_argument("--out", default="", help="write per-question JSONL here")
    args = parser.parse_args()

    from openai import OpenAI

    answer_client = OpenAI(base_url=args.endpoint, api_key=args.endpoint_key or "none")
    judge_client = (
        OpenAI(base_url=args.judge_base_url, api_key=args.judge_key or "none")
        if args.judge_base_url
        else answer_client
    )

    questions = load_questions(args.questions)
    log.info("judging %d questions with %s", len(questions), args.judge_model)

    results = run_judged_eval(
        questions,
        answer_client=answer_client,
        answer_model=args.model,
        judge_client=judge_client,
        judge_model=args.judge_model,
        max_score=args.max_score,
    )

    for result in results:
        if result.error:
            log.warning("%s: %s", result.key, result.error)

    print(format_report(aggregate(results)))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            for result in results:
                handle.write(
                    json.dumps(
                        {
                            "key": result.key,
                            "category": result.category,
                            "score": result.score,
                            "answer": result.answer,
                            "judgement": result.judgement,
                            "error": result.error,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        log.info("wrote %s", args.out)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
