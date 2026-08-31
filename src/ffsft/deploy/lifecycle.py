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
    python -m ffsft.deploy.lifecycle down --endpoint ffsft-qwen --deployment blue --yes
    python -m ffsft.deploy.lifecycle down --all --yes
"""

from __future__ import annotations

import argparse
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from ffsft.logging_setup import quiet_azure_sdk_logs

from .preflight import AML_CLIENT_SCOPE, UNIDENTIFIED_SCOPE, scope_lines  # noqa: F401

if TYPE_CHECKING:  # annotation only -- the Azure-facing import stays function-local
    from ffsft.azure_ml import AzureTarget

log = logging.getLogger("ffsft.deploy.lifecycle")

#: Measured Azure Retail Prices for koreacentral, Linux, USD/hr (2026-08).
#: Used to turn "you have an endpoint running" into a number that prompts action.
#:
#: PAYG only, and that is a correctness constraint rather than a scope note: this
#: table prices *managed online endpoints*, which cannot use LowPriority at all
#: (see CLAUDE.md) and are not Spot. Filing the cheaper tier here would under-report
#: the one resource in this repo that bills 24/7 by a factor of five.
#:
#: The 2026-08-27 re-query of the Retail Prices API reproduced the original five to
#: the digit, so none of them were touched; the rest are additions. Three row-selection
#: traps make a plausible wrong number easy here, and every value below survived all
#: three: `type` must be exactly `Consumption` (Reservation rows carry unitOfMeasure
#: "1 Hour" but hold a 1yr/3yr total -- ND96isr reads 744149.0), DevTestConsumption
#: rows collide numerically with the Linux price on some SKUs and not others, and the
#: Linux rows for the NV/NC-T4/NC-H100/ND series do not contain the word "Linux" at
#: all, so filtering *for* it silently returns nothing for 13 of 16 SKUs.
SKU_HOURLY_PAYG = {
    # T4
    "Standard_NC4as_T4_v3": 0.647,
    "Standard_NC8as_T4_v3": 0.925,
    "Standard_NC16as_T4_v3": 1.481,
    "Standard_NC64as_T4_v3": 5.353,
    # A10 -- Standard_NV6ads_A10_v5 is the SKU that was observed live reporting
    # "$0.000/hr" because it was missing from this table.
    "Standard_NV6ads_A10_v5": 0.613,
    "Standard_NV12ads_A10_v5": 1.226,
    "Standard_NV18ads_A10_v5": 2.160,
    "Standard_NV36ads_A10_v5": 4.320,
    "Standard_NV36adms_A10_v5": 6.102,
    "Standard_NV72ads_A10_v5": 8.802,
    # A100
    "Standard_NC24ads_A100_v4": 4.959,
    "Standard_NC48ads_A100_v4": 9.917,
    "Standard_NC96ads_A100_v4": 19.834,
    # H100 / ND
    "Standard_NC40ads_H100_v5": 9.423,
    "Standard_NC80adis_H100_v5": 18.846,
    "Standard_ND96isr_H100_v5": 132.732,
}

HOURS_PER_MONTH = 730

#: Premium SSD managed disks bill per *tier*, not per byte -- a 200 GB disk and
#: a 256 GB disk both cost P15. Prices are USD/month for koreacentral, taken
#: from the Azure Retail Prices API (`type: Consumption`; the Reservation rows
#: for the same SKU are ~20x higher and must not be used here).
PREMIUM_DISK_TIERS_USD = [
    (4, 0.81),  # P1
    (8, 1.62),  # P2
    (16, 3.24),  # P3
    (32, 5.2795),  # P4
    (64, 10.207),  # P6
    (128, 19.71),  # P10
    (256, 38.012142),  # P15
    (512, 73.22),  # P20
    (1024, 135.17),  # P30
]

#: USD/hour, Azure Retail Prices API, koreacentral.
PUBLIC_IP_HOURLY_USD = {"Standard": 0.005, "Basic": 0.0036, "Global": 0.01}


def disk_monthly_usd(size_gb: int, sku: str) -> float:
    """Monthly cost of a managed disk, or 0.0 when we genuinely do not know.

    Only Premium_LRS is priced. That is not laziness: it is what AML computes
    and GPU VMs provision by default, and it is what actually leaked here.
    Returning 0.0 for anything else is deliberate -- a made-up number in a cost
    report is worse than an admitted gap, because it gets believed.
    """
    if not str(sku).lower().startswith("premium"):
        return 0.0
    for tier_gb, price in PREMIUM_DISK_TIERS_USD:
        if size_gb <= tier_gb:
            return price
    return 0.0


def public_ip_monthly_usd(sku: str) -> float:
    return PUBLIC_IP_HOURLY_USD.get(sku, 0.0) * HOURS_PER_MONTH


def hourly_rate(sku: str) -> float:
    """Best-effort PAYG rate. Unknown SKUs return 0.0 and are reported as such.

    Callers that render money must ask :func:`rate_is_known` first. This
    returning 0.0 is what let `status` print "BILLING NOW: 1 resource(s)
    $0.000/hr ~$0/month if left running" over a live Standard_NV6ads_A10_v5.
    """
    return SKU_HOURLY_PAYG.get(sku, 0.0)


def rate_is_known(sku: str) -> bool:
    """Whether a price for this SKU exists at all -- 0.0 answers two questions.

    Same honesty rule the disk path has always followed (see
    :func:`disk_monthly_usd`): a made-up number in a cost report is worse than an
    admitted gap, because it gets believed. $0.000/hr next to an A10 reads as
    "safe to leave up" and is the most expensive sentence this tool can print.
    """
    return sku in SKU_HOURLY_PAYG


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
    #: Set for resources Azure meters per month rather than per compute-hour
    #: (managed disks, public IPs). When present it wins, because deriving a
    #: monthly figure back out of a fake hourly rate only loses precision.
    monthly_usd: float = 0.0
    #: True when the row is here *because* a listing failed, so what it costs was
    #: never read. Not the same gap as a SKU missing from `SKU_HOURLY_PAYG`: there
    #: is no SKU here to look up. Without it `rate_known` answered "known" off
    #: `bills_when_idle` being False, and the dark-endpoint row printed the free
    #: glyph over an endpoint that may be serving an A100 at $4.959/hr.
    cost_unknown: bool = False

    @property
    def hourly(self) -> float:
        if not self.bills_when_idle:
            return 0.0
        if self.monthly_usd:
            return self.monthly_usd / HOURS_PER_MONTH
        return hourly_rate(self.sku) * max(self.instances, 0)

    @property
    def monthly(self) -> float:
        return self.hourly * HOURS_PER_MONTH

    @property
    def rate_known(self) -> bool:
        """Whether this row's cost can be stated at all.

        A resource that does not bill when idle is genuinely free, so "known"
        is the honest answer for it -- unless `cost_unknown` says the row exists
        because a listing failed, in which case "does not bill when idle" was
        never read either and answering "free" is the whole bug. For the rest,
        a per-month price (disk, public IP) already went through the
        `disk_monthly_usd` / `public_ip_monthly_usd` gap check and only lands
        here non-zero when it is real; everything else is priced per compute
        hour off the SKU, and the SKU either has a rate or it does not.
        """
        if self.cost_unknown:
            return False
        if not self.bills_when_idle:
            return True
        if self.monthly_usd:
            return True
        return rate_is_known(self.sku)


class ScanStatus(str, Enum):
    """Why a section of the inventory holds the rows it holds.

    Deliberately the same shape as `LogStatus` in `deploy/logs.py`, for the same
    reason: a list is only evidence when the call that produced it succeeded.
    """

    #: The listing returned. Its rows -- including none of them -- are evidence.
    OK = "ok"
    #: The listing raised. Says nothing at all about what is running there.
    FAILED = "failed"


@dataclass
class SectionScan:
    """One listing `collect_inventory` attempted, and whether it happened."""

    section: str
    status: ScanStatus = ScanStatus.OK
    detail: str = ""

    @property
    def is_evidence(self) -> bool:
        """True only when an absence of rows in this section means something."""
        return self.status is ScanStatus.OK

    def __str__(self) -> str:
        if self.is_evidence:
            return self.section
        return f"{self.section} ({self.detail})"


@dataclass
class Inventory:
    items: list[BillingItem] = field(default_factory=list)
    #: One entry per listing attempted, so an empty `items` can be read. Left
    #: empty by a hand-built Inventory, where the caller supplied the rows and
    #: there is no failed call to hide; `collect_inventory` always fills it.
    scans: list[SectionScan] = field(default_factory=list)

    @property
    def failed_scans(self) -> list[SectionScan]:
        """Listings that did not happen. While this is non-empty, an empty table
        is silence rather than a clean workspace."""
        return [s for s in self.scans if not s.is_evidence]

    @property
    def billing(self) -> list[BillingItem]:
        return [i for i in self.items if i.bills_when_idle]

    @property
    def unpriced(self) -> list[BillingItem]:
        """Billing resources this tool holds no rate for. Never worth 0."""
        return [i for i in self.billing if not i.rate_known]

    @property
    def unread(self) -> list[BillingItem]:
        """Rows that exist because something about them could NOT be read.

        Deliberately spans `billing` and the rest, because the rows that hurt
        most are the ones outside it: a Running job and a cluster whose
        min_instances never came back are both excluded from `billing` (nothing
        here may act on a value nobody measured), so every verdict keyed on
        `billing` alone printed "BILLING NOW: nothing. No always-on compute in
        this workspace." directly beneath a visible `?` row saying the opposite,
        and `down` signed off "meter stopped." over it. A `?` in the table and a
        clean verdict under it cannot both be true; this is what the verdict
        consults so they cannot disagree.
        """
        return [i for i in self.items if i.cost_unknown]

    @property
    def hourly(self) -> float:
        # The filter changes no arithmetic -- an unpriced item contributes 0.0
        # either way -- but it states that the total is a total of the *priced*
        # resources, which is the only claim callers are allowed to make.
        return sum(i.hourly for i in self.items if i.rate_known)

    @property
    def monthly(self) -> float:
        return self.hourly * HOURS_PER_MONTH


def _orphaned_nic_names(nics: list) -> set[str]:
    """NIC names with no VM attached, lowercased for id matching."""
    orphans = set()
    for n in nics or []:
        try:
            if not (n.get("properties") or {}).get("virtualMachine"):
                orphans.add(str(n.get("name", "")).lower())
        except AttributeError:
            continue
    return orphans


def orphan_items(disks: list, public_ips: list, nics: list) -> list[BillingItem]:
    """Find resources a deleted VM left behind that Azure still charges for.

    Deleting a VM does **not** delete its OS disk or its public IP. Both keep
    billing indefinitely, and neither is visible to the AML workspace client
    that `collect_inventory` uses, so nothing in this repo was looking for them.
    A real leak of $41.66/month went unnoticed this way.

    The public-IP rule is transitive on purpose. The leaked IP had a valid
    `ipConfiguration`, so it looked attached; it pointed at a NIC whose VM had
    already been deleted. Checking only for a missing `ipConfiguration` reports
    that IP as healthy, which is exactly the failure this function exists to
    prevent.
    """
    items: list[BillingItem] = []
    dead_nics = _orphaned_nic_names(nics)
    live_nics = {
        str(n.get("name", "")).lower()
        for n in nics or []
        if isinstance(n, dict) and str(n.get("name", "")).lower() not in dead_nics
    }

    for d in disks or []:
        if not isinstance(d, dict):
            continue
        props = d.get("properties") or {}
        # `managedBy` is the authority: if a VM still claims the disk, deleting
        # it would break that VM, whatever `diskState` happens to say.
        if d.get("managedBy"):
            continue
        if str(props.get("diskState", "")).lower() != "unattached":
            continue
        gb = int(props.get("diskSizeGB") or 0)
        sku = str((d.get("sku") or {}).get("name", ""))
        price = disk_monthly_usd(gb, sku)
        detail = f"{gb} GB {sku}, unattached"
        if not price:
            detail += " (price unknown for this SKU)"
        items.append(
            BillingItem(
                kind="orphaned-disk",
                name=str(d.get("name", "")),
                detail=detail,
                sku=sku,
                bills_when_idle=True,
                monthly_usd=price,
            )
        )

    for ip in public_ips or []:
        if not isinstance(ip, dict):
            continue
        props = ip.get("properties") or {}
        config_id = (props.get("ipConfiguration") or {}).get("id", "")
        if config_id:
            nic_name = _nic_name_from_config_id(config_id)
            if nic_name in live_nics:
                continue
        sku = str((ip.get("sku") or {}).get("name", ""))
        price = public_ip_monthly_usd(sku)
        detail = f"{sku} public IP, " + (
            "attached to a NIC with no VM" if config_id else "not attached to anything"
        )
        if not price:
            detail += " (price unknown for this SKU)"
        items.append(
            BillingItem(
                kind="orphaned-public-ip",
                name=str(ip.get("name", "")),
                detail=detail,
                sku=sku,
                bills_when_idle=True,
                monthly_usd=price,
            )
        )

    return items


def _nic_name_from_config_id(config_id: str) -> str:
    """Pull the NIC name out of an ipConfiguration resource id, case-folded.

    ARM returns resource ids with inconsistent casing across APIs, so matching
    on the raw string silently fails and reports live IPs as orphans.
    """
    parts = str(config_id).lower().split("/")
    try:
        return parts[parts.index("networkinterfaces") + 1]
    except (ValueError, IndexError):
        return ""


def ips_judgeable_without_the_nic_listing(public_ips: list) -> list:
    """The public IPs whose orphan status does not need the NIC listing.

    `orphan_items` judges a public IP transitively: an IP that carries an
    `ipConfiguration` is leaked only when the NIC it points at has no VM, and
    the NIC listing is the only thing that answers that. Hand that classifier an
    empty NIC list *because the NIC listing did not complete* and every attached
    IP in the resource group comes back an orphan, with an `az network
    public-ip delete` printed under it, for an IP that is doing its job. That is
    this round's own invariant pointing the other way -- claiming a measurement
    nobody made -- and it is the trap that makes "report the rows you did read"
    wrong if it is applied bluntly.

    An IP with no `ipConfiguration` is attached to nothing at all, which the IP
    listing states by itself. Those rows survive an unread NIC listing. The
    rest are withheld, and `read_orphans` says how many in the scan detail.
    """
    return [
        ip
        for ip in public_ips or []
        if isinstance(ip, dict)
        and not ((ip.get("properties") or {}).get("ipConfiguration") or {}).get("id")
    ]


#: Section names are constants because `cmd_down` has to ask whether *these
#: specific* listings succeeded before it may claim an endpoint is empty. A
#: duplicated string literal there would go stale silently and re-open the hole:
#: a name that matches nothing looks exactly like a scan that passed.
ONLINE_ENDPOINTS_SECTION = "online endpoints"
#: What `read_orphans` records under. Same vocabulary as the AML sections on
#: purpose -- one report, one convention for "this listing did not happen".
ORPHANS_SECTION = "orphaned disks/IPs (resource group)"


def deployments_section(endpoint_name: str) -> str:
    """The scan name for one endpoint's deployment listing."""
    return f"deployments of {endpoint_name}"


