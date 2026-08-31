#!/usr/bin/env python3
"""Provision the Azure ML workspace and GPU cluster for a given model + method.

Nothing here is hardcoded to a model: the SKU defaults to whatever the registry
recommends, and the sizing/eligibility guard runs *before* any resource is
created, so a mis-sized or unprovisionable SKU fails in a second instead of
after a multi-minute ARM deployment.

    uv run python scripts/provision_azure.py --subscription <id> --resource-group rg-ffsft-kc
    uv run python scripts/provision_azure.py --model qwen3.5-9b --dry-run

Reads the target from `FFSFT_*` (see `AzureTarget.from_env`); flags override.
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
    # Every flag below defaults to None so that OMITTING one means "use my
    # environment", not "use the authors' resources". They used to default to
    # rg-ffsft-kc / mlw-ffsft / koreacentral as literals, which never consulted
    # `FFSFT_*` at all: a participant who had set their profile correctly and
    # omitted a flag provisioned into the authors' resource group and got a
    # permission error naming a resource they had never heard of. The defaults
    # themselves have not moved -- they live in `AzureTarget.from_env`, once.
    ap.add_argument("--subscription", default=None, help="overrides FFSFT_SUBSCRIPTION_ID")
    ap.add_argument("--resource-group", default=None, help="overrides FFSFT_RESOURCE_GROUP")
    ap.add_argument("--workspace", default=None, help="overrides FFSFT_WORKSPACE")
    ap.add_argument("--location", default=None, help="overrides FFSFT_LOCATION")
    ap.add_argument("--model", default="qwen3.8-27b")
    ap.add_argument("--method", default="qlora", choices=[m.value for m in TuningMethod])
    ap.add_argument("--compute-name", default=None, help="overrides FFSFT_COMPUTE")
    ap.add_argument("--sku", help="override the registry's recommended_sku")
    ap.add_argument("--max-nodes", type=int, default=1)
    ap.add_argument(
        "--priority",
        default=None,
        choices=["LowPriority", "Dedicated"],
        help="overrides FFSFT_VM_PRIORITY. LowPriority is the default because it is "
        "the only tier with pooled quota and the only one the tenant N-series deny "
        "policy permits",
    )
    ap.add_argument("--dry-run", action="store_true", help="run guards only, create nothing")
    args = ap.parse_args()

    spec = get_registry().get(args.model)
    method = TuningMethod(args.method)
    sku = args.sku or spec.recommended_sku
    if not sku:
        print(f"{spec.key} has no recommended_sku; pass --sku")
        return 2

    target = AzureTarget.from_env(
        subscription_id=args.subscription,
        resource_group=args.resource_group,
        workspace_name=args.workspace,
        location=args.location,
        compute_name=args.compute_name,
        # Not from the environment: this script derives the SKU from the model
        # registry and then validates it below, so FFSFT_SKU must not quietly
        # replace the SKU whose sizing was just checked.
        compute_sku=sku,
        max_nodes=args.max_nodes,
        vm_priority=args.priority,
    )
    # Read back off the target, never off `args`: after the flags stopped
    # carrying the defaults, `args.priority` is None whenever the value came
    # from FFSFT_VM_PRIORITY, and every guard below would have compared against
    # None and reported the wrong tier's quota.
    priority = target.vm_priority

    ok, why = check_sku_fits(spec, method, sku, priority)
    print(f"sizing check : {'OK' if ok else 'FAIL'} -- {why}")
    if not ok:
        usable = [
            name
            for name, info in GPU_SKUS.items()
            if (priority != "LowPriority" or info["low_priority"])
            and (spec.vram_gb.model_dump().get(method.value) or 0) <= info["vram_gb"]
        ]
        if usable:
            print(f"  SKUs that would work: {', '.join(usable)}")
        return 1

    family = GPU_SKUS.get(sku, {}).get("family")
    cores = GPU_SKUS.get(sku, {}).get("cores")
    if family:
        if priority == "LowPriority":
            print(
                f"quota needed : {cores} of the pooled TotalLowPriorityCores "
                f"in {target.location}"
            )
        else:
            print(f"quota needed : {cores} vCPU of {family} in {target.location}")

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
