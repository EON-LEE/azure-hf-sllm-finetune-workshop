"""Read one chat completion and say what it proves about the deployment.

A 200 is not evidence that a deployment is serving correctly. Three things go
wrong while the status code stays 200, and all three are invisible unless
something looks at the body:

* **The reasoning trace leaks into the answer.** Without `--reasoning-parser`,
  vLLM leaves the Qwen3 `<think>` block inside `message.content`, so every
  caller downstream gets the model's scratch work as the reply. The deployment
  is up and has not done the thing it exists to do.
* **The answer was truncated before it started.** With thinking on, the model
  can spend thousands of tokens before emitting a single character of answer --
  4908 completion tokens on one measured hard question. If `max_tokens` runs
  out first, `finish_reason` is `length` and `content` is empty, which reads
  exactly like a model that returned nothing.
* **The thinking arrives under a field nobody read.** The image serving this
  asset streams and returns the block as `reasoning`. An earlier version of
  this check read only `reasoning_content`, saw an empty string, and reported a
  model that had in fact produced 1737 characters of reasoning. Both spellings
  are read here, and which one arrived is part of the output, because that is
  the fact that was expensive to learn.

Usage: pipe a chat-completions response body in.

    curl ... | python -m ffsft.serve.smoke --max-tokens 400
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys

#: Both spellings vLLM has used for the thinking block, newest first.
THINKING_FIELDS = ("reasoning", "reasoning_content")


@dataclasses.dataclass(frozen=True)
class Reply:
    """What a single non-streaming chat completion actually contained."""

    content: str
    thinking: str
    thinking_field: str | None
    finish_reason: str | None
    completion_tokens: int | None
    trace_leaked: bool

    @property
    def ok(self) -> bool:
        """Whether this reply is evidence the deployment serves correctly.

        Empty content is a failure even at `finish_reason == "stop"`: a reply
        with nothing in it is not a reply. Truncation is a failure too, but it
        is the caller's budget that is wrong, not the deployment -- `summary`
        says which.
        """
        return bool(self.content.strip()) and not self.trace_leaked

    def summary(self) -> str:
        lines = [
            f"  content            : {len(self.content)} chars",
            f"  thinking           : {len(self.thinking)} chars"
            + (f" (field: {self.thinking_field})" if self.thinking_field else ""),
            f"  finish_reason      : {self.finish_reason}",
            f"  completion_tokens  : {self.completion_tokens}",
            f"  trace in content   : {self.trace_leaked}",
            f"  content head       : {' '.join(self.content.split())[:160]}",
        ]
        if self.trace_leaked:
            lines.append(
                "  -> the reasoning parser is not configured: set REASONING_PARSER "
                "(qwen3) on the deployment, or callers receive the scratch work"
            )
        elif self.finish_reason == "length" and not self.content.strip():
            lines.append(
                "  -> truncated before the answer began. Thinking consumed the "
                "whole budget; raise max_tokens or turn thinking off"
            )
        elif not self.content.strip():
            lines.append("  -> empty reply")
        return "\n".join(lines)


def parse_reply(body: dict) -> Reply:
    """Pull the facts out of a chat-completions response body."""
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}

    content = message.get("content") or ""
    thinking, field = "", None
    for name in THINKING_FIELDS:
        value = message.get(name)
        if value:
            thinking, field = value, name
            break

    return Reply(
        content=content,
        thinking=thinking,
        thinking_field=field,
        finish_reason=choice.get("finish_reason"),
        completion_tokens=(body.get("usage") or {}).get("completion_tokens"),
        # A closing tag is the reliable marker. An opening one can legitimately
        # be absent when the template pre-closes an empty block.
        trace_leaked="</think>" in content or "<think>" in content,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--max-tokens", type=int, default=None, help="What was requested, for context.")
    args = ap.parse_args(argv)

    raw = sys.stdin.read()
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"  unparseable response: {exc}")
        print(f"  raw head: {raw[:400]}")
        return 2
    if "error" in body and "choices" not in body:
        print(f"  server error: {json.dumps(body['error'])[:400]}")
        return 2

    reply = parse_reply(body)
    print(reply.summary())
    if args.max_tokens and reply.completion_tokens:
        pct = 100.0 * reply.completion_tokens / args.max_tokens
        print(f"  budget used        : {reply.completion_tokens}/{args.max_tokens} ({pct:.0f}%)")
    return 0 if reply.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