@contextmanager
def _section(inv: Inventory, name: str):
    """Run one listing and record whether it actually happened.

    Replaces a bare `except Exception: log.warning(...)`. Keeping the report
    alive was right; leaving no trace in the Inventory was not. A wrong resource
    group made all four listings raise, the four warnings went to stderr as
    prose, and stdout printed "BILLING NOW: nothing. No always-on compute in
    this workspace." -- byte-identical to a real teardown. That is the failure
    `deploy/logs.py` exists to prevent, one directory over and on the money
    path. The scan is what lets `format_inventory` refuse to make that claim.
    """
    scan = SectionScan(name)
    inv.scans.append(scan)
    try:
        yield scan
    except Exception as exc:  # noqa: BLE001 - record the gap, never abort the report
        scan.status = ScanStatus.FAILED
        # The type matters as much as the message: `ResourceNotFoundError` says
        # look at the rg/workspace above, `HttpResponseError: 403` says the
        # workspace is right and the identity is not.
        scan.detail = f"{type(exc).__name__}: {exc}"
        log.warning("could not list %s: %s", name, exc)


def collect_inventory(client) -> Inventory:
    """Walk the workspace and classify everything that could be metered.

    Each section is wrapped: a missing permission or an unsupported API on one
    resource type must not stop the report, because a partial cost report is far
    more useful than a traceback when you are trying to stop the meter. What a
    wrapped section may not do is vanish -- every listing records a
    `SectionScan`, so four failed calls cannot render as an idle workspace.
    """
    inv = Inventory()

    # Online endpoints first -- these are the ones that bill 24/7.
    with _section(inv, ONLINE_ENDPOINTS_SECTION):
        for endpoint in client.online_endpoints.list():
            deployments = []
            with _section(inv, deployments_section(endpoint.name)) as scan:
                deployments = list(client.online_deployments.list(endpoint.name))
            if not deployments:
                if scan.is_evidence:
                    detail = "no deployments (endpoint shell only, no compute cost)"
                else:
                    # The same categorical claim as the report-wide one, scoped to
                    # one row: an endpoint whose deployments could not be listed
                    # was printed as a shell with "no compute cost", which is the
                    # sentence that leaves an A10 serving.
                    detail = "deployments could NOT be listed -- what runs here is unknown"
                inv.items.append(
                    BillingItem(
                        kind="online-endpoint",
                        name=endpoint.name,
                        detail=detail,
                        # Same reason as the detail above, in the money column:
                        # this row rendered "-", which `format_inventory` defines
                        # as free, over an endpoint whose deployments nobody could
                        # list. The NOTE said unknown and the $/hr said free.
                        cost_unknown=not scan.is_evidence,
                    )
                )
                continue
            for dep in deployments:
                raw_count = getattr(dep, "instance_count", None)
                # `int(x or 0)` collapsed "the listing did not carry a count"
                # into "the count is zero", and zero instances of a priced SKU
                # renders `-` (the free column) with a `$0.000/hr` total under
                # it -- over a row whose own NOTE says it bills 24/7. That is
                # the §71 defect reached through the count instead of the SKU
                # table. The resource type is what makes `bills_when_idle` true,
                # and that much really was read; only the multiplier was not.
                count_unread = raw_count is None
                detail = "managed online endpoint: NO scale-to-zero, bills 24/7"
                if count_unread:
                    detail += " -- instance count NOT reported, so the rate cannot be multiplied"
                inv.items.append(
                    BillingItem(
                        kind="online-deployment",
                        name=f"{endpoint.name}/{dep.name}",
                        detail=detail,
                        sku=getattr(dep, "instance_type", "") or "",
                        instances=int(raw_count or 0),
                        bills_when_idle=True,
                        cost_unknown=count_unread,
                    )
                )

    with _section(inv, "batch endpoints"):
        for endpoint in client.batch_endpoints.list():
            inv.items.append(
                BillingItem(
                    kind="batch-endpoint",
                    name=endpoint.name,
                    detail="runs on AmlCompute; scales to 0 between jobs",
                )
            )

    # A cluster with min_instances=0 costs nothing idle, so it is reported but
    # not flagged. One with min_instances>0 is a silent, permanent charge.
    with _section(inv, "compute clusters"):
        for compute in client.compute.list():
            if getattr(compute, "type", "") != "amlcompute":
                continue
            raw_min = getattr(compute, "min_instances", None)
            sku = getattr(compute, "size", "") or ""
            priority = (getattr(compute, "tier", "") or "dedicated").lower()
            if raw_min is None:
                # `int(x or 0)` sent a min_instances that never came back down
                # the `else` branch, which prints "idle costs nothing" -- a
                # positive claim about a value nobody read, with the free glyph
                # beside it. Deliberately NOT marked `bills_when_idle`: that
                # field drives `teardown`, and scaling a cluster on a guess is
                # the same error with the sign flipped. It is reported, it is
                # unpriced, and it blocks "meter stopped." via `Inventory.unread`.
                inv.items.append(
                    BillingItem(
                        kind="compute-cluster",
                        name=compute.name,
                        detail=(
                            f"min_instances NOT reported ({priority}): whether this "
                            "cluster holds nodes while idle was never read"
                        ),
                        sku=sku,
                        cost_unknown=True,
                    )
                )
                continue
            min_i = int(raw_min)
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

    # Running jobs are transient, but a hung job on a GPU node bills like an
    # endpoint, so surface them even though `down` will not touch them.
    with _section(inv, "jobs") as scan:
        # `max_results=50` is a NO-OP on this branch and is kept only because
        # removing it would read as a behaviour change. Checked against the
        # installed azure-ai-ml 1.34.1 rather than assumed:
        # `JobOperations.list` does `max_results = kwargs.pop("max_results",
        # None)` and then passes it on ONLY in the `parent_job_name` branch
        # (`get_run_children`). With no parent job -- this call -- it is popped
        # and dropped, so every job is listed. It caps nothing, and nothing here
        # may state a count as if it did.
        listed = list(client.jobs.list(max_results=50))

        # A `None` here is not a job with no status; it is a job the SDK could
        # not turn into a `Job` at all, and this is the third shape of the
        # round-7 invariant: a read that SUCCEEDED and was INCOMPLETE, with the
        # incompleteness delivered INSIDE the successful list instead of as an
        # exception. Neither guard in `tests/` can see it -- the swallow guard
        # walks `except` handlers under `src/ffsft/` and `docker/`, and this
        # `except` is in site-packages; the ARM guard walks
        # `management.azure.com` GETs, and this is the AML client.
        #
        # Measured against the installed azure-ai-ml 1.34.1, not inferred.
        # `JobOperations.list` passes
        # `cls=lambda objs: [self._handle_rest_errors(obj) for obj in objs]`
        # (_job_operations.py:314) and `_handle_rest_errors` (:325) is
        # `except JobParsingError: return None`. Executed against the REAL
        # library with a REST `JobBase` whose `resources` block the entity
        # layer cannot read:
        #
        #     REAL _from_rest_object raised JobParsingError:
        #         'str' object has no attribute 'instance_count'
        #     REAL _handle_rest_errors returned: None
        #
        # `getattr(None, "status", "")` is `""`, so the old comprehension
        # filtered the hole out as "not a running job" and the section stayed
        # OK. Executed end-to-end through the real `cmd_down --all --yes`, one
        # Running A100 job, the ONLY variable being whether the SDK could parse
        # it:
        #
        #     parses -> "still on screen with an unread cost ... hung-a100-job"
        #               rc=1, no "meter stopped."
        #     None   -> "BILLING NOW: nothing. No always-on compute in this
        #               workspace."   "meter stopped."   rc=0
        #
        # rc=0 is what `down --all --yes && echo clean` reads. The SDK's own
        # trace of it is `module_logger.info("Failed to parse job resource")` on
        # an `azure.*` logger, which `logging_setup.QuietAzureFilter` drops
        # below `QUIET_THRESHOLD` -- so nothing reached the operator at all.
        #
        # The parsed rows are KEPT and the scan is marked FAILED, the same shape
        # `read_orphans` uses below: a section may hold measured rows AND an
        # unread entry at once. Marking it FAILED is what makes `is_evidence`
        # false, so "no running jobs" stops being sayable -- which is the only
        # honest reading, because a `None` carries nothing, not even the name of
        # the job it stood for.
        unreadable = sum(1 for j in listed if j is None)
        active = [
            j
            for j in listed
            if j is not None
            and getattr(j, "status", "") in {"Running", "Preparing", "Queued", "Starting"}
        ]
        for job in active:
            status = getattr(job, "status", "?")
            # "consuming cluster nodes" was applied to every active status, and
            # a Queued job has been allocated none -- it is waiting for exactly
            # the nodes the sentence says it is burning. Over-warning is the
            # cheaper direction, but stating a resource state that was not read
            # is the same defect as under-warning, and the status field that
            # distinguishes them is already in hand.
            holds_nodes = status in {"Running", "Preparing", "Starting"}
            detail = (
                f"status={status}: consuming cluster nodes"
                if holds_nodes
                else f"status={status}: waiting for nodes, holding none yet"
            )
            inv.items.append(
                BillingItem(
                    kind="job",
                    name=getattr(job, "name", "?"),
                    detail=detail,
                    # The job listing carries no VM size and no node count, so
                    # there is nothing to price -- and "consuming cluster nodes"
                    # under a "-" in the $/hr column said those nodes are free.
                    cost_unknown=True,
                )
            )

        if unreadable:
            # Recorded onto the scan rather than raised, for the reason
            # `read_orphans` gives at length: a raise leaves the `with` and
            # throws away the jobs that DID parse, and losing measured rows to
            # report an unread one is the defect this round already fixed once.
            # FAILED is the honest status -- `is_evidence` false means "an
            # absence of rows here proves nothing", which is exactly true of a
            # listing with a hole in it.
            #
            # The count is all there is to say. `_handle_rest_errors` returns a
            # bare `None`, so the job's name, status and compute never reach
            # this process; "1 job" is the whole of what was measured and the
            # sentence may not imply more.
            scan.status = ScanStatus.FAILED
            scan.detail = (
                f"{unreadable} job(s) in this workspace could not be read: the SDK "
                f"returned no usable record for them, so whether any is running on "
                f"a GPU node is unknown"
            )

    return inv


