"""Load test for any OpenAI-compatible LLM endpoint.

Written against the wire protocol, not against Azure, so the same client
measures a local `vllm serve`, an Azure ML managed online endpoint, an AKS
deployment or a Foundry model deployment. That is the point of insisting on
`openai_compatible: true` in the serving registry.

What it measures, and why these four and not "requests per second":

* **TTFT** (time to first token) -- what a user perceives as responsiveness.
  Dominated by prefill, so it grows with prompt length and with queue depth.
* **TPOT** (time per output token, a.k.a. inter-token latency) -- how fast the
  answer streams once it starts. Dominated by decode and by how many requests
  the engine is batching together.
* **End-to-end latency** -- TTFT + TPOT x output_tokens. Reported as p50/p95/p99
  because the mean hides exactly the tail that breaks SLOs.
* **Output token throughput** -- the number that decides how many GPUs you buy.

A single-request benchmark tells you almost nothing about a batching engine like
vLLM: throughput climbs and TTFT degrades as concurrency rises. So the default
mode is a *sweep* over concurrency levels, which is what produces the latency /
throughput curve you actually size a deployment from.

    ffsft-loadtest --base-url http://localhost:8000/v1 --model qwen3.8-27b-ko \\
        --concurrency 1,4,8,16 --requests-per-level 32
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field

log = logging.getLogger("ffsft.loadtest")

#: Korean prompts of deliberately different lengths. Prompt length drives prefill
#: cost, so a single fixed prompt would produce a flattering, unrealistic TTFT.
DEFAULT_PROMPTS: list[str] = [
    "안녕하세요. 간단히 자기소개해 주세요.",
    "한국의 4대 명절을 나열하고 각각 한 문장으로 설명해 주세요.",
    "다음 문장을 정중한 존댓말로 바꿔줘: '내일 회의 몇시야?'",
    "머신러닝에서 과적합이 무엇인지 비전공자에게 설명해 주세요.",
    "서울에서 부산까지 가는 교통수단을 비용과 소요시간 관점에서 비교해 주세요.",
    "다음 요구사항으로 파이썬 함수를 작성해줘: 리스트에서 중복을 제거하되 순서는 유지할 것.",
    "전세와 월세의 차이를 표로 정리하고, 각각 어떤 상황에 유리한지 설명해 주세요.",
    "'배가 고프다'와 '배가 아프다'에서 '배'의 의미 차이를 설명하고 예문을 3개 만들어 주세요.",
]


@dataclass
class RequestResult:
    """One request's timings. `ok=False` rows are excluded from the percentiles."""

    ok: bool
    status: int = 0
    error: str = ""
    ttft_s: float = 0.0
    total_s: float = 0.0
    output_tokens: int = 0
    prompt_chars: int = 0

    @property
    def tpot_s(self) -> float:
        """Seconds per output token after the first one."""
        if self.output_tokens <= 1:
            return 0.0
        return (self.total_s - self.ttft_s) / (self.output_tokens - 1)


@dataclass
class LevelResult:
    """Aggregate for one concurrency level."""

    concurrency: int
    requests: int
    succeeded: int
    failed: int
    wall_s: float
    ttft_p50: float = 0.0
    ttft_p95: float = 0.0
    ttft_p99: float = 0.0
    tpot_p50: float = 0.0
    tpot_p95: float = 0.0
    e2e_p50: float = 0.0
    e2e_p95: float = 0.0
    e2e_p99: float = 0.0
    output_tokens: int = 0
    output_tok_per_s: float = 0.0
    requests_per_s: float = 0.0
    errors: dict[str, int] = field(default_factory=dict)


