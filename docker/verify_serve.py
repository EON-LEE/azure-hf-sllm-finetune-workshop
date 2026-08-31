"""Build-time gate for the serving image.

The training image has docker/verify_stack.py for the same reason this exists:
a 20-minute GPU deployment that fails on an import or an unknown architecture is
a terrible way to discover a packaging mistake. Everything here runs on CPU at
build time and costs seconds.

The architecture check is the important one. Qwen3.8-27B is served by vLLM's
`Qwen3_5ForConditionalGeneration`, which only entered the registry in vLLM
v0.27.0. If someone rebases this image onto an older vLLM tag, the model becomes
unloadable, and without this check the first sign of that is a failed rollout.
"""

from __future__ import annotations

import sys
from pathlib import Path

REQUIRED_ARCHITECTURES = [
    # Qwen3.5 / Qwen3.8 hybrid linear-attention family, including Qwen3.8-27B.
    "Qwen3_5ForConditionalGeneration",
]

MIN_VLLM = (0, 27, 0)

# Flags docker/serve_entrypoint.sh passes to the OpenAI server. vLLM renames and
# retires CLI flags between minor releases (--disable-log-requests was inverted
# to --enable-log-requests, for one), and an unknown flag makes the server exit
# non-zero at startup. On a managed online endpoint that surfaces ~20 minutes
# later as an unhealthy rollout with little explanation, so assert them here
# while it costs a second.
REQUIRED_FLAGS = [
    "--model",
    "--served-model-name",
    "--max-model-len",
    "--gpu-memory-utilization",
    "--tensor-parallel-size",
    "--mamba-cache-mode",
    "--reasoning-parser",
    "--trust-remote-code",
    "--enable-lora",
    "--max-lora-rank",
    "--lora-modules",
    "--quantization",
]

# Passed only when LANGUAGE_MODEL_ONLY=1. Qwen3.8-27B is multimodal and this
# skips the vision tower. If a future vLLM drops the flag the image should still
# build, because the entrypoint can serve the model without it -- just report it
# so the default in Dockerfile.serve can be flipped.
OPTIONAL_FLAGS = [
    "--language-model-only",
]


def parse_version(raw: str) -> tuple[int, ...]:
    core = raw.split("+")[0].split("rc")[0].lstrip("v")
    parts = []
    for chunk in core.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts[:3])


def main() -> int:
    import torch
    import vllm

    print(f"vllm  {vllm.__version__}")
    print(f"torch {torch.__version__}")

    version = parse_version(vllm.__version__)
    if version < MIN_VLLM:
        print(
            f"FAIL: vllm {vllm.__version__} predates "
            f"{'.'.join(map(str, MIN_VLLM))}, which is the first release that "
            f"registers Qwen3_5ForConditionalGeneration.",
            file=sys.stderr,
        )
        return 1

    from vllm.model_executor.models.registry import ModelRegistry

    known = set(ModelRegistry.get_supported_archs())
    missing = [a for a in REQUIRED_ARCHITECTURES if a not in known]
    if missing:
        print(f"FAIL: vllm does not register {missing}", file=sys.stderr)
        print(f"      registry holds {len(known)} architectures", file=sys.stderr)
        return 1

    for arch in REQUIRED_ARCHITECTURES:
        print(f"arch  {arch}: registered")

    # The OpenAI server module is what the entrypoint execs; importing it here
    # turns a typo'd module path into a build failure instead of a crash loop.
    import vllm.entrypoints.openai.api_server  # noqa: F401

    print("openai api_server: importable")

    # Ideally we would build the real CLI parser and inspect it. We cannot:
    # make_arg_parser() constructs a VllmConfig, whose DeviceConfig raises
    # "Failed to infer device type" on the CPU-only ACR build agent. So verify
    # the flags by scanning vLLM's own sources instead.
    #
    # vLLM derives most server flags from dataclass fields, so "--mamba-cache-mode"
    # appears in the package as the field `mamba_cache_mode`. Accept either form.
    import vllm

    package_root = Path(vllm.__file__).parent
    corpus = []
    unreadable: list[str] = []
    for path in package_root.rglob("*.py"):
        try:
            corpus.append(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError as exc:
            # A file we could not open is a file whose flags we did not see, and
            # `flag_present` below states a NEGATIVE over this corpus. The
            # direction is safe -- an unread file makes a flag look absent, so
            # the build fails rather than shipping an image whose entrypoint
            # passes an unknown flag -- but "scanned N" on its own is an
            # omission claim, and the wrong build to debug is the one that
            # failed for a reason nothing printed. Reported in three rounds and
            # fixed in none, because this file sits outside the `_SRC` root the
            # except-handler guard walks; that root now includes `docker/`.
            unreadable.append(f"{path.relative_to(package_root)} ({type(exc).__name__})")
    blob = "\n".join(corpus)
    if unreadable:
        shown = ", ".join(unreadable[:5])
        more = f", and {len(unreadable) - 5} more" if len(unreadable) > 5 else ""
        print(
            f"scanned {len(corpus)} vllm source files; "
            f"{len(unreadable)} could NOT be read: {shown}{more}"
        )
        print(
            "      the flag check below is a negative over the files that WERE "
            "read, so it is incomplete by that many.",
            file=sys.stderr,
        )
    else:
        print(f"scanned {len(corpus)} vllm source files, 0 unreadable")

    def flag_present(flag: str) -> bool:
        return flag in blob or flag.lstrip("-").replace("-", "_") in blob

    unknown = [f for f in REQUIRED_FLAGS if not flag_present(f)]
    if unknown:
        print(f"FAIL: vllm sources mention none of {unknown}", file=sys.stderr)
        print(
            "      docker/serve_entrypoint.sh passes these; fix it or pin a "
            "different VLLM_TAG.",
            file=sys.stderr,
        )
        return 1
    print(f"flags {len(REQUIRED_FLAGS)} required: all present in vllm sources")

    for flag in OPTIONAL_FLAGS:
        state = "present" if flag_present(flag) else "NOT AVAILABLE"
        print(f"flags {flag}: {state}")

    print("serving stack OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