def read_orphans(target, *, credential=None, inv: Inventory | None = None) -> list[BillingItem]:
    """Scan the resource group for paid-for debris left by deleted VMs.

    This deliberately bypasses the AML client. Disks, NICs and public IPs are
    resource-group resources, not workspace resources, which is the structural
    reason `collect_inventory` could never have found them.

    Never raises -- a cost report that raises is a cost report nobody runs --
    and every failure is recorded as a `SectionScan` on `inv`, the same way
    every AML listing records one.

    It does NOT return `[]` on any failure, and this docstring said it did until
    round 10. That sentence was written when the three listings were three
    arguments to one `orphan_items(...)` call, so one raising discarded the other
    two and `[]` really was the only thing that came back. §81.1 made the three
    listings independent; the claim was retracted in JOURNAL §11.4 and §74.2,
    CLAUDE.md and lab7, and left standing HERE, in the docstring of the function
    it describes -- every derived copy corrected and the source of truth missed.
    Executed against the patched code, disks COMPLETE and holding the §11.4
    Premium_LRS disk while the NIC listing 403s:

        a failure DID occur       : ['orphaned disks/IPs (resource group)']
        read_orphans returned     : ['vm-a10-ffsft_OsDisk_1']

    So: rows from the listings that completed, plus a FAILED scan naming the one
    that did not. Both, at the same time. Without that scan, this function was
    `collect_inventory`'s pre-round-3 bug living on 60 lines below the fix: a
    credential raising 403 produced `[]`, no `LEFTOVERS` block, and the report
    printed "BILLING NOW: nothing. No always-on compute in this workspace."
    JOURNAL §11.4 already said so in as many words -- "`read_orphans` 는 어떤
    실패에도 `[]` 를 돌려주므로 '고아 없음'이 '인증 실패'를 가리고 있을 수 있다" --
    and this is the path that leaked $41.66/month. `log.debug` was the other half:
    invisible at the default level, so the only trace of the failure went nowhere,
    where the AML sections at least got a `log.warning`.

    `inv` is optional so a caller that only wants the rows still works, but every
    caller that renders a report passes it; the scan is what lets
    `format_inventory` refuse to call an unread resource group a clean one.
    """
    items: list[BillingItem] = []
    scans = inv if inv is not None else Inventory()
    # `_section` does the recording *and* the log.warning, and it is the reason
    # this is not a second convention for the same rule.
    with _section(scans, ORPHANS_SECTION) as scan:
        cred = credential
        if cred is None:
            # Only built when the caller supplied none: importing it up front
            # made an injected credential still depend on azure.identity.
            from azure.identity import DefaultAzureCredential

            cred = DefaultAzureCredential()
        token = cred.get_token("https://management.azure.com/.default").token
        headers = {"Authorization": f"Bearer {token}"}
        base = (
            f"https://management.azure.com/subscriptions/{target.subscription_id}"
            f"/resourceGroups/{target.resource_group}/providers"
        )

        # Imported at the point of use, per CLAUDE.md, and inside the section so
        # a missing optional extra is recorded as "could not look" rather than
        # silently answering "no orphans" -- the old ImportError branch returned
        # [] too. After the token, because the credential is the seam a caller
        # can inject, and that path must not depend on an extra it never uses.
        import requests

        # Same reason the `requests` import above is here: inside the section so
        # a missing module is recorded as "could not look" rather than crashing
        # the report. Function-local matches every other caller of this helper
        # (`probes.py`, `identity.py`), and `read_all_arm_pages` takes the
        # module as an argument so a test still patches `requests.get` itself.
        from .preflight import read_all_arm_pages

        def fetch(path: str, api: str) -> list:
            # Every page, not the first one. An ARM list-by-resource-group
            # response carries `value` and, when more exist, `nextLink`, and
            # this read `resp.json().get("value", [])` -- a successful read of
            # PART of the list, landing on the same `[]` an empty resource group
            # returns. Nothing raises, so `_section` above recorded the scan as
            # OK and the short page became a measured fact. Executed, a fake
            # `requests` serving ARM-shaped JSON, a fake empty ML client and the
            # real `cmd_down --all --yes`, the only variable being which page
            # the 256 GB Premium_LRS disk of JOURNAL §11.4 arrived on:
            #
            #     disk on page 1 -> "NOT idle: 1 leftover resource(s)"   rc=3
            #     disk on page 2 -> "BILLING NOW: nothing. No always-on
            #                        compute in this workspace."
            #                       "meter stopped."                     rc=0
            #
            # rc=0 is what `down --all --yes && echo clean` reads, so `clean`
            # printed over a live $41.66/month leak. That is the third time this
            # one money path has produced a false all-clear -- §11.4 was a 403,
            # round 3's was a swallowed exception, this one arrives with a 200 --
            # and it is why the fix is a helper rather than a `while nextLink`
            # here: `read_all_arm_pages` RAISES `TruncatedListing` instead of
            # returning a short list, so a listing that stopped early lands in
            # `_section`'s handler and is recorded as a FAILED scan. A truncated
            # listing is not a new `ScanStatus`; `is_evidence` asks one boolean
            # question -- does an absence of rows here mean anything -- and a
            # short read answers no exactly as a 403 does. What tells the two
            # apart for the operator is `scan.detail`, which already carries the
            # exception type.
            return read_all_arm_pages(
                requests,
                f"{base}/{path}?api-version={api}",
                headers=headers,
                timeout=30,
            )

        # Three listings, three INDEPENDENT reads. They were three arguments to
        # one call, and Python evaluates all three before calling, so one of
        # them raising threw away the other two. Executed with a labelled fake
        # serving the disks listing COMPLETE and holding the 256 GB Premium_LRS
        # disk of JOURNAL §11.4, while the NIC listing 403s on its second page:
        #
        #     could not list orphaned disks/IPs (resource group): 403 fake ARM
        #     read_orphans returned : []
        #     failed_scans          : ['orphaned disks/IPs (resource group)
        #                              (RuntimeError: 403 fake ARM error)']
        #
        # That is round 7's invariant running backwards: round 7 fixed "could
        # not look, reported as looked-and-saw-nothing", and this is "looked and
        # FOUND, reported as could not look". It fails toward alarm -- rc=1, no
        # "meter stopped.", `&& echo clean` stays silent -- so it is the safe
        # direction and the failure path below is NOT weakened to save the rows.
        # What it costs is the disk's NAME: the operator is told the scan did
        # not happen while one of the three listings did happen and found the
        # thing they have to delete, and they lose their
        # `az disk delete vm-a10-ffsft_OsDisk_1`. A section may hold measured
        # rows AND an unread listing at the same time, and `_section`,
        # `unlisted_note` and `leftovers_lines` already print both.
        failures: list[tuple[str, Exception]] = []

        def read_listing(label: str, path: str, api: str) -> list | None:
            """This listing's rows, or None when THIS listing did not complete.

            `None`, never `[]`, and it is the same distinction the whole
            function turns on one level up: `[]` is ARM stating the provider
            holds nothing in this resource group, `None` is nobody knowing.
            Recorded in `failures` on the way out, so nothing here is dropped.
            """
            try:
                return fetch(path, api)
            except Exception as exc:  # noqa: BLE001 - recorded below, never dropped
                failures.append((label, exc))
                log.warning("could not list %s in %s: %s", label, target.resource_group, exc)
                return None

        disks = read_listing("disks", "Microsoft.Compute/disks", "2023-04-02")
        ips = read_listing("public IPs", "Microsoft.Network/publicIPAddresses", "2023-09-01")
        nics = read_listing(
            "network interfaces", "Microsoft.Network/networkInterfaces", "2023-09-01"
        )

        withheld = 0
        if nics is None:
            # See `ips_judgeable_without_the_nic_listing`: without the NICs, an
            # attached IP cannot be told from a leaked one, and guessing prints
            # a delete command for a live address.
            judgeable = ips_judgeable_without_the_nic_listing(ips)
            withheld = len([i for i in ips or [] if isinstance(i, dict)]) - len(judgeable)
            ips = judgeable
        items = orphan_items(disks or [], ips or [], nics or [])

        if failures:
            # Recorded onto the scan this section already opened, rather than
            # raised: a raise would leave the `with` before `return items`, and
            # dropping the rows is the defect. Status is still FAILED, so
            # `is_evidence` is False and every consumer -- `failed_scans`,
            # `blind_spots`, `closing()`'s `rg_unread`, `format_inventory`'s
            # workspace/orphan split -- keeps treating this section's silence as
            # silence. rc stays EXIT_COULD_NOT_LOOK, which still outranks
            # EXIT_NOT_IDLE; that is round 5's priority and it is untouched.
            #
            # `Type: message` is `_section`'s own handler's format, kept because
            # `scan.detail` is what `unlisted_note` prints and the exception
            # TYPE is its load-bearing half: `TruncatedListing` says the
            # identity was fine and the list did not finish, `HttpResponseError:
            # 403` says the opposite. The listing's own name is added because
            # there are three of them now and "which one" is the next question.
            notes = []
            for label, exc in failures:
                note = f"{type(exc).__name__}: {label}: {exc}"
                if label == "network interfaces" and withheld:
                    note += f" (so {withheld} attached public IP(s) could not be judged)"
                notes.append(note)
            scan.status = ScanStatus.FAILED
            scan.detail = "; ".join(notes)
    return items


