"""Bring the serving stack up, and -- more importantly -- take it back down.

Managed online endpoints have no scale-to-zero. An `NV36ads_A10_v5` deployment
left running bills 24 hours a day at $4.320/hr PAYG, which is **$103/day** and
about **$3,150/month**, whether or not a single request arrives. That is the
single largest cost risk in this repo, and forgetting one is easy because the
endpoint is invisible unless you go looking for it.

So teardown is treated as a first-class, scriptable operation rather than a
manual portal chore, and `status` is written to be run casually and often.

The asymmetry between resources is deliberate and worth internalising:

    resource                     idle cost    teardown needed?
    managed online endpoint      FULL RATE    YES -- always
    batch endpoint               none         no (its cluster scales to 0)
    AmlCompute min_instances=0   none         no
    AmlCompute min_instances>0   FULL RATE    yes
    ACR image storage            ~$0.10/GB/mo optional
    registered models in blob    ~$0.02/GB/mo optional

`up` and `down` are inverses on purpose: an experiment is meant to be resumed by
re-running the same `up` command later, so nothing that is expensive to rebuild
(the ACR image, registered models, the training cluster definition) is destroyed
by `down`. Only the metered compute goes away.

    python -m ffsft.deploy.lifecycle status
    python -m ffsft.deploy.lifecycle up   --endpoint ffsft-qwen --model-uri azureml:qwen-ko:1
    python -m ffsft.deploy.lifecycle down --endpoint ffsft-qwen
    python -m ffsft.deploy.lifecycle down --all --yes
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field

log = logging.getLogger("ffsft.deploy.lifecycle")

#: Measured Azure Retail Prices for koreacentral, Linux, USD/hr (2026-08).
#: Used to turn "you have an endpoint running" into a number that prompts action.
SKU_HOURLY_PAYG = {
    "Standard_NC16as_T4_v3": 1.481,
    "Standard_NV18ads_A10_v5": 2.160,
    "Standard_NV36ads_A10_v5": 4.320,
    "Standard_NC24ads_A100_v4": 4.959,
    "Standard_NC40ads_H100_v5": 9.423,
}

HOURS_PER_MONTH = 730


def hourly_rate(sku: str) -> float:
    """Best-effort PAYG rate. Unknown SKUs return 0.0 and are reported as such."""
    return SKU_HOURLY_PAYG.get(sku, 0.0)


@dataclass
class BillingItem:
    """One resource that may be costing money right now."""

    kind: str
    name: str
    detail: str
    sku: str = ""
    instances: int = 0
    #: True when the resource bills while completely idle. These are what
    #: `down` exists for; everything else is noise in the report.
    bills_when_idle: bool = False

    @property
    def hourly(self) -> float:
        if not self.bills_when_idle:
            return 0.0
        return hourly_rate(self.sku) * max(self.instances, 0)

    @property
    def monthly(self) -> float:
        return self.hourly * HOURS_PER_MONTH


@dataclass
class Inventory:
    items: list[BillingItem] = field(default_factory=list)

    @property
    def billing(self) -> list[BillingItem]:
        return [i for i in self.items if i.bills_when_idle]

    @property
    def hourly(self) -> float:
        return sum(i.hourly for i in self.items)

    @property
    def monthly(self) -> float:
        return self.hourly * HOURS_PER_MONTH


def collect_inventory(client) -> Inventory:
    """Walk the workspace and classify everything that could be metered.

    Each section is wrapped: a missing permission or an unsupported API on one
    resource type must not stop the report, because a partial cost report is far
    more useful than a traceback when you are trying to stop the meter.
    """
    inv = Inventory()

    # Online endpoints first -- these are the ones that bill 24/7.
    try:
        for endpoint in client.online_endpoints.list():
            deployments = []
            try:
                deployments = list(client.online_deployments.list(endpoint.name))
            except Exception as exc:  # noqa: BLE001 - report, never abort
                log.warning("could not list deployments of %s: %s", endpoint.name, exc)
            if not deployments:
                inv.items.append(
                    BillingItem(
                        kind="online-endpoint",
                        name=endpoint.name,
                        detail="no deployments (endpoint shell only, no compute cost)",
                    )
                )
                continue
            for dep in deployments:
                inv.items.append(
                    BillingItem(
                        kind="online-deployment",
                        name=f"{endpoint.name}/{dep.name}",
                        detail="managed online endpoint: NO scale-to-zero, bills 24/7",
                        sku=getattr(dep, "instance_type", "") or "",
                        instances=int(getattr(dep, "instance_count", 0) or 0),
                        bills_when_idle=True,
                    )
                )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not list online endpoints: %s", exc)

    try:
        for endpoint in client.batch_endpoints.list():
            inv.items.append(
                BillingItem(
                    kind="batch-endpoint",
                    name=endpoint.name,
                    detail="runs on AmlCompute; scales to 0 between jobs",
                )
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not list batch endpoints: %s", exc)

    # A cluster with min_instances=0 costs nothing idle, so it is reported but
    # not flagged. One with min_instances>0 is a silent, permanent charge.
    try:
        for compute in client.compute.list():
            if getattr(compute, "type", "") != "amlcompute":
                continue
            min_i = int(getattr(compute, "min_instances", 0) or 0)
            sku = getattr(compute, "size", "") or ""
            priority = (getattr(compute, "tier", "") or "dedicated").lower()
            if min_i > 0:
                inv.items.append(
                    BillingItem(
                        kind="compute-cluster",
                        name=compute.name,
                        detail=f"min_instances={min_i} ({priority}): always-on charge",
                        sku=sku,
                        instances=min_i,
                        bills_when_idle=True,
                    )
                )
            else:
                inv.items.append(
                    BillingItem(
                        kind="compute-cluster",
                        name=compute.name,
                        detail=f"min_instances=0 ({priority}): idle costs nothing",
                        sku=sku,
                    )
                )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not list compute: %s", exc)

    # Running jobs are transient, but a hung job on a GPU node bills like an
    # endpoint, so surface them even though `down` will not touch them.
    try:
        active = [
            j
            for j in client.jobs.list(max_results=50)
            if getattr(j, "status", "") in {"Running", "Preparing", "Queued", "Starting"}
        ]
        for job in active:
            inv.items.append(
                BillingItem(
                    kind="job",
                    name=getattr(job, "name", "?"),
                    detail=f"status={getattr(job, 'status', '?')}: consuming cluster nodes",
                )
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not list jobs: %s", exc)

    return inv


def format_inventory(inv: Inventory) -> str:
    lines = [
        "",
        f"{'KIND':<20} {'NAME':<34} {'SKU':<26} {'$/hr':>8}  NOTE",
        "-" * 132,
    ]
    for item in sorted(inv.items, key=lambda i: (not i.bills_when_idle, i.kind)):
        marker = "!!" if item.bills_when_idle else "  "
        rate = f"{item.hourly:.3f}" if item.hourly else "-"
        lines.append(
            f"{marker}{item.kind:<18} {item.name:<34} {item.sku:<26} {rate:>8}  {item.detail}"
        )
    lines.append("-" * 132)
    if inv.billing:
        lines.append(
            f"BILLING NOW: {len(inv.billing)} resource(s)  "
            f"${inv.hourly:.3f}/hr  ~${inv.monthly:,.0f}/month if left running"
        )
        lines.append("Run `ffsft lifecycle down --all --yes` to stop the meter.")
    else:
        lines.append("BILLING NOW: nothing. No always-on compute in this workspace.")
    return "\n".join(lines)


def teardown(client, inv: Inventory, *, dry_run: bool = True) -> list[str]:
    """Delete every always-on resource. Returns what was (or would be) removed.

    Only metered compute is destroyed. Registered models, the ACR image and
    cluster definitions survive so that `up` can rebuild the same experiment
    without another 25-minute image build.
    """
    removed: list[str] = []
    #: Several deployments can share one endpoint, and deleting the endpoint
    #: takes all of them with it. Track what has been handled so a two-deployment
    #: endpoint is not deleted twice -- the second call fails on a missing
    #: resource and aborts the rest of the teardown, leaving other GPUs running.
    handled: set[str] = set()

    for item in inv.billing:
        if item.kind == "online-deployment":
            endpoint_name = item.name.split("/")[0]
            if endpoint_name in handled:
                continue
            handled.add(endpoint_name)
            removed.append(f"online-endpoint {endpoint_name} (with its deployments)")
            if not dry_run:
                log.info("deleting online endpoint %s", endpoint_name)
                # Deleting the endpoint removes its deployments too; deleting
                # them individually first would just be slower.
                client.online_endpoints.begin_delete(name=endpoint_name).result()
        elif item.kind == "compute-cluster":
            if item.name in handled:
                continue
            handled.add(item.name)
            removed.append(f"compute {item.name} -> min_instances=0 (kept, scaled down)")
            if not dry_run:
                log.info("scaling %s to min_instances=0", item.name)
                # Scale rather than delete: the cluster definition is cheap to
                # keep and re-creating it costs minutes on the next experiment.
                compute = client.compute.get(item.name)
                compute.min_instances = 0
                client.compute.begin_create_or_update(compute).result()

    return removed


def cmd_status(args) -> int:
    from ffsft.azure_ml import AzureTarget, get_ml_client

    client = get_ml_client(AzureTarget.from_env())
    inv = collect_inventory(client)
    print(format_inventory(inv))
    return 0


def cmd_down(args) -> int:
    from ffsft.azure_ml import AzureTarget, get_ml_client

    client = get_ml_client(AzureTarget.from_env())
    inv = collect_inventory(client)

    if args.endpoint:
        inv = Inventory(
            items=[
                i
                for i in inv.items
                if i.kind == "online-deployment" and i.name.startswith(f"{args.endpoint}/")
            ]
        )
        if not inv.items:
            print(f"no online deployment found for endpoint '{args.endpoint}'")
            # Delete the endpoint shell anyway: an endpoint whose deployment
            # failed to create still exists and still blocks the name.
            if args.yes:
                print(f"deleting endpoint shell '{args.endpoint}'")
                client.online_endpoints.begin_delete(name=args.endpoint).result()
                print("deleted")
            return 0

    if not inv.billing:
        print(format_inventory(inv))
        return 0

    planned = teardown(client, inv, dry_run=True)
    print("\nwill remove:")
    for entry in planned:
        print(f"  - {entry}")
    print(f"\nstops ${inv.hourly:.3f}/hr (~${inv.monthly:,.0f}/month)")

    if not args.yes:
        print("\ndry run. re-run with --yes to actually delete.")
        return 0

    done = teardown(client, inv, dry_run=False)
    print("\nremoved:")
    for entry in done:
        print(f"  - {entry}")
    print("\nmeter stopped. `ffsft lifecycle status` to confirm.")
    return 0


def cmd_up(args) -> int:
    from .endpoint import deploy_online

    # Resolving the registry key here is what makes the model swappable end to
    # end: the spec carries the architecture flags vLLM needs, so `--model
    # qwen3.8-27b` and `--model kanana2-3b` produce different launch arguments
    # without anyone editing the image or the deploy code.
    spec = None
    if args.model:
        from ..models.registry import get_model

        spec = get_model(args.model)
        print(f"model spec: {spec.key} ({spec.hf_id}) params={spec.params_b}B")

    scoring_uri = deploy_online(
        args.endpoint,
        args.model_uri,
        pattern_key=args.pattern,
        instance_count=args.instances,
        sku=args.sku,
        max_model_len=args.max_model_len,
        hf_model=args.hf_model or (spec.hf_id if spec and not args.model_uri else None),
        model_spec=spec,
        params_b=args.params_b,
        quantization=args.quantization,
    )
    print(f"\nendpoint '{args.endpoint}' is up")
    print(f"scoring uri: {scoring_uri}")
    rate = hourly_rate(args.sku or "")
    if rate:
        print(f"billing ${rate:.3f}/hr -> ~${rate * HOURS_PER_MONTH:,.0f}/month if left up")
    print(f"tear down with: ffsft lifecycle down --endpoint {args.endpoint} --yes")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="serving lifecycle: up / down / status")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="what is billing right now")
    p_status.set_defaults(func=cmd_status)

    p_up = sub.add_parser("up", help="create an online endpoint")
    p_up.add_argument("--endpoint", required=True)
    p_up.add_argument("--model", default=None, help="registry key, e.g. qwen3.8-27b")
    p_up.add_argument("--model-uri", default=None, help="registered azureml: model")
    p_up.add_argument("--hf-model", default=None, help="Hugging Face repo id instead")
    p_up.add_argument(
        "--params-b",
        type=float,
        default=None,
        help="parameter count in billions; sizes the startup probe when the "
        "repo id does not say and the model is not in the registry",
    )
    p_up.add_argument("--pattern", default="aml_online_vllm")
    p_up.add_argument("--sku", default=None)
    p_up.add_argument("--instances", type=int, default=1)
    p_up.add_argument("--max-model-len", type=int, default=4096)
    p_up.add_argument("--quantization", default=None)
    p_up.set_defaults(func=cmd_up)

    p_down = sub.add_parser("down", help="delete always-on compute")
    p_down.add_argument("--endpoint", default=None, help="only this endpoint")
    p_down.add_argument("--all", action="store_true", help="everything billing")
    p_down.add_argument("--yes", action="store_true", help="actually delete")
    p_down.set_defaults(func=cmd_down)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