def _pct(values: list[float], q: float) -> float:
    """Percentile with linear interpolation; safe on tiny samples."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 4)
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return round(ordered[lo] * (1 - frac) + ordered[hi] * frac, 4)


async def _one_request(
    client,
    base_url: str,
    model: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    headers: dict[str, str],
    timeout: float,
    chat_template_kwargs: dict[str, object] | None = None,
) -> RequestResult:
    """Issue one streaming chat completion and time the token stream.

    Streaming is mandatory: TTFT is unmeasurable from a non-streaming response,
    and TTFT is the metric that separates a healthy deployment from one that is
    quietly queueing.
    """
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        # vLLM and Azure OpenAI both honour this; it gives an exact token count
        # instead of one inferred from the number of SSE frames.
        "stream_options": {"include_usage": True},
    }
    if chat_template_kwargs:
        # The registry declares these per model -- `enable_thinking: false` for
        # Qwen3 -- and `bench_job.serving_env` already flags the server to match.
        # Sending them is not a convenience: `train/qlora.py` renders the
        # training prompts through the same kwargs, so omitting them here
        # measures the model in a different mode than the one that was tuned.
        body["chat_template_kwargs"] = dict(chat_template_kwargs)
    started = time.perf_counter()
    ttft = 0.0
    tokens = 0
    usage_tokens = 0

    try:
        async with client.stream(
            "POST",
            f"{base_url.rstrip('/')}/chat/completions",
            json=body,
            headers=headers,
            timeout=timeout,
        ) as resp:
            if resp.status_code != 200:
                text = (await resp.aread()).decode("utf-8", "replace")[:200]
                return RequestResult(
                    ok=False, status=resp.status_code, error=text, prompt_chars=len(prompt)
                )
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    usage_tokens = chunk["usage"].get("completion_tokens", 0) or usage_tokens
                for choice in chunk.get("choices", []):
                    delta = choice.get("delta") or {}
                    # `reasoning_content` counts. With `--reasoning-parser`
                    # active the server routes a Qwen3 <think> block there
                    # rather than into `content`, and those tokens are decoded
                    # on the GPU like any other. Counting only `content` scored
                    # a request whose thinking had not closed within
                    # `max_tokens` as "no tokens streamed" -- 40 of 64 at every
                    # concurrency level in `plum_wall_318nsvlvt6`, identical at
                    # concurrency 1 and 32 because it is a property of the
                    # prompt, not of the queue. See JOURNAL 55.
                    # ...and so does `reasoning`. The image serving this
                    # endpoint streams the thinking block under `reasoning`,
                    # NOT `reasoning_content`: 4920 of 4921 SSE frames from
                    # `ffsft-plc/green` carried `delta.reasoning` and not one
                    # carried `delta.reasoning_content`. The bundled mock emits
                    # `reasoning_content`, so every test here passed while the
                    # real server was scoring 0 -- the same shape of mistake as
                    # JOURNAL 55, one field name later. See JOURNAL 68.
                    if (
                        delta.get("content")
                        or delta.get("reasoning_content")
                        or delta.get("reasoning")
                    ):
                        if tokens == 0:
                            ttft = time.perf_counter() - started
                        tokens += 1
    except Exception as exc:  # noqa: BLE001 - a load test must survive any failure
        return RequestResult(
            ok=False, error=f"{type(exc).__name__}: {exc}"[:200], prompt_chars=len(prompt)
        )

    total = time.perf_counter() - started
    if tokens == 0:
        return RequestResult(ok=False, error="no tokens streamed", prompt_chars=len(prompt))
    return RequestResult(
        ok=True,
        status=200,
        ttft_s=ttft,
        total_s=total,
        # Prefer the server's own count; SSE frames are usually but not always 1:1.
        output_tokens=usage_tokens or tokens,
        prompt_chars=len(prompt),
    )


def summarize(results: list[RequestResult], concurrency: int, wall: float) -> LevelResult:
    """Turn raw per-request timings into one level's percentile summary."""
    ok = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]
    errors: dict[str, int] = {}
    for r in bad:
        key = r.error.split("\n")[0][:80] or f"http {r.status}"
        errors[key] = errors.get(key, 0) + 1

    total_tokens = sum(r.output_tokens for r in ok)
    return LevelResult(
        concurrency=concurrency,
        requests=len(results),
        succeeded=len(ok),
        failed=len(bad),
        wall_s=round(wall, 3),
        ttft_p50=_pct([r.ttft_s for r in ok], 0.50),
        ttft_p95=_pct([r.ttft_s for r in ok], 0.95),
        ttft_p99=_pct([r.ttft_s for r in ok], 0.99),
        tpot_p50=_pct([r.tpot_s for r in ok if r.output_tokens > 1], 0.50),
        tpot_p95=_pct([r.tpot_s for r in ok if r.output_tokens > 1], 0.95),
        e2e_p50=_pct([r.total_s for r in ok], 0.50),
        e2e_p95=_pct([r.total_s for r in ok], 0.95),
        e2e_p99=_pct([r.total_s for r in ok], 0.99),
        output_tokens=total_tokens,
        output_tok_per_s=round(total_tokens / wall, 2) if wall else 0.0,
        requests_per_s=round(len(ok) / wall, 3) if wall else 0.0,
        errors=errors,
    )