#: Appended to any row whose rate we do not hold, matching the wording the disk
#: path has always used so one report never has two vocabularies for one gap.
UNKNOWN_PRICE_NOTE = "(price unknown for this SKU)"


def unpriced_note(items: list[BillingItem]) -> str:
    """One line naming what a total left out. Counting them is not enough.

    "excludes 1 resource" tells you to go looking; naming it tells you which GPU
    is still running.
    """
    named = ", ".join(f"{i.name} [{i.sku or 'no sku reported'}]" for i in items)
    return f"EXCLUDES {len(items)} resource(s) whose rate is unknown: {named}"


def unlisted_note(scans: list[SectionScan]) -> str:
    """One line naming which listings failed. Counting them is not enough.

    Same rule as :func:`unpriced_note` above, for the same reason: "1 listing
    failed" sends you to the scrollback, "compute clusters (HttpResponseError:
    403)" tells you a GPU cluster is the thing you cannot currently see.
    """
    named = "; ".join(str(s) for s in scans)
    return f"COULD NOT LOOK at {len(scans)} listing(s): {named}"


def failed_orphan_scans(inv: Inventory) -> list[SectionScan]:
    """The resource-group listing, when it did not happen."""
    return [s for s in inv.failed_scans if s.section == ORPHANS_SECTION]


