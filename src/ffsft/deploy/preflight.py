"""Checks that must pass before anything expensive is created.

The rule this module exists to enforce: **a deployment that cannot possibly
succeed should say so in seconds.** Azure ML withholds an online deployment's
container logs until the deployment reaches a terminal state, and a rollout that
cannot fetch its artifacts does not reach one -- it retries until Azure's own
timeout. The observable result is over an hour in `Creating`, no logs, and a
generic `InternalServerError`, while the GPU bills the entire time.

That happened twice in this workspace for the same reason before anyone looked
at the storage account, because the failure gives no hint of where to look.
Everything needed to predict it is available from two ARM reads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger("ffsft.deploy.preflight")

#: `isolationMode` values that put Azure ML's managed compute inside a network
#: where a private endpoint is usable. Anything else means the compute sits
#: outside it, and a private endpoint on the storage account does not help.
ISOLATED_MODES = {"allowinternetoutbound", "allowonlyapprovedoutbound"}


@dataclass
class StorageReachability:
    """The facts that decide whether Azure ML can reach workspace storage.

    Deliberately a plain record rather than a live client: the decision is pure,
    so it can be tested against the exact configuration that failed without
    needing a subscription.
    """

    account_name: str
    #: `properties.publicNetworkAccess`. `None` means "not read", which is
    #: different from "off" and must never be treated as a blocker.
    public_network_access: str | None
    #: `properties.networkAcls.bypass`. `None` means "not read". When this
    #: contains `AzureServices` the account is reachable by Azure ML no matter
    #: what `publicNetworkAccess` says -- see `storage_blocker`.
    bypass: str | None = None
    ip_rules: list[str] = field(default_factory=list)
    vnet_rules: list[str] = field(default_factory=list)
    private_endpoints: list[str] = field(default_factory=list)
    workspace_isolation_mode: str | None = None

    @property
    def public_access_off(self) -> bool:
        if self.public_network_access is None:
            return False
        return self.public_network_access.strip().lower() == "disabled"

    @property
    def trusted_services_bypass(self) -> bool:
        """True when Azure ML is exempt from the network rules entirely."""
        if self.bypass is None:
            return False
        return any(p.strip().lower() == "azureservices" for p in self.bypass.split(","))

    @property
    def workspace_is_isolated(self) -> bool:
        mode = (self.workspace_isolation_mode or "").strip().lower()
        return mode in ISOLATED_MODES


def storage_blocker(state: StorageReachability) -> str | None:
    """Return why Azure ML cannot reach workspace storage, or None if it can.

    Three arrangements work. The account is reachable over the public endpoint;
    or `networkAcls.bypass` includes `AzureServices`, which exempts Azure ML
    from the network rules altogether; or there is a private endpoint *and* the
    workspace's managed network is enabled, so the compute running the
    deployment sits on a network that can use it. A private endpoint with a
    non-isolated workspace is the trap worth naming: the account looks fixed,
    and nothing that runs the deployment is on that network.

    The bypass clause is here because leaving it out was a real and expensive
    mistake. An earlier version of this function argued that `networkAcls` need
    not be consulted at all, since `publicNetworkAccess: Disabled` overrides it.
    That is false, and Microsoft says so directly: trusted-service access "takes
    the highest precedence over other network access restrictions". The account
    on this subscription had the bypass set the entire time, so the function
    would have refused every deployment for a reason that was never real -- and
    the confident docstring is exactly what would have stopped anyone checking.

    `ip_rules` and `vnet_rules` remain deliberately unconsulted: they cannot
    grant access that `publicNetworkAccess: Disabled` has already withdrawn.
    """
    if not state.public_access_off:
        return None

    if state.trusted_services_bypass:
        return None

    if state.private_endpoints and state.workspace_is_isolated:
        return None

    lines = [
        f"workspace storage account '{state.account_name}' is unreachable: "
        f"publicNetworkAccess=Disabled",
    ]

    if state.private_endpoints and not state.workspace_is_isolated:
        lines.append(
            f"  it has a private endpoint ({', '.join(state.private_endpoints)}), but the "
            f"workspace managedNetwork isolation mode is "
            f"'{state.workspace_isolation_mode or 'Disabled'}', so the compute that runs "
            f"the deployment is not on a network that can use it."
        )
    elif state.workspace_is_isolated:
        lines.append(
            "  the workspace managed network is enabled, but the storage account has no "
            "private endpoint, so there is still no path to it."
        )
    else:
        lines.append(
            "  there is no private endpoint and no public path, so nothing can reach it."
        )

    if state.ip_rules or state.vnet_rules:
        lines.append(
            "  note: its networkAcls rules are irrelevant here -- publicNetworkAccess "
            "overrides them."
        )

    lines += [
        "",
        "An Azure ML managed online deployment stages artifacts through this account, so",
        "the rollout will retry until it times out: over an hour in 'Creating', no",
        "container logs (Azure withholds them until a deployment is terminal), and the",
        "GPU billing the whole time.",
        "",
        "Fix it one of two ways, then retry:",
        "  1. re-enable public access on the storage account, or",
        "  2. create a private endpoint for it and set the workspace's managedNetwork",
        "     isolation mode to AllowInternetOutbound.",
        "",
        "If option 1 appears to succeed but the value stays 'Disabled', an Azure Policy",
        "modify effect is reverting it and option 2 is the only route.",
        "",
        "Pass force=True to deploy anyway.",
    ]
    return "\n".join(lines)


def read_storage_reachability(target, *, credential=None) -> StorageReachability | None:
    """Read the live facts for `target`'s workspace, or None if unreadable.

    Every failure path returns None rather than raising. This is a preflight
    check: it may prevent a doomed deployment, but it must never be the reason a
    workable one does not happen.
    """
    try:
        import requests
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        log.debug("preflight skipped, azure libraries missing: %s", exc)
        return None

    try:
        cred = credential or DefaultAzureCredential()
        token = cred.get_token("https://management.azure.com/.default").token
        headers = {"Authorization": f"Bearer {token}"}
        base = (
            f"https://management.azure.com/subscriptions/{target.subscription_id}"
            f"/resourceGroups/{target.resource_group}/providers"
            f"/Microsoft.MachineLearningServices/workspaces/{target.workspace_name}"
        )
        ws = requests.get(f"{base}?api-version=2024-10-01", headers=headers, timeout=30)
        ws.raise_for_status()
        ws_props = ws.json().get("properties", {})

        storage_id = ws_props.get("storageAccount")
        if not storage_id:
            return None

        sa = requests.get(
            f"https://management.azure.com{storage_id}?api-version=2023-05-01",
            headers=headers,
            timeout=30,
        )
        sa.raise_for_status()
        sa_body = sa.json()
        sa_props = sa_body.get("properties", {})
        acls = sa_props.get("networkAcls") or {}

        return StorageReachability(
            account_name=sa_body.get("name", storage_id.rsplit("/", 1)[-1]),
            public_network_access=sa_props.get("publicNetworkAccess"),
            bypass=acls.get("bypass"),
            ip_rules=[r.get("value", "") for r in acls.get("ipRules") or []],
            vnet_rules=[r.get("id", "") for r in acls.get("virtualNetworkRules") or []],
            private_endpoints=[
                c.get("name", "") for c in sa_props.get("privateEndpointConnections") or []
            ],
            workspace_isolation_mode=(ws_props.get("managedNetwork") or {}).get(
                "isolationMode"
            ),
        )
    except Exception as exc:  # noqa: BLE001 - a preflight must never be the blocker
        log.debug("preflight could not read storage reachability: %s", exc)
        return None


# -- SKU availability ----------------------------------------------------
#
# Quota answers "how much may I ask for". `restrictions` answers "may I ask at
# all". They are separate gates and the second one is invisible in every place
# a person naturally looks: the portal quota page, `az quota show`, and the
# usages API all report the first.
#
# koreacentral granted 36 dedicated A10 cores to a subscription that is not
# allowed to place an A10 in any of that region's three zones. Three
# deployments were created against that grant. None got a node, none produced a
# log, and each took 50-90 minutes to not happen. One ARM read predicts all of
# it.

#: Restriction reason codes that mean "the scheduler cannot place this here".
#: `QuotaId` is deliberately excluded: it describes which offers may purchase
#: the SKU, not whether this subscription can place one, and treating it as a
#: blocker would refuse deployments that actually work.
BLOCKING_REASON_CODES = {"NotAvailableForSubscription"}


@dataclass
class SkuAvailability:
    """Whether a subscription may place `sku` in `region`, and where.

    A plain record like `StorageReachability`, for the same reason: the
    decision is pure, so the exact configuration that failed can be tested
    without a subscription.
    """

    sku: str
    region: str
    #: Raw `restrictions` from Microsoft.Compute/skus. `None` means "not read",
    #: which must never be treated as a blocker -- that mistake is what let the
    #: AcrPull precheck stay silent for the only case it existed to catch.
    restrictions: list[dict] | None = None
    #: Zones the SKU is offered in at all, from `locationInfo[].zones`.
    zones: list[str] = field(default_factory=list)

    @property
    def region_blocked(self) -> bool:
        """True when the whole region is refused, zones irrelevant."""
        for r in self.restrictions or []:
            if r.get("reasonCode") not in BLOCKING_REASON_CODES:
                continue
            if str(r.get("type", "")).lower() == "location":
                return True
        return False

    @property
    def blocked_zones(self) -> set[str]:
        blocked: set[str] = set()
        for r in self.restrictions or []:
            if r.get("reasonCode") not in BLOCKING_REASON_CODES:
                continue
            if str(r.get("type", "")).lower() != "zone":
                continue
            info = r.get("restrictionInfo") or {}
            blocked |= {str(z) for z in (info.get("zones") or [])}
        return blocked

    @property
    def usable_zones(self) -> set[str]:
        """Zones left to land in. Empty with offered zones means nowhere."""
        if self.region_blocked:
            return set()
        return {str(z) for z in self.zones} - self.blocked_zones


def sku_blocker(state: SkuAvailability | None) -> str | None:
    """Why this SKU cannot be placed in this region, or None if it can.

    Returns None when `state` is None or its restrictions were never read.
    "Not measured" is not evidence of a problem, and a check that guesses
    otherwise gets disabled by the first person it blocks wrongly.
    """
    if state is None or state.restrictions is None:
        return None

    advice = (
        f"More quota will NOT fix this -- quota is permission to ask for "
        f"something the subscription may not have here. Deploy to a region "
        f"where '{state.sku}' is unrestricted, or use a LowPriority/Spot "
        f"pattern, which is allocated from a separate pool and is why training "
        f"runs on GPUs this same subscription cannot serve on."
    )

    if state.region_blocked:
        return (
            f"'{state.sku}' is NotAvailableForSubscription across the whole of "
            f"'{state.region}'. {advice}"
        )

    blocked = state.blocked_zones
    if blocked and not state.usable_zones and state.zones:
        return (
            f"'{state.sku}' is NotAvailableForSubscription in every zone of "
            f"'{state.region}' ({', '.join(sorted(blocked))}), so there is "
            f"nowhere to place it. The deployment will sit in 'Creating' "
            f"without a node and without container logs until it is deleted. "
            f"{advice}"
        )
    return None


def read_sku_availability(
    subscription_id: str,
    region: str,
    sku: str,
    *,
    credential=None,
) -> SkuAvailability | None:
    """Read `restrictions` for one SKU in one region. None if unreadable.

    Raw REST rather than azure-mgmt-compute: this repo already depends on
    `requests` + `azure-identity` for every other ARM read, and adding an SDK
    for one GET would make the check silently unavailable wherever that extra
    is not installed -- which is exactly how this function first shipped, and
    it returned None against a subscription it was supposed to catch.

    Returning None on failure keeps a transient ARM error from blocking a
    deployment that would have worked.
    """
    try:
        import requests
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        log.debug("SKU preflight skipped, azure libraries missing: %s", exc)
        return None

    try:
        cred = credential or DefaultAzureCredential()
        token = cred.get_token("https://management.azure.com/.default").token
        resp = requests.get(
            f"https://management.azure.com/subscriptions/{subscription_id}"
            f"/providers/Microsoft.Compute/skus",
            params={"api-version": "2021-07-01", "$filter": f"location eq '{region}'"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
        resp.raise_for_status()

        for entry in resp.json().get("value", []):
            if entry.get("name") != sku:
                continue
            zones = sorted(
                {
                    str(z)
                    for li in entry.get("locationInfo") or []
                    for z in (li.get("zones") or [])
                }
            )
            return SkuAvailability(
                sku=sku,
                region=region,
                restrictions=entry.get("restrictions") or [],
                zones=zones,
            )

        # Offered nowhere in this region is a different fact from restricted,
        # and not one this check is entitled to turn into a blocker.
        log.warning("SKU %s is not offered at all in %s", sku, region)
        return SkuAvailability(sku=sku, region=region, restrictions=None, zones=[])
    except Exception as exc:  # pragma: no cover - network path
        log.debug("could not read SKU availability for %s in %s: %s", sku, region, exc)
        return None
