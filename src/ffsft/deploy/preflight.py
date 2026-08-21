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
