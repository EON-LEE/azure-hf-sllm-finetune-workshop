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

The scope header at the bottom of this file is here for the same reason one step
earlier -- a check nobody can locate is a check nobody can act on -- and here
rather than beside either caller because two commands print it. See its comment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:  # annotation only -- nothing in this module is allowed to touch Azure
    from ffsft.azure_ml import AzureTarget

log = logging.getLogger("ffsft.deploy.preflight")

#: `isolationMode` values that put Azure ML's managed compute inside a network
#: where a private endpoint is usable. Anything else means the compute sits
#: outside it, and a private endpoint on the storage account does not help.
ISOLATED_MODES = {"allowinternetoutbound", "allowonlyapprovedoutbound"}

# ---------------------------------------------------------------------------
# Paging
#
# An ARM list response is ONE PAGE, not the list. The body carries `value` and,
# when more exist, `nextLink`. Round 6 gave both datastore readers an unread
# sentinel for the case where the GET *fails* and left this door open, which is
# the same mistake with a 200 attached: page 1 of a truncated listing is a
# successful read, and it reads as the complete one.
#
# Executed against the same fake account and the same four fake datastores, the
# only variable being whether ARM paged them (S78.2):
#
#     one page   -> key_auth_refused True  -> deploy_online REFUSED, 0 writes
#     paginated  -> key_auth_refused False -> deploy_online RETURNED and
#                   recorded PUT online_deployments/blue, A10, count=1
#
# That is the round-6 A/B with `403` swapped for `nextLink`, flipping the same
# way: the run nobody could fully vet is the one that got to spend.
# ---------------------------------------------------------------------------

#: Stop following `nextLink` here. A listing that has not ended after this many
#: pages is not a long listing, it is a server looping, and continuing would
#: hang a preflight whose whole promise is "in seconds". Hitting the cap is a
#: truncation like any other -- it is never silently treated as the end.
MAX_ARM_PAGES = 50


class TruncatedListing(RuntimeError):
    """ARM said there was more of the list and it could not be read.

    Raised rather than returned on purpose. Every caller of
    :func:`read_all_arm_pages` already has a "could not look" handler that
    answers with the unread sentinel its own callers branch on, so raising
    routes a partial read into the tri-state round 6 built instead of adding a
    second, parallel way to say the same thing.

    The distinction it protects is the one this whole round is about: a listing
    that stopped early is not a listing that was short.
    """


