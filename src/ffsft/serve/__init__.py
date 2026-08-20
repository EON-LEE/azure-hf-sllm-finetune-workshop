"""Client-side tooling for a served model: load testing and smoke calls.

Everything here talks the OpenAI chat-completions wire protocol, so it is
independent of which serving pattern in `configs/serving.yaml` is behind the
URL -- local vLLM, an Azure ML managed online endpoint, AKS or Foundry.
"""

from .loadtest import (
    LevelResult,
    RequestResult,
    find_knee,
    format_table,
    run_level,
    summarize,
    sweep,
)

__all__ = [
    "LevelResult",
    "RequestResult",
    "find_knee",
    "format_table",
    "run_level",
    "summarize",
    "sweep",
]