def orphan_scan(inv: Inventory) -> SectionScan | None:
    """That listing's own record, or None when it was never attempted at all.

    "Did the scan happen?" is a stronger question than "did any scan fail?", and
    the difference is the whole defect this function was added for: `cmd_down`
    never called `read_orphans`, so there was no failed scan to find, and the
    absence of one read as a clean resource group all the way through to the
    sentence "meter stopped."
    """
    for scan in inv.scans:
        if scan.section == ORPHANS_SECTION:
            return scan
    return None


def leftovers_lines(orphans: list[BillingItem], failed: list[SectionScan]) -> list[str]:
    """The LEFTOVERS block, or [] when there is genuinely nothing to say.

    Lifted out of `format_inventory` so `cmd_down` can print the same block from
    the same rows instead of growing a second copy that drifts. `down` printed
    no block at all, which is this block's own stated failure mode -- a missing
    LEFTOVERS block is the claim "nothing was left behind" -- made underneath
    the strongest wording in the file, "meter stopped."
    """
    lines: list[str] = []
    if not orphans and failed:
        # A missing LEFTOVERS block is itself a claim -- "nothing was left
        # behind" -- and it is the claim that hid a $41.66/month leak once
        # already (JOURNAL §11.4). Same split as BILLING NOW twenty lines up:
        # no figure, no count, and no statement about the resource group.
        lines += [
            "",
            # "did not happen" was true while all three ARM listings were one
            # read. They are three independent reads now, so the disks listing
            # can have completed empty while the NIC listing did not -- and this
            # block still has to be true then. "did not complete" is; the
            # `unlisted_note` on the next line names WHICH of the three.
            "LEFTOVERS: UNKNOWN -- the resource-group scan did not complete, so this report "
            "cannot say whether a deleted VM left a disk or a public IP billing.",
            f"  {unlisted_note(failed)}",
        ]
    if orphans:
        # Not folded into `down`: these are leftovers from resources that no
        # longer exist, deleting a disk is irreversible, and there is no `up`
        # that would recreate them. Show the command; let a human run it.
        unpriced_orphans = [i for i in orphans if not i.rate_known]
        if len(unpriced_orphans) < len(orphans):
            # The sum is over the priced orphans only, for the same reason
            # `Inventory.hourly` filters: a total may only claim what it covers.
            priced_total = sum(i.monthly for i in orphans if i.rate_known)
            cost = f"~${priced_total:,.2f}/month for nothing"
        else:
            # This branch is the BILLING NOW rule applied one block lower, and
            # the stakes are higher here than there: summing unpriced orphans
            # rendered "~$0.00/month for nothing", where the trailing two words
            # turn a hole in the price table into "nothing to recover here" --
            # the one sentence that leaves an unattached disk billing forever.
            cost = "cost UNKNOWN -- no rate for any of them, which is not the same as free"
        lines += ["", f"LEFTOVERS: {len(orphans)} resource(s) from deleted VMs, {cost}."]
        if unpriced_orphans:
            lines.append(f"  the total {unpriced_note(unpriced_orphans)}")
        lines.append(
            "`down` will not touch these -- deleting a disk cannot be undone. To remove:"
        )
        for item in orphans:
            verb = "disk" if item.kind == "orphaned-disk" else "network public-ip"
            extra = " --yes" if item.kind == "orphaned-disk" else ""
            lines.append(f"  az {verb} delete -g <rg> -n {item.name}{extra}")
        lines.append("  (delete the NIC first if a public IP refuses to go)")
    return lines


#: `scope_lines` and `UNIDENTIFIED_SCOPE` moved to `preflight` (imported at the
#: top of this module, so `lifecycle.scope_lines` still resolves for anything
#: that imported it from here). `ffsft-deploy check` prints the same header and
#: was printing an unlabelled location instead; one copy of the string is the
#: only way both stay right. `preflight` is the Azure-free half of the deploy
#: split, so neither CLI module has to import the other to reach it.