def read_all_arm_pages(requests_mod, url, *, headers, params=None, timeout=30):
    """Return every item of a paginated ARM list, or raise `TruncatedListing`.

    `requests_mod` is passed in rather than imported here because this module
    is the Azure-free half of the deploy split (see the module docstring) and
    because every caller already imports `requests` function-locally so a test
    can patch the module attribute the function actually reaches for.

    Four shapes are refused rather than read as an empty list. The first three
    all arrive with HTTP 200 and were all measured landing on `[]`:

      * `value` present with a `nextLink` we cannot follow -- the truncation;
      * `value` absent entirely, or `null`, or not a list -- not a well-formed
        `*ArmPaginatedResult` at all, so nothing about the collection was
        actually stated;
      * a `nextLink` that points back at a page already fetched, or a listing
        still going after `MAX_ARM_PAGES`;
      * a `nextLink` on a different host or scheme from the url the caller
        asked for. That one is not about emptiness: `nextLink` chooses where
        this loop sends the caller's bearer token next, and it used to choose
        with no constraint at all. Round 9, argued at the check itself.

    A body of `{"value": []}` with no `nextLink` is NOT refused: that is ARM
    saying the collection is empty, which is a measurement and the one this
    function must keep telling apart from all of the above.
    """
    items: list = []
    seen: set[str] = set()
    next_url = url
    next_params = params
    #: The host and scheme every later page has to agree with. Taken from the
    #: caller's own url, never hardcoded -- see the nextLink check below.
    origin = urlsplit(url)

    for _ in range(MAX_ARM_PAGES):
        if next_url in seen:
            raise TruncatedListing(
                f"ARM served a nextLink it had already served ({next_url}); "
                f"the listing cannot be completed"
            )
        seen.add(next_url)

        kwargs = {"headers": headers, "timeout": timeout}
        # Only when there are any: `nextLink` is fully qualified, and the two
        # datastore callers pass their api-version inline, so sending
        # `params=None` where nothing was sent before would change the call
        # signature every existing fake in the suite is written against.
        if next_params:
            kwargs["params"] = next_params
        resp = requests_mod.get(next_url, **kwargs)
        resp.raise_for_status()
        body = resp.json()

        value = body.get("value") if isinstance(body, dict) else None
        if not isinstance(value, list):
            raise TruncatedListing(
                f"ARM returned a body with no usable `value` list for {next_url}; "
                f"nothing was stated about the collection"
            )
        items.extend(value)

        next_url = body.get("nextLink")
        next_params = None
        if not next_url:
            return items

        # A `nextLink` decides the next URL this loop sends the caller's ARM
        # bearer token to, and until now it decided it with no constraint at
        # all: any host, any scheme. Executed against a fake serving
        # `"nextLink": "http://evil.example.invalid/steal?p=2"`, both hops went
        # out as `Authorization='Bearer <ARM token>'`, the second over plaintext
        # to a host that is not ARM, and that host's rows were then returned as
        # the rest of the listing:
        #
        #     https://management.azure.com/...  Authorization='Bearer FAKE-ARM-TOKEN'
        #     http://evil.example.invalid/...   Authorization='Bearer FAKE-ARM-TOKEN'
        #     rows: [{'id': 'real-row'}, {'id': 'row-from-a-host-that-is-not-arm'}]
        #
        # Framed honestly: real ARM emits neither, so this is a HARDENING gap
        # with no demonstrated live trigger -- it needs a compromised or
        # MITM'd management endpoint, or a proxy rewriting response bodies --
        # not a leak anybody has measured. Two things make it worth closing
        # now. Round 8 newly routed BOTH the money path (`read_orphans`) and the
        # RBAC path (`ArmRoleAuth.list_roles`, whose caller can go on to PUT a
        # role assignment) through this one helper, so seven call sites now
        # depend on it. And the SDK installed in this repo already refuses half
        # of it: azure-core 1.41.0's `_enforce_https`
        # (pipeline/policies/_authentication.py:83) raises `ServiceRequestError`
        # -- "Bearer token authentication is not permitted for non-TLS
        # protected (non-https) URLs." -- before any bearer token is attached.
        # This was parity debt against a library sitting in `.venv`.
        #
        # Compared against the ORIGINAL url rather than an allowlist of ARM
        # hostnames, which is what keeps `management.usgovcloudapi.net` and
        # `management.chinacloudapi.cn` working: whatever host the caller
        # decided to trust is the host the rest of its listing may come from.
        # A mismatch is a truncation, not a fatal error, because it is exactly
        # the state `TruncatedListing` names -- ARM said there was more of the
        # list and it could not be read -- so it lands in every caller's
        # existing could-not-look handler instead of a new parallel path.
        hop = urlsplit(next_url)
        here = (origin.scheme.lower(), origin.netloc.lower())
        if (hop.scheme.lower(), hop.netloc.lower()) != here:
            raise TruncatedListing(
                f"ARM served a nextLink pointing at {hop.scheme}://{hop.netloc} while the "
                f"listing was requested from {origin.scheme}://{origin.netloc}; refusing to "
                f"send the request's credentials off the host the caller named"
            )

    raise TruncatedListing(
        f"listing at {url} had not ended after {MAX_ARM_PAGES} pages; refusing to "
        f"call a partial read a complete one"
    )


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
    #: `properties.allowSharedKeyAccess`. `None` means "not read". `False` is
    #: the hardened posture and is only a problem next to an `AccountKey`
    #: datastore -- see `key_auth_refused`.
    allow_shared_key: bool | None = None
    #: Datastores whose `credentials.credentialsType` is `AccountKey`. `None`
    #: means "not read" -- this list comes from a second ARM GET that fails on
    #: its own -- and `[]` means that GET succeeded and found none. Those two
    #: were the same value until round 6; `key_auth_refused` says what it cost.
    key_based_datastores: list[str] | None = None

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

    @property
    def key_auth_refused(self) -> bool | None:
        """The account refuses the key its own datastores present.

        Three answers, not two. `None` means the question could not be
        answered: `allowSharedKeyAccess` came back a measured `False`, and the
        datastore listing -- the other half of the conjunction -- was not read.

        That third answer is a deliberate narrowing of the rule this docstring
        used to state, "an unread property is never a blocker". The rule stands
        for the network posture, where an unread property is one nothing ever
        got a value for. It was wrong here, and the difference was executed:
        with this list defaulting to `[]`, "the listing 403'd" and "the listing
        succeeded and found none" were the same value, so one failed ARM GET
        flipped this property and `deploy_online`'s `storage_blocker` gate went
        ahead. Same fake account both runs, only the listing differing --
        listing 200 -> True -> RuntimeError, refused; listing 403 -> False ->
        proceeded into the $4.959/hr rollout that docs/JOURNAL.md S57.8 says
        ends at artifacts=0 with no container logs. See
        `_credential_unread_blocker` for why the unread half refuses the deploy
        instead of degrading to "fine", which is the half of this that is a
        judgement call rather than a bug fix.

        `allow_shared_key is None` still answers `False` rather than `None`,
        and that is not an inconsistency to tidy up later: blindness is only
        reported where it could change an answer. With `allowSharedKeyAccess`
        anything but a measured `False`, the account does not refuse keys, so
        what the datastores present cannot make this True and an unread listing
        beside it costs nothing. Reporting every unread read regardless is the
        over-correction that makes a check nobody can pass -- if the rule for
        that first half is ever revisited, this cell moves with it.
        """
        if self.allow_shared_key is not False:
            return False
        if self.key_based_datastores is None:
            return None
        return bool(self.key_based_datastores)


