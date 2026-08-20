#!/usr/bin/env python3
"""Provision the Azure ML workspace and GPU cluster for a given model + method.

Nothing here is hardcoded to a model: the SKU defaults to whatever the registry
recommends, and the sizing/eligibility guard runs *before* any resource is
created, so a mis-sized or unprovisionable SKU fails in a second instead of
after a multi-minute ARM deployment.

    uv run python scripts/provision_azure.py --subscription <id> --resource-group rg-ffsft-kc
    uv run python scripts/provision_azure.py --model qwen3.5-9b --dry-run

Requires the `azure` extra and an `az login` session.
"""

from __future__ import annotations

import argparse
import sys

from ffsft.azure_ml import (
    GPU_SKUS,
    AzureTarget,
    check_sku_fits,
    ensure_compute,
    ensure_workspace,
)
from ffsft.models import TuningMethod, get_registry


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--subscription", required=True)
    ap.add_argument("--resource-group", default="rg-ffsft-kc")
    ap.add_argument("--workspace", default="mlw-ffsft")
    ap.add_argument("--location", default="koreacentral")
    ap.add_argument("--model", default="qwen3.8-27b")
    ap.add_argument("--method", default="qlora", choices=[m.value for m in TuningMethod])
    ap.add_argument("--compute-name", default="gpu-a100-lp")
    ap.add_argument("--sku", help="override the registry's recommended_sku")
    ap.add_argument("--max-nodes", type=int, default=1)
    ap.add_argument(
        "--priority",
        default="LowPriority",
        choices=["LowPriority", "Dedicated"],
        help="LowPriority is the default because it is the only tier with pooled "
        "quota and the only one the tenant N-series deny policy permits",
    )
    ap.add_argument("--dry-run", action="store_true", help="run guards only, create nothing")
    args = ap.parse_args()

    spec = get_registry().get(args.model)
    method = TuningMethod(args.method)
    sku = args.sku or spec.recommended_sku
    if not sku:
        print(f"{spec.key} has no recommended_sku; pass --sku")
        return 2

    target = AzureTarget(
        subscription_id=args.subscription,
        resource_group=args.resource_group,
        workspace_name=args.workspace,
        location=args.location,
        compute_name=args.compute_name,
        compute_sku=sku,
        max_nodes=args.max_nodes,
        vm_priority=args.priority,
    )

    ok, why = check_sku_fits(spec, method, sku, args.priority)
    print(f"sizing check : {'OK' if ok else 'FAIL'} -- {why}")
    if not ok:
        usable = [
            name
            for name, info in GPU_SKUS.items()
            if (args.priority != "LowPriority" or info["low_priority"])
            and (spec.vram_gb.model_dump().get(method.value) or 0) <= info["vram_gb"]
        ]
        if usable:
            print(f"  SKUs that would work: {', '.join(usable)}")
        return 1

    family = GPU_SKUS.get(sku, {}).get("family")
    cores = GPU_SKUS.get(sku, {}).get("cores")
    if family:
        if args.priority == "LowPriority":
            print(f"quota needed : {cores} of the pooled TotalLowPriorityCores in {args.location}")
        else:
            print(f"quota needed : {cores} vCPU of {family} in {args.location}")

    if args.dry_run:
        print("\ndry run, nothing created")
        return 0

    print(f"\nworkspace    : creating {target.workspace_name} in {target.location} ...")
    print(f"  -> {ensure_workspace(target)}")

    print(
        f"\ncompute      : creating {target.compute_name} "
        f"({sku}, min=0 max={target.max_nodes}) ..."
    )
    print(f"  -> {ensure_compute(target)}")

    print("\nDONE. The cluster scales to zero when idle, but verify in the portal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