def format_inventory(inv: Inventory, target: AzureTarget | None = None) -> str:
    """Render the report. `target` is the workspace that answered it.

    `target` stays optional so a hand-built Inventory -- and the tests that
    render one -- keep working; every path that actually talked to Azure passes
    it, because without it the report cannot say where it looked.
    """
    lines = [
        "",
        # AML_CLIENT_SCOPE, not a claim that the header covers everything below
        # it: `cmd_status` appends `read_orphans` rows, which come from an ARM
        # scan of the resource group and never go through `get_ml_client`.
        *scope_lines(target, AML_CLIENT_SCOPE),
        "",
        f"{'KIND':<20} {'NAME':<34} {'SKU':<26} {'$/hr':>8}  NOTE",
        "-" * 132,
    ]
    for item in sorted(inv.items, key=lambda i: (not i.bills_when_idle, i.kind)):
        marker = "!!" if item.bills_when_idle else "  "
        if not item.rate_known:
            # "-" is the free column and would be read as free. A rate we do not
            # have gets its own glyph and says so in the NOTE.
            rate = "?"
        else:
            rate = f"{item.hourly:.3f}" if item.hourly else "-"
        detail = item.detail
        # The note names a *SKU* we could not price. A `cost_unknown` row has no
        # SKU to look up and its own detail already says which read did not
        # happen, so appending this would assert a SKU that was never reported.
        if not item.rate_known and not item.cost_unknown and UNKNOWN_PRICE_NOTE not in detail:
            detail = f"{detail} {UNKNOWN_PRICE_NOTE}"
        lines.append(
            f"{marker}{item.kind:<18} {item.name:<34} {item.sku:<26} {rate:>8}  {detail}"
        )
    lines.append("-" * 132)
    orphans = [i for i in inv.items if i.kind.startswith("orphaned-")]
    unpriced = inv.unpriced
    failed = inv.failed_scans
    # Which half of the report a failure belongs to. The AML listings are the
    # workspace; ORPHANS_SECTION is an ARM scan of the resource group, reaching
    # the report through a different function entirely, so a sentence about "the
    # workspace" is false when that scan is the only one that did not happen.
    workspace_failed = [s for s in failed if s.section != ORPHANS_SECTION]
    # Rendered here rather than at the bottom because the BILLING NOW lines have
    # to know what this block already said. Naming the resource-group scan in
    # both printed the identical `COULD NOT LOOK at 1 listing(s): orphaned
    # disks/IPs ...` line twice, four lines apart, which reads as two failures.
    orphan_failed = failed_orphan_scans(inv)
    leftovers = leftovers_lines(orphans, orphan_failed)
    # Asking the block what it rendered, rather than re-deriving the condition
    # `leftovers_lines` uses: a second copy of that condition is what would put
    # the duplicate back the next time either side moves.
    leftovers_names_it = bool(orphan_failed) and any(
        unlisted_note(orphan_failed) in line for line in leftovers
    )
    named_here = [s for s in failed if not (leftovers_names_it and s.section == ORPHANS_SECTION)]
    # What failed, named once each. Non-empty whenever `failed` is: a scan is
    # either named here or handed to LEFTOVERS, never dropped by both. The
    # pointer is not decoration -- without it the count above says two listings
    # failed and the line under it names one, which reads as a third gap.
    failure_notes: list[str] = []
    if named_here:
        failure_notes.append(unlisted_note(named_here))
    if leftovers_names_it:
        failure_notes.append(
            "The resource-group scan failed too; LEFTOVERS names it below."
            if named_here
            else "The resource-group scan is what failed; LEFTOVERS names it below."
        )
    if inv.billing:
        if len(unpriced) < len(inv.billing):
            lines.append(
                f"BILLING NOW: {len(inv.billing)} resource(s)  "
                f"${inv.hourly:.3f}/hr  ~${inv.monthly:,.0f}/month if left running"
            )
        else:
            # Every billing resource is unpriced, so there is no total to print.
            # Printing $0.000/hr here is the exact bug this branch exists to kill.
            lines.append(
                f"BILLING NOW: {len(inv.billing)} resource(s)  "
                "cost UNKNOWN -- no rate for any of them, which is not the same as free"
            )
        if unpriced:
            lines.append(f"  the total {unpriced_note(unpriced)}")
        if failed:
            # "A total may only claim what it covers" -- the rule the unpriced
            # split already follows, applied to the resources that never reached
            # the table at all. A count is a floor here, not a count.
            lines.append(f"  the count covers only what could be listed. {failure_notes[0]}")
            lines += [f"  {note}" for note in failure_notes[1:]]
        lines.append("Run `ffsft lifecycle down --all --yes` to stop the meter.")
    elif failed:
        # The branch the old `else` swallowed. Every word here is chosen to be
        # unreadable as reassurance: no figure, no count of resources, and no
        # claim about the workspace -- only about this tool.
        # "the empty table above" was a claim about the rows, and the table is
        # not always empty here: `collect_inventory` emits a visible dark-endpoint
        # row in this exact failure, and that row does not bill when idle, so
        # `inv.billing` is empty and this branch runs with a row on screen. What
        # holds in both states is that an *absence* from the table proves nothing
        # while a listing is missing.
        lines.append(
            f"BILLING NOW: UNKNOWN -- could not look. {len(failed)} of {len(inv.scans)} "
            "listing(s) failed, so what is missing from the table above is silence, "
            "not evidence."
        )
        lines += [f"  {note}" for note in failure_notes]
        # The workspace listings can all have returned here -- a failed orphan
        # scan on its own reaches this branch -- and then the workspace was
        # perfectly readable and it is the resource group that was not.
        lines.append(
            "Fix the errors above and re-run: "
            + (
                "an unreadable workspace is not an idle one."
                if workspace_failed
                else "an unread resource group is not a clean one."
            )
        )
    else:
        lines.append("BILLING NOW: nothing. No always-on compute in this workspace.")

    # Printed under every branch, because the contradiction it fixes appeared
    # under every branch: a `?` row is on screen saying its cost was never read,
    # while the verdict above is keyed on `inv.billing`, which these rows are
    # deliberately not in. Without this the report said "nothing" and showed a
    # Running job in the same breath, with no failed scan to explain it.
    outside = [i for i in inv.unread if not i.bills_when_idle]
    if outside:
        named = ", ".join(f"{i.name} [{i.kind}]" for i in outside)
        lines.append(
            f"  that verdict EXCLUDES {len(outside)} row(s) marked ? above, whose cost "
            f"was never read: {named}"
        )

    lines += leftovers
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


def teardown_deployment(
    client, endpoint: str, deployment: str, *, dry_run: bool = True
) -> list[str]:
    """Delete ONE deployment and leave its endpoint -- and its siblings -- standing.

    `teardown` deletes the endpoint, which takes every deployment with it. That
    is right for `--all` and wrong for the blue/green step: lab 8 shifts 100% of
    the traffic to green and then says "blue 를 지우세요", and until now there was
    no command that did that. The only available teardown destroyed the endpoint,
    so the realistic outcome was that blue was left alone and kept billing.
    """
    removed = [f"online-deployment {endpoint}/{deployment} (endpoint {endpoint} kept)"]
    if not dry_run:
        log.info("deleting deployment %s of endpoint %s", deployment, endpoint)
        client.online_deployments.begin_delete(name=deployment, endpoint_name=endpoint).result()
    return removed


#: Returned when a command could not see what it is talking about. Distinct from
#: 2, which every refusal in this file uses for "you did not say what you meant"
#: -- that one is a usage error the operator fixes by typing something else, this
#: one is a workspace that did not answer. Both are non-zero, which is the part
#: that matters: `ffsft lifecycle down --all --yes && echo clean` printed `clean`
#: over four failed listings, because the prose said UNKNOWN and the exit code
#: said success. Scripts read the exit code.
EXIT_COULD_NOT_LOOK = 1

#: Returned by `down` when every listing answered and what they showed is a
#: resource group that is still billing -- an orphaned disk or public IP that
#: `down` is not allowed to delete.
#:
#: This is a third code rather than a reuse of 1, and the distinction is the
#: whole point. Round 5 shipped `down` returning 0 here, argued from "0 matches
#: `cmd_status` on the same workspace" and "EXIT_COULD_NOT_LOOK means could not
#: look, and finding these is the opposite of that". The second half of that
#: argument is right, which is why this is not 1. The first half compares two
#: commands whose exit codes answer different questions: `status` is a read-only
#: report and its code answers "did I manage to read", so 0 is correct there
#: even over a leak. `down`'s last line asserts the meter is stopped, so its
#: code has to answer "is it stopped". Under the old rule
#: `down --all --yes && echo clean` printed `clean` over a measured $41.66/month
#: leak that the command had just printed as "NOT idle ... still billing" --
#: the identical failure `EXIT_COULD_NOT_LOOK` was created to prevent, one line
#: lower. Collapsing the two into one non-zero code would leave the operator
#: unable to tell "I could not see" from "I saw, and it is not idle", which are
#: opposite next moves: fix a permission, versus run the printed `az` command.
EXIT_NOT_IDLE = 3


def blind_spots(inv: Inventory, endpoint: str | None) -> list[SectionScan]:
    """The failed listings that make *this* command's claim unprovable.

    Scope matters, so this is not simply `inv.failed_scans`. `--all` claims
    something about the whole workspace, so every listing is load-bearing. A
    named endpoint claims something about that endpoint only: if the jobs
    listing 403s while the endpoint's deployments listed cleanly, refusing to
    tear the endpoint down would leave a $4.320/hr A10 running to protect a
    statement nobody made. Widening this to every scan is the tempting mistake
    -- it turns a permission gap anywhere in the workspace into an untearable
    endpoint, and the operator's next move is `--yes` somewhere less careful.
    """
    if endpoint is None:
        return inv.failed_scans
    scoped = {ONLINE_ENDPOINTS_SECTION, deployments_section(endpoint)}
    return [s for s in inv.failed_scans if s.section in scoped]


def cmd_status(args) -> int:
    from ffsft.azure_ml import AzureTarget, get_ml_client

    target = AzureTarget.from_env()
    client = get_ml_client(target)
    inv = collect_inventory(client)
    # `inv=inv` is what puts the orphan scan in the same list as the four AML
    # ones, so the report counts five listings and names the one that failed.
    inv.items.extend(read_orphans(target, inv=inv))
    print(format_inventory(inv, target))
    # The report says "could not look"; the exit code has to agree with it. A
    # status run whose listings failed did not observe an idle workspace, and
    # anything scripting this tool sees only this number.
    return EXIT_COULD_NOT_LOOK if inv.failed_scans else 0