def _network_blocker(state: StorageReachability) -> str | None:
    """Return why nothing can reach workspace storage over the network, or None.

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


def _credential_blocker(state: StorageReachability) -> str | None:
    """Return why the datastore's credential is refused by the account, or None.

    Orthogonal to `_network_blocker`, which is the whole point. A key is refused
    on the public endpoint and over a private link alike, so none of the three
    arrangements that satisfy the network check say anything about this one.
    Measured on `mlw-ffsft-plc` (docs/JOURNAL.md S58): two healthy private
    endpoints, an isolated workspace, the network check green -- and every write
    still returned `KeyBasedAuthenticationNotPermitted`.
    """
    refused = state.key_auth_refused
    if refused is None:
        return _credential_unread_blocker(state)
    if not refused:
        return None
    # Narrowed to True above, so the listing was read and is non-empty.
    stores = ", ".join(state.key_based_datastores or ())
    return "\n".join(
        [
            f"workspace storage account '{state.account_name}' refuses the credential "
            f"its own datastores present:",
            f"  allowSharedKeyAccess=false, but {stores} authenticate with "
            f"credentialsType=AccountKey.",
            "",
            "Every write fails the same way -- job log upload, artifact upload, output",
            "mounts, and client-side jobs.download() -- so runs finish with artifacts=0",
            "and there is nothing to register as a model. A managed online deployment",
            "stages through the same account and hangs in 'Creating'.",
            "",
            "Private endpoints and role assignments do not fix this; the key is refused",
            "before either is consulted.",
            "",
            "Fix: PATCH the WORKSPACE with properties.systemDatastoresAuthMode='identity'.",
            "That is the real lever -- it rewrites all four system datastores at once, so",
            "PUTing them one by one just loses to the workspace setting the next time it",
            "is applied. Use a PREVIEW api-version: the stable one does not return this",
            "field, so a stable-version GET reads 'None' forever and every check of the",
            "change looks like it did not take (docs/JOURNAL.md S62.7, S63).",
            "",
            "Then grant the workspace MSI, the cluster identity and yourself Storage Blob",
            "Data Contributor on the account. A cluster created later gets a new identity",
            "and needs the same grant.",
            "",
            "Pass force=True to deploy anyway.",
        ]
    )


def _credential_unread_blocker(state: StorageReachability) -> str:
    """Return why an *unread* datastore list stops this deployment.

    This is the one read in this module that refuses a deployment by not having
    an answer, and it is a deliberate exception to the rule the rest of the file
    follows. Two reasons, and the first one is the one that decides it.

    The mistakes are not the same size. Proceed on the unread half and the run
    is the one docs/JOURNAL.md S57.8 describes: over an hour of a $4.959/hr GPU
    in `Creating`, no container logs (Azure withholds them until a deployment is
    terminal), artifacts=0 at the end, and nothing in the output that points at
    storage. Refuse on the unread half and the operator loses one command, to a
    message that names the missing permission and offers `force=True` in the
    same breath. "An unread property is never a blocker" was written where both
    outcomes were cheap; this one gates money, and the cheap direction moved.

    And the half that WAS measured is the half that makes the other one fatal.
    `allowSharedKeyAccess=false` is precisely the posture under which one
    surviving `AccountKey` datastore ends every write in
    `KeyBasedAuthenticationNotPermitted`. So this is not "unknown, assume the
    worst" -- it is "measured hardened, and the question of whether anything
    still presents a key went unanswered". Where that first half is unread too,
    `key_auth_refused` never reaches this function.
    """
    return "\n".join(
        [
            f"workspace storage account '{state.account_name}' has "
            f"allowSharedKeyAccess=false, and its datastore list could not be read:",
            "  so whether any datastore still authenticates with "
            "credentialsType=AccountKey is UNKNOWN, and on this account that is the",
            "  single fact that decides whether the deployment can write anything.",
            "",
            "If one still does, every write returns KeyBasedAuthenticationNotPermitted",
            "-- job log upload, artifact upload, output mounts and jobs.download() alike",
            "-- so the rollout hangs in 'Creating' with no container logs and the GPU",
            "bills the whole time, and the run ends with artifacts=0.",
            "",
            "This refuses instead of assuming the list is empty because the two possible",
            "mistakes cost different amounts: an hour of a GPU against one re-run.",
            "",
            "Fix: re-run with a principal that can list the workspace's datastores --",
            "Reader on the workspace is enough -- and the answer becomes measured either",
            "way. The read's own error is in this module's debug log.",
            "",
            "Pass force=True to deploy anyway.",
        ]
    )


def storage_blocker(state: StorageReachability) -> str | None:
    """Return why Azure ML cannot use workspace storage, or None if it can.

    Two independent things can break it and they are reported together. Naming
    only the first sends the caller through a fix-verify-fix round trip for a
    blocker that was already visible in the same two reads.
    """
    network = _network_blocker(state)
    credential = _credential_blocker(state)
    if credential and network:
        return f"{credential}\n\nA second, independent blocker is also present:\n\n{network}"
    return credential or network


def read_storage_reachability(target, *, credential=None) -> StorageReachability | None:
    """Read the live facts for `target`'s workspace, or None if unreadable.

    Every failure path returns None rather than raising. This is a preflight
    check: it may prevent a doomed deployment, but it must never be the reason a
    workable one does not happen.

    A *partial* read is a different thing and is no longer flattened into a
    clean one. When the account answers but the datastore listing does not,
    `key_based_datastores` stays `None` and `key_auth_refused` reports that it
    could not answer rather than answering "none are key-based" -- which is what
    it did, and what let a 403 on one ARM GET wave through the deployment its
    sibling read had already refused. Total silence still returns None here,
    because knowing nothing at all is not the same as measuring the hardened
    posture and then going blind on the half that makes it fatal.
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

        # Read separately, and separately answerable: this is its own ARM GET
        # and it 403s on its own. It used to degrade to "none are key-based",
        # which is a measurement nobody took -- `keyed` stays None unless the
        # listing actually came back. See `key_auth_refused`.
        keyed: list[str] | None = None
        try:
            # Every page, not the first one. `read_all_arm_pages` raises rather
            # than returning a short list, so a truncated listing lands in the
            # handler below and leaves `keyed` at None -- the same answer a 403
            # gets, because it is the same fact. See S78.2.
            keyed = sorted(
                d.get("name", "")
                for d in read_all_arm_pages(
                    requests,
                    f"{base}/datastores?api-version=2024-10-01",
                    headers=headers,
                    timeout=30,
                )
                if ((d.get("properties") or {}).get("credentials") or {}).get(
                    "credentialsType"
                )
                == "AccountKey"
            )
        except Exception as exc:  # noqa: BLE001 - see above
            # `keyed` is deliberately left None: this is the "could not look"
            # branch, and the caller has to be able to tell it from an empty
            # listing. log.debug is not a channel `deploy_online` reads.
            log.debug("preflight could not read datastore credentials: %s", exc)

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
            allow_shared_key=sa_props.get("allowSharedKeyAccess"),
            key_based_datastores=keyed,
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
    #: False when ARM said the SKU catalogue had more pages and they could not
    #: be read. Deliberately NOT the same fact as `restrictions is None`, which
    #: means "nothing was read at all": here part of the catalogue was read and
    #: we positively know the scan did not finish, so "the SKU is not in it" is
    #: a claim the read does not support. The old code made that claim anyway --
    #: `log.warning("SKU %s is not offered at all in %s")` after one page.
    scan_complete: bool = True

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


