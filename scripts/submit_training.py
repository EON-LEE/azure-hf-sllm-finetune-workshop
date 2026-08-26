"""Submit a training or preflight job to the Azure ML cluster.

    python scripts/submit_training.py --subscription <id> --preflight
    python scripts/submit_training.py --subscription <id> --model qwen3.8-27b \
        --mix ko_smoke --max-steps 20 --max-samples 200
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ffsft.azure_ml import AzureTarget  # noqa: E402
from ffsft.train.aml_job import JobSpec, submit  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--subscription", required=True)
    ap.add_argument("--resource-group", default="rg-ffsft-kc")
    ap.add_argument("--workspace", default="mlw-ffsft")
    ap.add_argument("--location", default="koreacentral")
    ap.add_argument("--compute-name", default="gpu-a100-lp")
    ap.add_argument("--sku", default="Standard_NC24ads_A100_v4")
    ap.add_argument("--priority", default="LowPriority", choices=["LowPriority", "Dedicated"])
    ap.add_argument("--model", default="qwen3.8-27b")
    ap.add_argument("--mix", default="ko_smoke")
    ap.add_argument("--max-steps", type=int, default=-1)
    ap.add_argument("--max-samples", type=int, default=None)
    ap.add_argument("--max-seq-length", type=int, default=1024)
    ap.add_argument("--batch-size", type=int, default=1)
    ap.add_argument("--grad-accum", type=int, default=16)
    ap.add_argument("--rank", type=int, default=16)
    # `JobSpec` chains `train && eval` into one node allocation, so scoring the
    # adapter costs no second image pull and no trip through the datastore.
    # Section 23 measured the whole chain at ~$1.5 on A100 LowPriority.
    ap.add_argument(
        "--eval-suite", default=None,
        help="Score base vs tuned in the same job (e.g. ko_fast). Omit to train only.",
    )
    ap.add_argument(
        "--eval-limit", type=int, default=None,
        help="Examples per benchmark task. Without it a 27B suite is hours of GPU.",
    )
    ap.add_argument("--preflight", action="store_true", help="run the node self-test only")
    ap.add_argument(
        "--no-outputs",
        action="store_true",
        help="skip uri_folder output mounts (needed when workspace storage is network-isolated)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="submit even if workspace storage is unreachable (see --no-outputs)",
    )
    ap.add_argument("--wait", action="store_true", help="stream logs until the job ends")
    args = ap.parse_args()

    target = AzureTarget(
        subscription_id=args.subscription,
        resource_group=args.resource_group,
        workspace_name=args.workspace,
        location=args.location,
        compute_name=args.compute_name,
        compute_sku=args.sku,
        vm_priority=args.priority,
    )
    job = JobSpec(
        model_key=args.model,
        mix=args.mix,
        max_steps=args.max_steps,
        max_samples=args.max_samples,
        max_seq_length=args.max_seq_length,
        batch_size=args.batch_size,
        grad_accum=args.grad_accum,
        rank=args.rank,
        eval_suite=args.eval_suite,
        eval_limit=args.eval_limit,
        preflight=args.preflight,
        mount_outputs=not args.no_outputs,
    )

    # The deploy path has refused an unreachable storage account since section 58;
    # this path did not, and it is the path that can waste more. A training run
    # whose storage is unreachable does not fail fast: under `upload` it trains
    # for an hour and then has nowhere to put the adapter, and under `rw_mount`
    # -- which is what this script actually selects, see `mount_outputs` above --
    # it dies in the lifecycler *after* paying for node allocation and a 9 GB
    # image pull, before the command ever starts.
    #
    # `read_storage_reachability` returns None on every failure path, so an
    # unreadable subscription degrades to "not blocked" rather than to a refusal
    # that is not real.
    from ffsft.deploy.preflight import read_storage_reachability, storage_blocker

    reachability = read_storage_reachability(target)
    storage_issue = storage_blocker(reachability) if reachability else None
    if storage_issue and not args.force:
        print(storage_issue, file=sys.stderr)
        print(
            "\nThis run would allocate a node and produce nothing that outlives it.\n"
            "Fix the account, or pass --force to submit anyway.",
            file=sys.stderr,
        )
        return 1
    if storage_issue:
        print(f"submitting despite: {storage_issue}", file=sys.stderr)

    info = submit(target, job, wait=args.wait)
    print(json.dumps(info, indent=2))
    return 0 if info.get("status") in (None, "Completed", "NotStarted", "Starting",
                                       "Preparing", "Queued", "Running") else 1


if __name__ == "__main__":
    sys.exit(main())