def cmd_down(args) -> int:
    deployment = getattr(args, "deployment", None)
    if deployment and not args.endpoint:
        # Refused before the client is even built, so a mistyped teardown cannot
        # reach Azure. Every endpoint in this workshop has a deployment called
        # `blue`, so the name alone does not identify one resource, and picking
        # for the user here would pick a $4.959/hr one.
        print("--deployment needs --endpoint: a deployment name alone is ambiguous")
        return 2

    if not args.endpoint and not getattr(args, "all", False):
        # `--all` was declared on the parser and never read by this function, so
        # `down --yes` and `down --all --yes` were byte-identical: the bare form
        # tore down every billing resource in the workspace while reading like a
        # scoped command. Making the flag real means making the scope explicit
        # rather than making the bare form mean "all" -- every doc that teaches a
        # teardown already passes `--all --yes` or `--endpoint X --yes`, so
        # nothing written down breaks, and the bare form stops instead. Lab 5 put
        # `down --yes` under a "정리 -- 반드시" header and then sent the reader to
        # Lab 6 with the endpoint still needed; refusing here is what turns that
        # from a silent teardown into a question.
        #
        # Refused before the client is built, so this cannot reach Azure.
        print("down needs a scope: --endpoint NAME for one endpoint, or --all for everything")
        print("refusing to guess: --all deletes every billing resource in this workspace")
        return 2

    from ffsft.azure_ml import AzureTarget, get_ml_client

    target = AzureTarget.from_env()
    client = get_ml_client(target)
    inv = collect_inventory(client)
    # The resource-group scan `cmd_status` has run since round 3, now run by the
    # command that ends with the sentence "meter stopped." That sentence is a
    # claim about a resource group this function had never once looked at:
    # `collect_inventory` asks the AML workspace client, which structurally
    # cannot see a disk or a public IP. Executed on one fake holding the
    # replayed $41.66/month leak, `status` printed both orphan rows and the
    # LEFTOVERS block while `down --all --yes` printed "meter stopped." rc=0 and
    # named neither.
    #
    # `inv=inv` is what puts this listing in the same list as the four AML ones,
    # so a failed scan reaches `blind_spots` below and `unlisted_note` in the
    # report -- one convention for "this listing did not happen", not a second.
    # The rows are deliberately NOT appended to `inv.items` the way `cmd_status`
    # appends them: `inv.billing` is what `teardown` walks and what the "stops
    # $X/hr" figure sums, so an orphaned disk in there would join a "will
    # remove:" list that removes nothing and add $38.01/month to a saving that
    # never happens. Reporting them and acting on them are different questions.
    orphans = read_orphans(target, inv=inv)
    blind = blind_spots(inv, args.endpoint)

    def report(current: Inventory) -> str:
        """The report `status` would print for `current`: the AML rows plus the
        resource-group rows, over the one scan list both listings recorded in."""
        return format_inventory(
            Inventory(items=[*current.items, *orphans], scans=current.scans), target
        )

    def closing(current: Inventory, *, leftovers_already_shown: bool = False) -> int:
        """The LEFTOVERS block, every reason the meter may still be running, and
        the exit code that agrees with all of them.

        One routine because round 5 grew eight endings and they disagreed. Each
        early `return` had been written against the case in front of it, so the
        resource-group scan -- paid for on every path -- was rendered by three of
        them and silently discarded by five: the dry run, the endpoint-shell
        delete, the missing-deployment refusal and both no-op paths printed
        nothing about a leak they had already measured, which handed the
        *cautious* operator who previews before `--yes` strictly less
        information than the careless one.

        The reasons are accumulated rather than raced. The old tail returned on
        the first match, so a teardown that successfully scanned the resource
        group and named two leaking resources still signed off "the meter
        stopped for what could be listed" -- those two were exactly what could
        be listed, and their meter had not stopped.
        """
        lines = leftovers_lines(orphans, failed_orphan_scans(current))
        # `format_inventory` ends with this same block, so the paths that print
        # a full report pass `leftovers_already_shown` rather than growing a
        # second copy of the condition that decides whether it appeared.
        if lines and not leftovers_already_shown:
            print("\n".join(lines))

        scan = orphan_scan(current)
        rg_unread = scan is None or not scan.is_evidence
        # One scope rule for the whole resource-group half, and it is the rule
        # `blind_spots` already applies to the AML listings. `--all` claims the
        # workspace is idle, so a leaked disk and an unread scan both bear on
        # that claim. `--endpoint X` claims that one endpoint is gone and claims
        # nothing about the resource group, so both are printed and neither
        # touches rc: widening this is the mistake `blind_spots`' docstring paid
        # for -- a 403 on the resource group must not make a $4.320/hr A10 read
        # as untearable to whatever checks the exit code, because the operator's
        # next move is `--yes` somewhere less careful. The prose is not scoped
        # the same way; it is stricter. A scoped run does not print "meter
        # stopped." at all, because `inv` here holds one endpoint's deployments
        # and the sentence is about a whole workspace.
        rg_in_scope = args.endpoint is None
        # Only the AML half: a failed resource-group scan is reported by the
        # LEFTOVERS block above, and naming it here too printed the identical
        # `COULD NOT LOOK at 1 listing(s): orphaned disks/IPs ...` line twice.
        aml_blind = [s for s in blind if s.section != ORPHANS_SECTION]
        # Rows whose cost was never read and which are therefore not in
        # `billing` -- a Running job, a cluster whose min_instances never came
        # back. `teardown` does not touch them, so they survive it, and the
        # sentence "meter stopped." is false while one is on screen.
        unread = [i for i in current.unread if not i.bills_when_idle]

        if aml_blind:
            print("\nsome workspace listings failed, so this command cannot tell you the")
            print(f"workspace is now idle. {unlisted_note(aml_blind)}")
        if rg_unread:
            # "the scan above did not happen" was written when one raising
            # listing discarded the other two, so there was never anything above
            # it. A completed listing's rows now survive an unread sibling, and
            # this sentence prints directly under the `az disk delete` for a
            # disk that WAS found -- where "did not happen" contradicts the
            # screen. What is true in both states is that the scan is
            # incomplete, so an absence from it still proves nothing.
            print("\nwhat else this resource group holds is UNKNOWN -- at least one of the")
            print("resource-group listings did not complete, so nothing above rules out a")
            print("deleted VM having left another disk or public IP behind.")
        if unread:
            named = ", ".join(f"{i.name} [{i.kind}]" for i in unread)
            print(f"\nstill on screen with an unread cost, and untouched by `down`: {named}")
        if orphans:
            print(f"\nNOT idle: {len(orphans)} leftover resource(s) from deleted VMs are still")
            print("billing in this resource group -- listed above, with the `az` command for")
            print("each. `down` deletes none of them: a disk cannot be un-deleted and no `up`")
            print("recreates it, so that call is yours. `ffsft lifecycle status` after.")

        if aml_blind or unread or (rg_in_scope and rg_unread):
            # "Could not look" outranks "not idle" when both apply: the operator
            # cannot act on a leak list that is admittedly incomplete.
            return EXIT_COULD_NOT_LOOK
        if rg_in_scope and orphans:
            return EXIT_NOT_IDLE
        if not rg_in_scope:
            print("\nthat is the scope you asked for. this run says nothing about the rest of")
            print("the workspace, and nothing above claims the meter is stopped.")
            return 0
        print("\nmeter stopped. `ffsft lifecycle status` to confirm.")
        return 0

    if args.endpoint:
        wanted = f"{args.endpoint}/{deployment}" if deployment else None
        inv = Inventory(
            items=[
                i
                for i in inv.items
                if i.kind == "online-deployment"
                and (i.name == wanted if wanted else i.name.startswith(f"{args.endpoint}/"))
            ],
            # Carried over, not dropped: narrowing the rows does not narrow what
            # the listing failed to see, and the report printed below would
            # otherwise call an unreadable workspace an empty one.
            scans=inv.scans,
        )
        if not inv.items:
            if blind:
                # An empty result here has two causes -- there is nothing, or we
                # could not look -- and the next statement used to delete either
                # way. `down --endpoint X --yes` over a 403 on the deployments
                # listing printed "no online deployment found", then issued a
                # real `online_endpoints.begin_delete(name=X)` and printed
                # "deleted", rc=0. The scans were already being carried into
                # this Inventory for exactly this branch to render, and this
                # branch returned before any `format_inventory` call, so nothing
                # rendered them: lab8's blue teardown read "no deployment 'blue'
                # on endpoint ..." over an unreadable listing while blue billed
                # $4.959/hr. Report first, then refuse -- deleting is the one
                # thing that cannot be taken back by re-running.
                print(report(inv))
                subject = (
                    f"whether deployment '{deployment}' of endpoint "
                    f"'{args.endpoint}' is still there"
                    if deployment
                    else f"what runs on endpoint '{args.endpoint}'"
                )
                print(f"\nCOULD NOT LOOK: {subject} is UNKNOWN.")
                print("nothing was deleted. an endpoint whose deployments could not be listed")
                print("is not an empty one -- it may be serving, and it bills either way.")
                print("fix the errors above and re-run.")
                return EXIT_COULD_NOT_LOOK
            if deployment:
                # Deliberately does NOT fall through to the endpoint-shell delete
                # below. "remove this deployment" is not "remove this endpoint",
                # and the endpoint may still be serving the sibling that the
                # blue/green cutover just moved all the traffic onto.
                print(f"no deployment '{deployment}' on endpoint '{args.endpoint}'")
                print(f"endpoint '{args.endpoint}' left alone; you did not ask to delete it")
                return closing(inv)
            print(f"no online deployment found for endpoint '{args.endpoint}'")
            # Delete the endpoint shell anyway: an endpoint whose deployment
            # failed to create still exists and still blocks the name. Reachable
            # only because `blind` is empty above -- the listing happened and
            # returned nothing, which is evidence.
            if args.yes:
                print(f"deleting endpoint shell '{args.endpoint}'")
                client.online_endpoints.begin_delete(name=args.endpoint).result()
                print("deleted")
            return closing(inv)

    if not inv.billing:
        print(report(inv))
        if orphans:
            # `report` ends in "Run `ffsft lifecycle down --all --yes` to stop
            # the meter" once orphan rows push BILLING NOW off zero -- advice
            # printed BY that command, about resources it is not allowed to
            # delete. The line it would send the operator round in a circle on
            # gets answered here instead.
            print(f"\nthere was no always-on compute to tear down, and the {len(orphans)} "
                  "leftover(s)")
            print("above are still billing. `down` does not delete those; the `az` commands do.")
        # `report` already rendered the LEFTOVERS block; the verdict and the
        # exit code that agrees with it are still owed.
        return closing(inv, leftovers_already_shown=True)

    def plan(dry_run: bool) -> list[str]:
        if deployment:
            return teardown_deployment(client, args.endpoint, deployment, dry_run=dry_run)
        return teardown(client, inv, dry_run=dry_run)

    planned = plan(dry_run=True)
    print("\nwill remove:")
    for entry in planned:
        print(f"  - {entry}")
    if inv.unpriced:
        # Same rule as the status table: a resource we cannot price is never
        # rendered as $0.000/hr, because that reads as "nothing to save here".
        savings = (
            f"stops ${inv.hourly:.3f}/hr (~${inv.monthly:,.0f}/month)"
            if len(inv.unpriced) < len(inv.billing)
            else "stops an UNKNOWN amount per hour"
        )
        print(f"\n{savings}; {unpriced_note(inv.unpriced)}")
    else:
        print(f"\nstops ${inv.hourly:.3f}/hr (~${inv.monthly:,.0f}/month)")

    # Only the AML half. `down` is forbidden from deleting a leftover, so a
    # failed resource-group scan cannot make the removal plan less complete --
    # saying "this plan covers only what could be listed" over it promised rows
    # that fixing the Reader grant would never add, and printed the same
    # `COULD NOT LOOK ... orphaned disks/IPs` line the LEFTOVERS block prints.
    plan_blind = [s for s in blind if s.section != ORPHANS_SECTION]
    if plan_blind:
        # Same rule the BILLING NOW total follows: a plan may only claim what it
        # covers. Here the stakes are one step higher, because the operator is
        # about to read this list as "and then nothing is left running".
        print(f"\nthis plan covers only what could be listed. {unlisted_note(plan_blind)}")

    if not args.yes:
        print("\ndry run. re-run with --yes to actually delete.")
        # The scan already ran -- three ARM GETs and a credential -- and the old
        # branch returned before rendering a byte of it, so previewing a
        # teardown hid the leak that `--yes` would have shown.
        return closing(inv)

    done = plan(dry_run=False)
    print("\nremoved:")
    for entry in done:
        print(f"  - {entry}")
    # Every reason the meter may still be running, accumulated rather than
    # raced, plus the exit code that agrees with all of them. See `closing`.
    return closing(inv)