async def run_level(
    base_url: str,
    model: str,
    concurrency: int,
    requests: int,
    *,
    prompts: list[str] | None = None,
    max_tokens: int = 128,
    temperature: float = 0.0,
    api_key: str | None = None,
    timeout: float = 300.0,
    chat_template_kwargs: dict[str, object] | None = None,
) -> LevelResult:
    """Drive `requests` requests through the endpoint at a fixed concurrency."""
    import httpx

    prompts = prompts or DEFAULT_PROMPTS
    headers = {"Content-Type": "application/json"}
    if api_key:
        # Azure ML online endpoints use `Authorization: Bearer`, and so does the
        # OpenAI protocol, so one header covers both.
        headers["Authorization"] = f"Bearer {api_key}"

    sem = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency + 4, max_keepalive_connections=concurrency)

    async with httpx.AsyncClient(limits=limits, http2=False) as client:

        async def bounded(i: int) -> RequestResult:
            async with sem:
                return await _one_request(
                    client,
                    base_url,
                    model,
                    prompts[i % len(prompts)],
                    max_tokens,
                    temperature,
                    headers,
                    timeout,
                    chat_template_kwargs,
                )

        started = time.perf_counter()
        results = await asyncio.gather(*(bounded(i) for i in range(requests)))
        wall = time.perf_counter() - started

    return summarize(list(results), concurrency, wall)


async def sweep(
    base_url: str,
    model: str,
    levels: list[int],
    requests_per_level: int,
    **kwargs,
) -> list[LevelResult]:
    """Run one level per concurrency value, sequentially.

    Sequential on purpose: overlapping levels would contaminate each other's
    queue depth and every number below would be meaningless.
    """
    out: list[LevelResult] = []
    for c in levels:
        log.info("concurrency=%d, %d requests ...", c, requests_per_level)
        res = await run_level(base_url, model, c, requests_per_level, **kwargs)
        log.info(
            "  ok=%d fail=%d | TTFT p50 %.3fs p95 %.3fs | TPOT p50 %.4fs | %.1f tok/s",
            res.succeeded, res.failed, res.ttft_p50, res.ttft_p95,
            res.tpot_p50, res.output_tok_per_s,
        )
        if res.errors:
            log.warning("  errors: %s", res.errors)
        out.append(res)
    return out


def format_table(results: list[LevelResult]) -> str:
    """Plain-text summary that survives a log pipe, no rich dependency."""
    head = (
        f"{'conc':>5} {'ok':>5} {'fail':>5} {'TTFT p50':>9} {'TTFT p95':>9} "
        f"{'TPOT p50':>9} {'e2e p95':>9} {'tok/s':>9} {'req/s':>8}"
    )
    lines = [head, "-" * len(head)]
    for r in results:
        lines.append(
            f"{r.concurrency:>5} {r.succeeded:>5} {r.failed:>5} "
            f"{r.ttft_p50:>9.3f} {r.ttft_p95:>9.3f} {r.tpot_p50:>9.4f} "
            f"{r.e2e_p95:>9.3f} {r.output_tok_per_s:>9.1f} {r.requests_per_s:>8.2f}"
        )
    return "\n".join(lines)