def sku_advisory(state: SkuAvailability | None) -> str | None:
    """Report what `restrictions` says, or None when it says nothing.

    Deliberately not a blocker. `Standard_NC24ads_A100_v4` is restricted
    `Location`/`NotAvailableForSubscription` across the whole of koreacentral,
    and it is the cluster that fine-tuned a 27B model there. Enforcing this
    field would refuse the only GPU configuration this subscription can run.

    Returns None when `state` is None or its restrictions were never read.
    "Not measured" is not a finding.

    One thing it does report without enforcing: a catalogue scan that stopped
    short. That is not "not measured", it is "measured part of it and stopped",
    and saying nothing about it is how the old code turned one page into "not
    offered at all in <region>". This caller keeps the LowPriority escape hatch
    the online one does not have, so it says so rather than refusing. S78.3.
    """
    if state is not None and not state.scan_complete:
        return (
            f"the Microsoft.Compute/skus catalogue for '{state.region}' stopped "
            f"short of the end, so whether '{state.sku}' carries a "
            f"NotAvailableForSubscription restriction there was NOT established. "
            f"This is a gap, not a finding: treat it as unknown, not as clear."
        )
    if state is None or state.restrictions is None:
        return None

    if state.region_blocked:
        scope = f"across the whole of '{state.region}'"
    elif state.blocked_zones and not state.usable_zones and state.zones:
        scope = (
            f"in every zone of '{state.region}' "
            f"({', '.join(sorted(state.blocked_zones))})"
        )
    else:
        return None

    return (
        f"'{state.sku}' is marked NotAvailableForSubscription {scope}. "
        f"This is not conclusive: the field describes on-demand dedicated "
        f"eligibility, and LowPriority/Spot allocates from a separate pool "
        f"that ignores it -- this subscription trains on an A100 carrying "
        f"exactly this restriction. Treat it as one signal if the rollout "
        f"stalls in 'Creating' with no container logs, alongside quota and "
        f"regional capacity."
    )


