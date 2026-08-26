"""Submit a LoRA merge job to the Azure ML cluster.

    python scripts/submit_merge.py --model qwen3.8-27b --adapter qwen3_8-27b-ko-lora:1

Reads the target from `FFSFT_*` (see `AzureTarget.from_env`) so the tenant is
pinned the same way every other entry point pins it; flags override. That
matters more here than it looks: a workstation signed in to two directories can
have the Azure CLI's default move between two calls in one session, and the
failure reads as a permissions problem rather than a wrong-directory one.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ffsft.azure_ml import AzureTarget  # noqa: E402
from ffsft.deploy.merge_job import MergeSpec, submit  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--subscription", default=None, help="overrides FFSFT_SUBSCRIPTION_ID")
    ap.add_argument("--resource-group", default=None)
    ap.add_argument("--workspace", default=None)
    ap.add_argument("--compute-name", default=None)
    ap.add_argument("--sku", default=None)
    ap.add_argument("--priority", default=None, choices=["LowPriority", "Dedicated"])
    ap.add_argument("--model", default="qwen3.8-27b")
    ap.add_argument(
        "--adapter",
        required=True,
        help="registered adapter asset as 'name:version' -- a bare name is refused",
    )
    ap.add_argument("--dtype", default="bfloat16", choices=["bfloat16", "float16", "float32"])
    ap.add_argument(
        "--device-map",
        default="auto",
        help="'auto' spills a 27B across the GPU and host RAM; 'cpu' avoids the GPU entirely",
    )
    ap.add_argument("--max-shard-size", default="4GB")
    ap.add_argument("--display-name", default=None)
    ap.add_argument("--wait", action="store_true", help="stream logs until the job ends")
    ap.add_argument(
        "--force",
        action="store_true",
        help="submit even if workspace storage is unreachable",
    )
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

    spec = MergeSpec(
        model_key=args.model,
        adapter=args.adapter,
        dtype=args.dtype,
        device_map=args.device_map,
        max_shard_size=args.max_shard_size,
        display_name=args.display_name,
    )

    # The merge is the job that mounts. It reads the adapter the trainer wrote and
    # writes the merged weights back, so a storage account that refuses its own
    # datastores' credential kills it -- and kills it as a bare "Failed to mount
    # URI ... at mount point ...", with the real reason nowhere in the message.
    # That cost three jobs and an A/B before the axis was found (docs/JOURNAL.md
    # S63). `submit_training.py` already ran this check; this path did not, which
    # is exactly why the check did not fire.
    from ffsft.deploy.preflight import read_storage_reachability, storage_blocker

    reachability = read_storage_reachability(target)
    storage_issue = storage_blocker(reachability) if reachability else None
    if storage_issue and not args.force:
        print(storage_issue, file=sys.stderr)
        print(
            "\nThe merge would allocate a node, fail to mount the adapter, and retry\n"
            "until it gives up. Fix the account, or pass --force to submit anyway.",
            file=sys.stderr,
        )
        return 1
    if storage_issue:
        print(f"submitting despite: {storage_issue}", file=sys.stderr)

    info = submit(target, spec, wait=args.wait)
    print(json.dumps(info, indent=2))
    return 0 if info.get("status") in (None, "Completed", "NotStarted", "Starting",
                                       "Preparing", "Queued", "Running") else 1


if __name__ == "__main__":
    sys.exit(main())