def find_knee(results: list[LevelResult], ttft_slo_s: float) -> LevelResult | None:
    """Highest concurrency level whose p95 TTFT still meets the SLO.

    This is the number you size a deployment with: peak throughput is useless if
    it is reached at a latency your product cannot ship.
    """
    passing = [r for r in results if r.succeeded and r.ttft_p95 <= ttft_slo_s and not r.failed]
    return max(passing, key=lambda r: r.concurrency) if passing else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Load test an OpenAI-compatible LLM endpoint")
    ap.add_argument("--base-url", required=True, help="e.g. http://localhost:8000/v1")
    ap.add_argument("--model", required=True, help="Model name the endpoint expects")
    ap.add_argument("--concurrency", default="1,4,8,16", help="Comma-separated levels to sweep.")
    ap.add_argument("--requests-per-level", type=int, default=32)
    ap.add_argument("--max-tokens", type=int, default=128)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--ttft-slo", type=float, default=1.0, help="p95 TTFT budget in seconds.")
    ap.add_argument(
        "--api-key",
        default=None,
        help="Defaults to $FFSFT_ENDPOINT_KEY. Never hardcode this.",
    )
    ap.add_argument(
        "--chat-template-kwargs",
        default=None,
        help="JSON object forwarded as `chat_template_kwargs`, e.g. "
        '\'{"enable_thinking": false}\'. Must match what training rendered.',
    )
    ap.add_argument("--output", default=None, help="Write the full JSON report here.")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s | %(message)s"
    )
    levels = [int(x) for x in args.concurrency.split(",") if x.strip()]
    # An unparseable value is fatal rather than ignored: silently dropping it
    # would run the whole sweep against a model in the wrong mode and publish
    # the numbers as if they were comparable.
    ctk = json.loads(args.chat_template_kwargs) if args.chat_template_kwargs else None
    if ctk is not None and not isinstance(ctk, dict):
        raise SystemExit("--chat-template-kwargs must be a JSON object")

    results = asyncio.run(
        sweep(
            args.base_url,
            args.model,
            levels,
            args.requests_per_level,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            api_key=args.api_key or os.environ.get("FFSFT_ENDPOINT_KEY"),
            timeout=args.timeout,
            chat_template_kwargs=ctk,
        )
    )

    print()
    print(format_table(results))
    knee = find_knee(results, args.ttft_slo)
    print()
    if knee:
        print(
            f"Max concurrency meeting p95 TTFT <= {args.ttft_slo}s: {knee.concurrency} "
            f"({knee.output_tok_per_s:.1f} output tok/s, {knee.requests_per_s:.2f} req/s)"
        )
    else:
        print(f"No concurrency level met the p95 TTFT SLO of {args.ttft_slo}s.")

    report = {
        "base_url": args.base_url,
        "model": args.model,
        "max_tokens": args.max_tokens,
        # Recorded because it changes what the model does. Two sweeps with
        # different values are not comparable, and without this the report does
        # not say which one it is.
        "chat_template_kwargs": ctk,
        "ttft_slo_s": args.ttft_slo,
        "levels": [asdict(r) for r in results],
        "knee_concurrency": knee.concurrency if knee else None,
        "peak_output_tok_per_s": max((r.output_tok_per_s for r in results), default=0.0),
    }
    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
        log.info("wrote %s", args.output)

    failed_total = sum(r.failed for r in results)
    if failed_total:
        log.error("%d requests failed across the sweep", failed_total)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
