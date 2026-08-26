#!/usr/bin/env python
"""Send the same prompts to two deployments of one endpoint and diff the replies.

`ffsft-loadtest` reports aggregates. An aggregate cannot tell you *why* one
deployment emits fewer tokens than another, and there are three different
causes with three different meanings:

  finish_reason="length"   the answer was cut at the cap -- the number is a
                           floor, and the real gap is unknown
  a reasoning trace in     the model is spending output tokens on thinking
  `content`                that the caller did not ask for
  finish_reason="stop"     the model genuinely said less

The reference run's 9.2% token gap looked like the middle one and was written
up that way. It was the first: 6 of 8 prompts sat on the cap in *both*
deployments, and one prompt carried the entire aggregate (PERFORMANCE §6.1,
JOURNAL §70). This script is what settles that, and it is worth running before
anyone reads a tok/s difference as a speed difference.

Traffic is not touched: each request is addressed with the
`azureml-model-deployment` header, so a deployment at 0% traffic answers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from ffsft.serve.loadtest import DEFAULT_PROMPTS


def ask(client, base_url: str, deployment: str, prompt: str, args) -> dict:
    body = {
        "model": args.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
    }
    if args.enable_thinking is not None:
        body["chat_template_kwargs"] = {"enable_thinking": args.enable_thinking}
    r = client.post(
        f"{base_url.rstrip('/')}/chat/completions",
        json=body,
        headers={"azureml-model-deployment": deployment},
        timeout=args.timeout,
    )
    r.raise_for_status()
    data = r.json()
    choice = data["choices"][0]
    message = choice["message"]
    content = message.get("content") or ""
    # `reasoning`, not `reasoning_content`: vLLM renamed the field and reading
    # the old one reports "no thinking" on a model that is thinking (JOURNAL §68).
    reasoning = message.get("reasoning") or ""
    return {
        "finish_reason": choice.get("finish_reason"),
        "completion_tokens": data.get("usage", {}).get("completion_tokens"),
        "prompt_tokens": data.get("usage", {}).get("prompt_tokens"),
        "chars": len(content),
        "reasoning_tokens": len(reasoning),
        "thinking_in_content": "<think>" in content,
        "content": content,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="compare_deployments.py",
        description="Same prompts, two deployments, one table of finish_reason and token counts.",
    )
    ap.add_argument("--base-url", required=True, help="e.g. https://<ep>.<region>....azure.com/v1")
    ap.add_argument("--model", default="ffsft")
    ap.add_argument("--deployment", action="append", required=True, help="repeat for each")
    ap.add_argument("--max-tokens", type=int, default=128)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument(
        "--enable-thinking",
        choices=("true", "false"),
        default="false",
        help="chat_template_kwargs.enable_thinking; 'default' omits the field entirely.",
    )
    ap.add_argument("--api-key", default=None, help="Defaults to $FFSFT_ENDPOINT_KEY.")
    ap.add_argument("--output", default=None, help="Write the full JSON, replies included.")
    args = ap.parse_args()
    args.enable_thinking = {"true": True, "false": False}.get(args.enable_thinking)

    if len(args.deployment) < 2:
        print("name at least two deployments -- one alone has nothing to compare", file=sys.stderr)
        return 2
    key = args.api_key or os.environ.get("FFSFT_ENDPOINT_KEY")
    if not key:
        print("no key: pass --api-key or set FFSFT_ENDPOINT_KEY", file=sys.stderr)
        return 2

    import httpx

    rows: dict[str, list[dict]] = {}
    with httpx.Client(headers={"Authorization": f"Bearer {key}"}) as client:
        for dep in args.deployment:
            rows[dep] = [ask(client, args.base_url, dep, p, args) for p in DEFAULT_PROMPTS]

    report = {
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
        "chat_template_kwargs": (
            {"enable_thinking": args.enable_thinking} if args.enable_thinking is not None else None
        ),
        "prompts": DEFAULT_PROMPTS,
        "deployments": rows,
    }

    head = "".join(f"{d[:11]:>12} {'finish':>7} {'think':>6}" for d in args.deployment)
    print(f"{'#':>2}{head}")
    for i in range(len(DEFAULT_PROMPTS)):
        line = f"{i:>2}"
        for dep in args.deployment:
            r = rows[dep][i]
            think = "yes" if (r["thinking_in_content"] or r["reasoning_tokens"]) else "-"
            line += f"{r['completion_tokens']:>12} {r['finish_reason']:>7} {think:>6}"
        print(line)
    print()
    for dep in args.deployment:
        r = rows[dep]
        tot = sum(x["completion_tokens"] for x in r)
        cut = sum(1 for x in r if x["finish_reason"] == "length")
        print(
            f"{dep:>12}: {tot} tok total, {tot / len(r):.1f}/req, "
            f"{cut}/{len(r)} cut at max_tokens={args.max_tokens}"
        )
    if any(x["finish_reason"] == "length" for r in rows.values() for x in r):
        print(
            "\nSome replies hit the cap. Token counts are then a floor, not a length --\n"
            "raise --max-tokens before reading any difference between the columns."
        )
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1)
        print(f"\nwrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