def effective_sku(explicit: str | None, pattern_key: str) -> str:
    """The SKU the deployment actually runs on, which is not always the typed one.

    `deploy_online` falls back to the serving spec's `default_sku` when `--sku`
    is omitted, so reading `args.sku` alone made the *most common* invocation --
    no `--sku` at all -- print no billing line whatsoever, while a
    Standard_NV12ads_A10_v5 started billing at $1.226/hr, a rate this tool holds.
    Silence reads as "free" just as loudly as $0.000 does.

    Reads a YAML registry and nothing else; a failure to resolve returns "" so a
    cost line can never be what breaks a deployment that already succeeded.
    """
    if explicit:
        return explicit
    try:
        from .registry import get_serving

        return get_serving(pattern_key).default_sku or ""
    except Exception as exc:  # noqa: BLE001 - never fail `up` over a print
        log.debug("could not resolve the default SKU for pattern %s: %s", pattern_key, exc)
        return ""


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
        gpu_memory_utilization=args.gpu_memory_utilization,
        hf_model=args.hf_model or (spec.hf_id if spec and not args.model_uri else None),
        model_spec=spec,
        params_b=args.params_b,
        quantization=args.quantization,
        extra_args=args.extra_args,
    )
    print(f"\nendpoint '{args.endpoint}' is up")
    print(f"scoring uri: {scoring_uri}")
    # The SKU that was deployed, not the SKU that was typed -- see `effective_sku`.
    sku = effective_sku(args.sku, args.pattern)
    if rate_is_known(sku):
        rate = hourly_rate(sku)
        print(f"billing {sku} ${rate:.3f}/hr -> ~${rate * HOURS_PER_MONTH:,.0f}/month if left up")
    elif sku:
        # Saying nothing here reads as "free" just as loudly as $0.000 does.
        print(f"billing rate for {sku} is UNKNOWN to this tool -- it is billing anyway")
    else:
        # Neither a --sku nor a resolvable default. Still the wrong moment to be
        # quiet: the endpoint is up, so something is metering.
        print("billing rate UNKNOWN to this tool -- this endpoint is billing anyway")
    print(f"tear down with: ffsft lifecycle down --endpoint {args.endpoint} --yes")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # After basicConfig, never before: the filter half attaches to the root
    # handlers basicConfig creates. `status` prints one small table, and the SDK
    # was burying it under hundreds of INFO-level HTTP dumps.
    quiet_azure_sdk_logs()
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
    # Both of these reach container ENV the serve image has always read
    # (GPU_MEMORY_UTILIZATION, EXTRA_ARGS); only this parser was missing the
    # passthrough, which made them unreachable without an image rebuild.
    p_up.add_argument(
        "--gpu-memory-utilization",
        type=float,
        default=0.90,
        help="fraction of the card vLLM may claim. Lower it when the weights "
        "nearly fill the GPU: what is left has to cover the KV cache, the "
        "hybrid state cache and CUDA graph capture, and vLLM exits rather "
        "than shrinking to fit.",
    )
    p_up.add_argument("--quantization", default=None)
    p_up.add_argument(
        "--extra-args",
        default="",
        help="appended verbatim to the vLLM launch line. Attach the value with "
        "'=' -- argparse reads a dash-leading token as the next option, so "
        "--extra-args='--enforce-eager --max-num-seqs 8' works and the "
        "space-separated spelling exits 2. Graph capture is the first thing "
        "to drop when a model only just fits.",
    )
    p_up.set_defaults(func=cmd_up)

    p_down = sub.add_parser("down", help="delete always-on compute")
    p_down.add_argument(
        "--endpoint", default=None, help="only this endpoint; one of --endpoint/--all is required"
    )
    p_down.add_argument(
        "--deployment",
        default=None,
        help="only this deployment of --endpoint; the endpoint and any sibling "
        "deployments survive. This is what the blue/green cutover needs -- "
        "deleting the endpoint would take green with it.",
    )
    p_down.add_argument(
        "--all",
        action="store_true",
        help="every billing resource in the workspace. Required when --endpoint is "
        "not given: a scope-less teardown is not a default worth having.",
    )
    p_down.add_argument("--yes", action="store_true", help="actually delete")
    p_down.set_defaults(func=cmd_down)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