class RestrictedSkuError(RuntimeError):
    """A managed online endpoint was asked for a SKU it cannot be given.

    Distinct from the advisory `sku_advisory` returns, and deliberately fatal.
    See `online_endpoint_blocker`.
    """


def online_endpoint_blocker(state: SkuAvailability | None) -> str | None:
    """Refuse a managed online deployment the scheduler can never place.

    `sku_advisory` reports the same field without enforcing it, and that is
    correct where it is used: AmlCompute defaults to LowPriority, Spot allocates
    from a pool that ignores `NotAvailableForSubscription`, and this
    subscription fine-tunes a 27B model on an A100 restricted `Location` across
    the whole region. Enforcing it there would refuse the only GPU configuration
    that works.

    Managed online endpoints have no such escape hatch. They reject LowPriority
    outright, so every node they get is on-demand dedicated -- exactly what the
    restriction describes. The advisory's own caveat ("LowPriority/Spot
    allocates from a separate pool that ignores it") is therefore true of the
    training path and false here, and collapsing the two is what made this field
    look inconclusive when for this one caller it is decisive.

    The cost of not enforcing it, measured: five rollouts, none of which got a
    node, none of which produced a container log, at 50-113 minutes each. The
    last two were preceded by this exact advisory being logged and read.

    Returns None when nothing was measured -- "could not look" is never a
    finding -- or when a zone remains to land in.

    ONE narrow exception, and it is a change to that rule, so it is argued
    here. A catalogue scan that ARM told us was incomplete is not "nothing was
    measured": we read part of the catalogue and know we did not finish it, and
    the branch that used to consume that state announced `not offered at all in
    <region>` and returned the same object a total read failure returns -- so
    the one caller that cannot fall back to Spot proceeded in silence. The two
    mistakes are not the same size here: five rollouts have been spent on this
    field at 50-113 minutes each with no node and no container log, while
    refusing costs one command and `force=True` is offered in the same
    sentence. A total read failure still blocks nothing (`state is None`), and
    a completed scan behaves exactly as before.
    """
    if state is not None and not state.scan_complete:
        return (
            f"the Microsoft.Compute/skus catalogue for '{state.region}' could not "
            f"be read to the end, so whether '{state.sku}' is refused there is "
            f"unknown -- and a managed online endpoint has no LowPriority pool to "
            f"fall back on if it is. This is a gap, not a measured restriction: "
            f"the previous code reported the same state as '{state.sku} is not "
            f"offered at all in {state.region}' and let the rollout go. Re-run to "
            f"get a complete listing, or pass force=True to deploy unverified."
        )
    if state is None or state.restrictions is None:
        return None
    if not state.region_blocked and not (
        state.zones and state.blocked_zones and not state.usable_zones
    ):
        return None

    scope = (
        f"across the whole of '{state.region}'"
        if state.region_blocked
        else f"in every zone of '{state.region}' "
        f"({', '.join(sorted(state.blocked_zones))})"
    )
    return (
        f"'{state.sku}' is marked NotAvailableForSubscription {scope}, and a "
        f"managed online endpoint cannot use LowPriority/Spot -- so unlike an "
        f"AmlCompute cluster it has no pool that ignores this. The rollout "
        f"would sit in 'Creating' at 0% for roughly two hours, produce no "
        f"container logs because no container is ever created, and end in "
        f"InternalServerError. Choose a SKU with an unrestricted zone, or pass "
        f"force=True to spend the two hours anyway."
    )


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
        # `Microsoft.Compute/skus` is paginated too (`ResourceSkusResult` carries
        # the same `nextLink`), and the branch below states a FULL-SCAN NEGATIVE
        # -- "not offered at all in <region>". Reading one page and then saying
        # that is exactly this round's invariant, so the scan is either finished
        # or reported unfinished. S78.3.
        entries: list[dict]
        scan_complete = True
        try:
            entries = read_all_arm_pages(
                requests,
                f"https://management.azure.com/subscriptions/{subscription_id}"
                f"/providers/Microsoft.Compute/skus",
                params={"api-version": "2021-07-01", "$filter": f"location eq '{region}'"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=60,
            )
        except TruncatedListing as exc:
            log.warning(
                "the SKU catalogue for %s stopped short of the end (%s); "
                "whether %s is offered there was not established",
                region, exc, sku,
            )
            entries, scan_complete = [], False

        for entry in entries:
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

        if not scan_complete:
            # The scan did not finish, so "not offered at all" is unsayable.
            # `scan_complete=False` is what carries that to the two readers
            # instead of the silence a bare `restrictions=None` produced -- it
            # was byte-identical to a total read failure, so nothing reached an
            # operator at all.
            return SkuAvailability(
                sku=sku, region=region, restrictions=None, zones=[], scan_complete=False
            )

        # Offered nowhere in this region is a different fact from restricted,
        # and not one this check is entitled to turn into a blocker. Reachable
        # only after every page was read.
        log.warning("SKU %s is not offered at all in %s", sku, region)
        return SkuAvailability(sku=sku, region=region, restrictions=None, zones=[])
    except Exception as exc:  # pragma: no cover - network path
        log.debug("could not read SKU availability for %s in %s: %s", sku, region, exc)
        return None


# ---------------------------------------------------------------------------
# Where did we look?
#
# Not a storage check, but the same rule one step earlier: a report that never
# names its scope cannot be read, and "nothing here" is the reading it produces.
# It lives in this file rather than beside either caller because TWO commands
# print it -- `ffsft-lifecycle status` and `ffsft-deploy check` -- and a second
# copy of the string is a second chance to word it wrongly for one of them, which
# is exactly what happened: `check` printed `subscription <id> / <location>`, an
# unlabelled region it does not query that way, and named neither the resource
# group nor the workspace it was about to query. This file is the pure,
# Azure-free half of the deploy split (CLAUDE.md), so both callers reach it
# without either importing the other's CLI module.
# ---------------------------------------------------------------------------

#: Printed in place of the scope when a report is rendered with no target -- a
#: hand-built inventory, or a caller that forgot. Saying so is the point: the
#: whole defect this header exists for is a report that quietly did not say
#: where it read.
UNIDENTIFIED_SCOPE = "LOOKED IN: unrecorded -- this report cannot name the workspace it read."

#: The trailing lines of :func:`scope_lines`, supplied by the caller and not
#: defaulted, because the three identity lines above them are shared and the
#: scope they describe is NOT. Getting this clause wrong is the whole defect
#: class, so each caller has to state which reads its report is made of:
#:
#:   `status` reads the AML client (subscription + resource group + workspace)
#:   and an ARM scan of the resource group. Neither is sent a location.
#:
#:   `check` reads the AML client too, and `read_dedicated_quota`, which is
#:   `/locations/{location}/providers/Microsoft.Quota`. There the location IS
#:   part of the query, so repeating "not sent" would send a participant whose
#:   quota was granted in another region looking anywhere but at the region.
#:
#: `{loc}` and `{rg}` are filled in from the target.
AML_CLIENT_SCOPE: tuple[str, ...] = (
    "that triple is what get_ml_client sends, and it scopes every row that came back",
    "through it. LEFTOVERS does not: it is a separate ARM scan of resource group {rg},",
    "same subscription, no workspace. FFSFT_LOCATION={loc} is sent by neither, so it",
    "does not scope this read and cannot explain a missing resource.",
)

#: See :data:`AML_CLIENT_SCOPE`. `check` is the one report where the location is
#: load-bearing: `read_dedicated_quota` 404s per region and returns 0, so a
#: dedicated grant that exists in another region reads here as no quota at all.
QUOTA_SCOPE: tuple[str, ...] = (
    "that triple is what get_ml_client sends, and it scopes the datastore and cluster",
    "probes. Dedicated quota is read per region instead: FFSFT_LOCATION={loc} scopes",
    "those rows, and a grant held in another region reads as 0 here.",
)


def scope_lines(target: AzureTarget | None, note: tuple[str, ...]) -> list[str]:
    """Name the workspace that answered, above the report.

    "BILLING NOW: nothing" means "not HERE", and the report never said where
    HERE was. A status run against the wrong resource group -- the easy mistake,
    because FFSFT_RESOURCE_GROUP silently defaults to rg-ffsft-kc rather than
    failing -- printed the same reassuring line as a real teardown.

    The second line is not decoration. `get_ml_client` passes subscription_id,
    resource_group_name and workspace_name and nothing else, so those three name
    the workspace read; FFSFT_LOCATION is read into the target and never reaches
    it. Printing the location unlabelled -- which is all `ffsft-deploy check`
    used to print -- invites the wrong theory, that the report is region-scoped
    and a resource elsewhere is simply missing from it.

    `note` is required rather than defaulted because the identity lines are the
    shared part and the scope is not: the first wording of this header said "that
    triple is the whole query" above a table that also carries resource-group ARM
    rows, which overstated its coverage in the one direction that hides a leak.
    """
    sub = str(getattr(target, "subscription_id", "") or "")
    rg = str(getattr(target, "resource_group", "") or "")
    ws = str(getattr(target, "workspace_name", "") or "")
    if not (sub or rg or ws):
        return [UNIDENTIFIED_SCOPE]
    loc = str(getattr(target, "location", "") or "(unset)")
    lines = [
        f"LOOKED IN: workspace {ws or '(unset)'}   resource group {rg or '(unset)'}",
        f"           subscription {sub or '(unset)'}",
    ]
    lines += [f"           {line.format(loc=loc, rg=rg or '(unset)')}" for line in note]
    return lines
