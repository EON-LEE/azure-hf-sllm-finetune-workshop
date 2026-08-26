"""Serve the merged model on the GPU cluster and load-test it in the same job.

    python scripts/submit_bench.py --model-asset qwen3_8-27b-ko-merged:1 --wait

This is the load test. It is not an endpoint benchmark: there is no managed
online endpoint in this subscription to point one at (docs/JOURNAL.md §40), so
the server and the client run in one command job on the A100 cluster that
training already proved works. See `ffsft.serve.bench_job` for what that keeps
and what it gives up.

Reads the target from `FFSFT_*` (see `AzureTarget.from_env`) so the tenant is
pinned the way every other entry point pins it; flags override.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ffsft.azure_ml import AzureTarget  # noqa: E402
from ffsft.serve.bench_job import BenchSpec, submit  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--subscription", default=None, help="overrides FFSFT_SUBSCRIPTION_ID")
    ap.add_argument("--resource-group", default=None)
    ap.add_argument("--workspace", default=None)
    ap.add_argument(
        "--compute-name",
        default=None,
        help="AmlCompute cluster; must have an 80 GB card for bf16 (default: FFSFT_COMPUTE)",
    )
    ap.add_argument("--sku", default=None)
    ap.add_argument("--priority", default=None, choices=["LowPriority", "Dedicated"])
    ap.add_argument("--model", default="qwen3.8-27b")
    ap.add_argument(
        "--model-asset",
        required=True,
        help="registered merged model as 'name:version' -- a bare name is refused",
    )
    ap.add_argument("--max-model-len", type=int, default=4096)
    ap.add_argument("--gpu-memory-utilization", type=float, default=0.90)
    ap.add_argument("--max-num-seqs", type=int, default=16)
    ap.add_argument("--concurrency", default="1,2,4,8,16,32")
    ap.add_argument("--requests-per-level", type=int, default=64)
    ap.add_argument("--max-tokens", type=int, default=128)
    ap.add_argument("--ttft-slo", type=float, default=1.0)
    ap.add_argument(
        "--quantization",
        default=None,
        help="omit for bf16, which is the point of running on an 80 GB card",
    )
    ap.add_argument("--startup-timeout", type=int, default=3600)
    ap.add_argument("--extra-args", default=None, help="raw vLLM flags, appended last")
    ap.add_argument("--display-name", default=None)
    ap.add_argument("--wait", action="store_true", help="stream logs until the job ends")
    args = ap.parse_args()

    target = AzureTarget.from_env()
    overrides = {
        "subscription_id": args.subscription,
        "resource_group": args.resource_group,
        "workspace_name": args.workspace,
        "compute_name": args.compute_name,
        "compute_sku": args.sku,
        "vm_priority": args.priority,
    }
    target = dataclasses.replace(
        target, **{k: v for k, v in overrides.items() if v is not None}
    )

    spec = BenchSpec(
        model_asset=args.model_asset,
        model_key=args.model,
        max_model_len=args.max_model_len,
        gpu_memory_utilization=args.gpu_memory_utilization,
        max_num_seqs=args.max_num_seqs,
        concurrency=args.concurrency,
        requests_per_level=args.requests_per_level,
        max_tokens=args.max_tokens,
        ttft_slo=args.ttft_slo,
        quantization=args.quantization,
        startup_timeout=args.startup_timeout,
        extra_args=args.extra_args,
        display_name=args.display_name,
    )

    info = submit(target, spec, wait=args.wait)
    print(json.dumps(info, indent=2, ensure_ascii=False))
    return 0 if info.get("status") in (None, "Completed", "NotStarted", "Starting",
                                       "Preparing", "Queued", "Running") else 1


if __name__ == "__main__":
    sys.exit(main())
