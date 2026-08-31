"""Preflight probes: ask Azure, rather than assume.

Split out of `endpoint.py`. Every function here answers "would this actually
work?" before anything expensive is created, by making the cheapest real call
that can return a real answer -- a quota read, a `min_instances=0` cluster
created and immediately deleted, a storage account's own network and key
properties. Each one replaced an assumption that had already cost a rollout.

This is the *live* half of preflight; `preflight.py` holds the pure classifiers
that turn a read state into a blocker string. `check_pattern` is the entry
point that combines them.

Azure SDK imports stay function-local, per the docstring of
`tests/test_aml_job.py`: tests inject fakes by monkeypatching the attribute on
the module the caller reaches for, which for `check_pattern` is *this* module.

`endpoint.py` re-exports every public name here.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import textwrap
from collections.abc import Sequence

from .registry import get_serving_registry
from .spec import ServingSpec

log = logging.getLogger("ffsft.deploy.probes")


def read_dedicated_quota(subscription_id: str, location: str, family: str) -> int:
    """Read the *measured* dedicated-core limit for one VM family.

    Uses the Microsoft.Quota provider rather than the AML usages API on purpose:
    the AML usages endpoint reports `-1` for families that have no dedicated
    allocation, which reads like 'unlimited' and is the opposite of the truth.
    """
    import requests
    from azure.identity import DefaultAzureCredential

    cred = DefaultAzureCredential()
    token = cred.get_token("https://management.azure.com/.default").token
    scope = (
        f"subscriptions/{subscription_id}/providers/Microsoft.MachineLearningServices"
        f"/locations/{location}"
    )
    url = (
        f"https://management.azure.com/{scope}/providers/Microsoft.Quota"
        f"/quotas/{family}?api-version=2023-02-01"
    )
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    if resp.status_code == 404:
        log.warning("quota family '%s' is not defined in %s", family, location)
        return 0
    resp.raise_for_status()
    return int(resp.json()["properties"]["limit"]["value"])


@dataclasses.dataclass(frozen=True)
class SkuProbe:
    """What the control plane said when actually asked to create the cluster.

    Two fields exist because the probe can end without an answer. `probed=False`
    means the control plane was never asked -- the probe refused to touch a name
    it did not own, or could not read whether it owned it -- so `creatable=False`
    there records "not measured", never "refused"; the detail says so in words
    because that is what a human reads. `leftover` is non-empty when a compute
    this call created could not be confirmed gone.

    Both reach the operator through `blocker` for any caller that wants one
    string. `endpoint.cmd_check` no longer does: it prints `probe_report`
    instead, because a table cell has to say which of the three things happened
    and `blocker` deliberately flattens them into one.
    """

    sku: str
    tier: str
    creatable: bool
    code: str
    detail: str
    probed: bool = True
    leftover: str = ""

    @property
    def blocker(self) -> str | None:
        """Everything the operator must act on before trusting this line.

        Wider than "this SKU cannot be created", and deliberately so: a probe
        cluster whose delete failed is a resource left behind by a command whose
        `--help` calls itself free, and `log.warning` is not a channel `check`
        prints. A leftover therefore blocks the line it is attached to even when
        the create itself succeeded.
        """
        if not self.creatable:
            base = f"{self.code}. {self.detail}"
            return f"{base} {self.leftover}" if self.leftover else base
        return self.leftover or None


def _absence_is_proven(exc: BaseException) -> bool:
    """True only when Azure actually said the thing is not there.

    Moved here from `endpoint.py` with the other classifiers (`classify_store`,
    `classify_cluster_error`), which is also what paid for the lines the
    pre-delete cleanup needed to stop reporting a refused DELETE as a failed
    GET -- `endpoint.py` sits under a deliberate line ratchet.

    A 404 is an answer; a 403, a timeout or a DNS failure is the absence of
    one, and they all arrive at the same `except` clause. Unknown is not empty
    -- the inversion `lifecycle.EXIT_COULD_NOT_LOOK` exists for.

    Two checks because a `ResourceNotFoundError` built without an HTTP response
    -- how the suite's fakes build one -- carries `status_code=None`, so the
    duck-typed test alone would warn on every clean first run.
    """
    from azure.core.exceptions import ResourceNotFoundError

    if isinstance(exc, ResourceNotFoundError):
        return True
    return getattr(exc, "status_code", None) == 404


def _summary(text: str) -> str:
    """One table cell's worth of a message that may be a paragraph.

    Lives here rather than in `endpoint.py` because `probe_report` below needs
    the same rule and `endpoint` already imports this module; the reverse import
    would be a cycle.
    """
    return text if len(text) <= 110 else text[:107].rstrip() + "..."


def _wrapped(text: str) -> list[str]:
    """A probe's paragraph, indented under its row instead of inside its cell."""
    return textwrap.fill(text, width=92, initial_indent="      ", subsequent_indent="      ").split(
        "\n"
    )


def probe_report(probe: SkuProbe, key: str, width: int) -> tuple[list[str], str | None]:
    """The `check --probe` line(s) for one probe, plus what it leaves unread.

    Three states, and the caller rendered two of them with the same word. It was
    `if probe.blocker: print(f"{key}  BLOCKED  {probe.blocker}")`, which turned a
    probe that never ran into a verdict about the SKU. Executed against a
    workspace that already owned a compute named `ffsft-probe-0`:

        aks_vllm  BLOCKED  ProbeNameTaken. a compute named 'ffsft-probe-0'
        already exists and this probe did not create it ...
        Standard_NV36ads_A10_v5 was NOT tested at LowPriority

    rc=0. The cell says BLOCKED, the sentence says NOT tested, and nothing was
    added to `cmd_check`'s COULD NOT LOOK list because a blocker is an answer and
    answers are deliberately not collected there. `check --probe && echo ok`
    printed ok over a SKU nobody asked about. So the unread state gets its own
    word and its own footer entry -- that is what the second return value is.

    The third state is a create the control plane accepted whose probe cluster
    could not be confirmed deleted. That was BLOCKED as well, which reads as "you
    cannot deploy this pattern" over a SKU that had just been accepted, and sends
    the operator to pick another SKU when the thing to do is delete a cluster. It
    is an `ok` row now, with the leftover named under it -- and it counts as
    unread too, because a delete that failed is not a resource anyone confirmed
    is gone, which is what `_discard_probe`'s own message says in words.

    Paragraphs are wrapped under the row rather than pasted into the cell. Every
    other row in `cmd_check` runs through `_summary` for that reason; this one
    did not, and a 400-character detail reflowed the column and buried the rows
    that followed it.
    """
    cell = f"  {key:<{width}}  "
    if not probe.probed:
        return (
            [
                f"{cell}UNKNOWN   {probe.code}: {probe.sku} was not tested",
                *_wrapped(probe.detail),
            ],
            f"whether {key} can create {probe.sku} at {probe.tier} ({probe.code})",
        )

    if not probe.creatable:
        lines = [f"{cell}BLOCKED   {_summary(f'{probe.code}. {probe.detail}')}"]
        if len(f"{probe.code}. {probe.detail}") > 110:
            lines += _wrapped(f"{probe.code}. {probe.detail}")
    else:
        lines = [f"{cell}ok        {probe.tier} {probe.sku} (create accepted)"]

    if probe.leftover:
        lines += _wrapped(f"LEFTOVER: {probe.leftover}")
        return lines, f"whether the probe cluster this run created for {key} is gone"
    return lines, None


def classify_cluster_error(message: str) -> tuple[str, str]:
    """Turn an AmlCompute create failure into a code plus an actionable reason.

    The two responses differ in what they ask of you -- one is a support
    ticket, the other is 'pick a different SKU' -- so collapsing them into
    'deployment failed' throws away the only useful part.

    `InvalidPropertyValue` arrives with a list of "supported VM sizes" that is
    old enough to omit `Standard_NC24ads_A100_v4`, the SKU this project trains
    on every day. Repeating it would send the reader looking for a K80. The
    honest summary is that the control plane refuses this SKU here regardless
    of what the catalogue and the quota say.
    """
    if "ClusterMinNodesExceedCoreQuota" in message:
        family = re.search(r"Standard\s+(\w+)\s+family", message)
        quota = re.search(r"quota of (\d+)", message)
        detail = (
            f"dedicated quota for {family.group(1) if family else 'this family'} is "
            f"{quota.group(1) if quota else '0'}. Managed online endpoints are always "
            "dedicated, so no amount of retrying helps -- request a quota increase."
        )
        return "ClusterMinNodesExceedCoreQuota", detail
    if "InvalidPropertyValue" in message:
        sku = re.search(r"value (\S+) for property", message)
        detail = (
            f"{sku.group(1) if sku else 'this SKU'} cannot be created in this "
            "workspace at either tier, however many cores the catalogue and the "
            "usage APIs advertise. Choose a SKU that a real create call accepts."
        )
        return "InvalidPropertyValue", detail
    return "Unknown", message.strip()[:300]


def probe_sku(client, sku: str, tier: str, *, name: str = "ffsft-probe") -> SkuProbe:
    """Ask the control plane to create the cluster, then take it straight back.

    Scope, stated first because this function was read as answering a broader
    question than it does: this creates an **AmlCompute cluster**, so it answers
    "can a training job run on this SKU". It says nothing about a managed online
    endpoint, which is a different resource type on a different control plane.

    Reading it as a deployment probe inverts its answer. In koreacentral all six
    A10 v5 SKUs are MIR-only -- their `supportedComputeTypes` lists MIR and not
    AmlCompute -- so this call refuses precisely the SKUs a managed endpoint
    accepts. JOURNAL 43 concluded "every GPU SKU is NotAvailableForSubscription"
    from exactly that inversion; JOURNAL 51 retracts it, having created an
    endpoint in 69 seconds. For the deployment question, attempt a
    `ManagedOnlineDeployment` -- nothing else is evidence.

    Within its own scope it is the honest answer, and that part still holds:
    quota says yes for A10 v5 and the create call says no; the catalogue lists
    all sixteen GPU SKUs and the create call still says no.

    Free: a refusal returns in about two seconds having created nothing, and an
    acceptance is a `min_instances=0` cluster that allocates no node before it
    is deleted -- and the delete is now awaited, so "deleted" is something this
    function observed rather than something it started.

    This is a destructive caller, and it was read as a read-only one for a long
    time. `begin_create_or_update` is an upsert and `name` comes from the caller
    (`cmd_check` passes `ffsft-probe-{index}`), so against a workspace that
    already owned a cluster of that name the old code re-sized it to the probe's
    settings and then deleted it -- audited output was `created:
    [('ffsft-probe-0', 'Standard_NV36ads_A10_v5', 0)]` followed by `DELETED:
    ['ffsft-probe-0']`. So the name is read before it is written, only a name
    this call created is ever deleted, and a delete that fails is returned to
    the caller instead of being logged where nothing prints it.
    """
    from azure.ai.ml.entities import AmlCompute

    # The one name this call is allowed to delete. It stays False through every
    # refusal path, so a cluster this call did not create cannot reach
    # `_discard_probe`. Ownership is tracked, never inferred from the resource:
    # a cluster somebody else made and one this probe made look identical.
    probe_owns_name = False

    taken, unreadable = _name_is_taken(client, name)
    if taken is not False:
        return _refuse_name(sku, tier, name, unreadable)

    entity = AmlCompute(
        name=name,
        size=sku,
        min_instances=0,
        max_instances=1,
        tier=tier,
        idle_time_before_scale_down=120,
    )
    # Claimed before the call, not after it: the create is an ARM PUT, so a
    # refused create still leaves a compute record behind, and that record is
    # this call's to remove (the cleanup below is exactly that case).
    probe_owns_name = True
    try:
        client.compute.begin_create_or_update(entity).result()
    except KeyboardInterrupt:
        # `except Exception` does not catch this one, and `.result()` is the
        # long wait in this function -- ~30s of polling against a name the PUT
        # has already written. Ctrl-C there used to unwind straight past the
        # discard below, leaving a real AmlCompute named `ffsft-probe-N` in a
        # workspace, created by a command whose own help calls it free ("a
        # refusal creates nothing, an acceptance is deleted"). It holds no nodes
        # at min_instances=0, but it holds the name, so the NEXT `check --probe`
        # refuses that pattern for `ProbeNameTaken` and reports it untested.
        # The cleanup is the same call the normal paths make; the interrupt is
        # re-raised, because swallowing it would mean Ctrl-C did nothing.
        leftover = _discard_probe(client, name)
        print(
            f"\ninterrupted: removing the probe cluster '{name}' this run created."
            + (f"\n{leftover}" if leftover else " it is gone.")
        )
        raise
    except Exception as exc:  # noqa: BLE001 - the message is the whole point
        code, detail = classify_cluster_error(str(exc))
        # A refused create still leaves a compute record in `Failed`. It holds no
        # nodes and bills nothing, but it accumulates, and this project's whole
        # teardown story is that nothing is left behind.
        leftover = _discard_probe(client, name) if probe_owns_name else ""
        return SkuProbe(
            sku=sku, tier=tier, creatable=False, code=code, detail=detail, leftover=leftover
        )

    leftover = _discard_probe(client, name) if probe_owns_name else ""
    return SkuProbe(sku=sku, tier=tier, creatable=True, code="", detail="", leftover=leftover)


def _name_is_taken(client, name: str) -> tuple[bool | None, str]:
    """Is a compute called `name` already there? `(None, why)` when the read failed.

    Read before write, because the write is an upsert and the teardown after it
    is unconditional. A read that fails is not a free name, it is no answer at
    all: this codebase's standing rule that "could not look" may never be
    reported as "looked, saw nothing" applies with the sharpest edge here,
    because acting on the wrong guess overwrites and then deletes a cluster
    belonging to somebody who never ran this command.
    """
    from azure.core.exceptions import ResourceNotFoundError

    try:
        client.compute.get(name)
    except ResourceNotFoundError:
        return False, ""
    except Exception as exc:  # noqa: BLE001 - an unreadable name is not a free one
        log.warning("could not read whether compute '%s' already exists: %s", name, exc)
        # The type carries the next move -- a 403 points at the identity, a
        # timeout points at retrying -- the same reason `lifecycle._section`
        # records the exception type next to the gap it explains.
        return None, f"{type(exc).__name__}: {exc}"
    return True, ""


def _refuse_name(sku: str, tier: str, name: str, unreadable: str) -> SkuProbe:
    """The probe declining to touch a name it cannot prove is free.

    `probed=False` and the wording carry the same thing twice on purpose: the
    control plane was never asked, so this result says nothing about the SKU. A
    refusal rendered as "this SKU cannot be created" would be a finding invented
    out of a value nobody measured, which costs as much as the opposite error.
    """
    if unreadable:
        code = "ProbeNameUnreadable"
        opening = (
            f"could not read whether a compute named '{name}' already exists "
            f"({unreadable}), so nothing was created and nothing was deleted."
        )
    else:
        code = "ProbeNameTaken"
        opening = (
            f"a compute named '{name}' already exists and this probe did not create "
            f"it, so nothing was created and nothing was deleted."
        )
    detail = (
        f"{opening} {sku} was NOT tested at {tier} -- this line is about the name, "
        f"not the SKU. Re-run the probe against a name nobody owns, or remove "
        f"'{name}' yourself first: the create call is an upsert, so it would have "
        f"replaced that cluster's size, tier and scale settings, and the teardown "
        f"that follows it would then have deleted the cluster outright."
    )
    return SkuProbe(sku=sku, tier=tier, creatable=False, code=code, detail=detail, probed=False)


def _discard_probe(client, name: str) -> str:
    """Delete a probe cluster *this call created*; return "" or why it is still there.

    Both halves were paid for. The poller is awaited like all four delete sites
    in `deploy/lifecycle.py`: without `.result()` this returns while the delete
    is still in flight or has already failed server-side, and `check --probe`
    then prints "an acceptance is deleted" on no evidence. And the failure is
    returned rather than logged, because `log.warning` is not a channel
    `ffsft-deploy check` prints -- a leftover nobody is told about is precisely
    the leak the teardown story exists to prevent.
    """
    from azure.core.exceptions import ResourceNotFoundError

    try:
        client.compute.begin_delete(name).result()
    except ResourceNotFoundError:
        # Already absent is the end state this function exists to reach, not a
        # leftover: a create refused before the record was written leaves
        # nothing behind, and reporting that as a leak sends the operator
        # hunting a cluster that was never created.
        return ""
    except Exception as exc:  # noqa: BLE001 - reported to the caller, never swallowed
        log.warning("probe cluster %s could not be deleted: %s", name, exc)
        return (
            f"the probe cluster '{name}' this run created is still there: the delete "
            f"failed with {type(exc).__name__}: {exc}, and nothing here can confirm "
            f"it is gone. It was created with min_instances=0 so it holds no nodes, "
            f"but it does hold the name -- check it with `ffsft-lifecycle status` and "
            f"delete it."
        )
    return ""


@dataclasses.dataclass(frozen=True)
class StoreProbe:
    """Whether a model asset can be created at all, and why not.

    Azure exposes no API that answers "can I register a model?", so this
    reconstructs the answer from the two properties that decide it on the
    workspace's default datastore account.
    """

    account: str
    public_access: str
    private_endpoints: int
    reachable: bool
    detail: str
    #: `True` refused, `False` not refused, `None` the question was not
    #: answered -- the account said `allowSharedKeyAccess=false` and the
    #: datastore listing that decides the other half did not come back. `None`
    #: is not a verdict about the account and must not be rendered as one; it
    #: belongs in `cmd_check`'s COULD NOT LOOK list. Kept the same shape as
    #: `preflight.StorageReachability.key_auth_refused` on purpose: the same
    #: swallow existed in both files and was fixed in one round.
    key_auth_refused: bool | None = False
    #: `None` means the listing was not read; `()` means it was read and found
    #: nothing. Those were the same value until round 6.
    key_based_datastores: tuple[str, ...] | None = None


def classify_store(
    account: str,
    public_access: str,
    private_endpoints: int,
    *,
    allow_shared_key: bool | None = None,
    key_based_datastores: Sequence[str] | None = None,
) -> StoreProbe:
    """Decide whether a storage account is reachable by anything.

    Two ways to be reachable, and they are the only two:

    * the public endpoint is on -- then `networkAcls` decides who gets in, and
      an `Allow` default action lets in the compute node and this laptop alike;
    * the public endpoint is off but a private endpoint exists -- the designed
      hardened posture, where traffic arrives over a private link instead.

    Off with no private endpoint is not a posture, it is an outage. Measured on
    this subscription (§24): three finished training runs each uploaded zero
    artifacts, `mount_outputs=True` fails during node setup, and registering a
    model from a job output returns `NoMatchingArtifactsFoundFromJob` -- all one
    cause. An ARM `PATCH` setting `publicNetworkAccess: Enabled` returns 200 and
    changes nothing, and a *newly created* account asked for `Enabled` comes
    back `Disabled`, so this is enforced above the subscription and cannot be
    fixed from here.

    Network reachability is necessary and *not* sufficient. A datastore also
    names how to authenticate, and that is a separate axis this check was blind
    to until polandcentral (S57.8): `mlw-ffsft-plc` sat behind two working
    private endpoints -- reachable by the rule above, and this function said so
    -- while every write still failed, because all four of its datastores were
    created with `credentialsType: AccountKey` against a storage account with
    `allowSharedKeyAccess: false`. The account refuses the key the datastore
    insists on presenting, so job log upload, artifact upload, output mounts and
    client-side `jobs.download()` all return `KeyBasedAuthenticationNotPermitted`
    -- the *same* zero-artifact symptom as an unreachable account, from a cause
    no amount of private endpoints or RBAC can fix. Two workspaces created the
    same way disagreed on this: koreacentral came up `None`, polandcentral came
    up `AccountKey`, so it cannot be assumed from the deployment path either.

    Anything this function cannot read reports reachable. A probe that cannot
    see is not the same as a resource that is broken, and the expensive mistake
    in this project has consistently been turning the former into the latter.
    That is why `allow_shared_key=None` (unread) never fails the check: only a
    measured `False` alongside a measured `AccountKey` datastore does.

    Reporting reachable is not the same as reporting nothing, and that is what
    the third `key_auth_refused` state is for. `key_based_datastores=None` means
    the listing was not read at all; next to a measured `allowSharedKeyAccess:
    false` the credential axis then has no answer, and this returns
    `key_auth_refused=None` for `cmd_check` to put in its COULD NOT LOOK list.
    `reachable` deliberately stays the network answer there -- flipping it would
    print UNREACHABLE, which is a claim about the account nothing here measured.
    An unread listing next to anything other than a measured `false` is not
    reported at all: it cannot change the answer, so it is not a blind spot.
    """
    key_based = None if key_based_datastores is None else tuple(key_based_datastores)

    if public_access != "Disabled":
        net_ok, net_detail = True, ""
    elif private_endpoints > 0:
        net_ok, net_detail = (
            True,
            (f"{account}: public access off, reached over {private_endpoints} private endpoint(s)"),
        )
    else:
        net_ok, net_detail = (
            False,
            (
                f"no reachable datastore: '{account}' has publicNetworkAccess=Disabled "
                f"and 0 private endpoints, so neither this client nor the Azure ML "
                f"compute node can open a session against it. Job outputs never upload "
                f"(artifacts=0 on every finished run), so there is nothing to register "
                f"as a model -- and every hosted pattern deploys a model asset. "
                f"Fix: attach a private endpoint to the account and put the compute in "
                f"that VNet. Turning public access back on is rejected silently by "
                f"tenant-level enforcement."
            ),
        )

    if allow_shared_key is False and key_based is None:
        # Could not look. Not a blocker (nothing measured says the datastores
        # present a key) and not a pass (nothing measured says they do not).
        # The row, the COULD NOT LOOK block and the exit code are `cmd_check`'s
        # to print -- it branches on `store.key_auth_refused is None`, inlined
        # there rather than given a helper because endpoint.py is under a line
        # ratchet (tests/test_deploy_module_split.py).
        return StoreProbe(account, public_access, private_endpoints, net_ok, net_detail, None, None)

    if allow_shared_key is False and key_based:
        # Reported even when the network posture passes, because it is
        # orthogonal to it: the key is refused on the public endpoint and over a
        # private link alike, so a green network answer says nothing about this.
        detail = (
            f"datastore credential mismatch: '{account}' has "
            f"allowSharedKeyAccess=false, but datastore(s) {', '.join(key_based)} "
            f"authenticate with credentialsType=AccountKey. Every write fails "
            f"with KeyBasedAuthenticationNotPermitted -- job logs, artifacts, "
            f"output mounts and jobs.download() alike -- so runs finish with "
            f"artifacts=0 and there is nothing to register as a model. Private "
            f"endpoints and role assignments do not fix this. Fix: PUT each "
            f"datastore with credentials.credentialsType='None' (identity-based) "
            f"and grant the workspace MSI, the cluster identity and yourself "
            f"Storage Blob Data Contributor on the account. Keep isDefault=true "
            f"on the workspace default datastore or the PUT is rejected."
        )
        if not net_ok:
            # Both broken at once (measured on `mlw-ffsft-jpe`). Reporting only
            # the first sends the caller through a fix-verify-fix round trip for
            # a blocker that was already visible here.
            detail += f" A second, independent blocker is also present -- {net_detail}"
        return StoreProbe(account, public_access, private_endpoints, False, detail, True, key_based)

    return StoreProbe(
        account, public_access, private_endpoints, net_ok, net_detail, False, key_based
    )


def _key_based_datastores(root: str, workspace: str, head: dict) -> list[str] | None:
    """Names of datastores that authenticate with an account key, or None.

    `None` is "could not look" and `[]` is "looked, none are key-based". This
    returned `[]` for both, and `classify_store` reads the empty list as the
    measured absence of the S57.8 blocker, so a 403 on this one GET rendered a
    workspace nobody could see as strictly cleaner than a broken one. Executed
    with everything else stubbed clean, the listing the only variable:

        listing readable = True   -> datastore  UNREACHABLE  stffsftkc ...
        listing readable = False  -> no datastore line at all, rc=0, `check &&
                                     echo ok` printed ok

    `raise_for_status` is here for the same reason and was the worse half: with
    no status check a 403 whose body still parses as JSON never raised at all,
    so `page.get("value")` returned `[]` and not even the `log.warning` below
    fired. And `log.warning` is stderr -- not the structured report, not the
    COULD NOT LOOK block whose own prose claims exhaustiveness, and not the exit
    code -- so it was never the whole answer either way.
    """
    import requests

    from .preflight import read_all_arm_pages

    try:
        # `page` was the right name and the wrong number of them. An ARM list
        # answers one page at a time and says `nextLink` when there are more;
        # reading only the first is a successful read of part of the list, and
        # it landed on `[]` -- the value the sentinel above reserves for
        # "measured, and none of them present an account key". Executed, the
        # only variable being whether ARM paged the same four datastores:
        #
        #     one page   -> `check` printed  datastore  UNREACHABLE  ...
        #     paginated  -> no datastore row at all, rc 0, `check && echo ok`
        #                   printed ok
        #
        # `read_all_arm_pages` raises on a listing it cannot finish, so a
        # truncation lands in the same handler a 403 does and answers None.
        # S78.2.
        return sorted(
            d["name"]
            for d in read_all_arm_pages(
                requests,
                f"{root}/Microsoft.MachineLearningServices/workspaces/"
                f"{workspace}/datastores?api-version=2024-10-01",
                headers=head,
                timeout=60,
            )
            if ((d.get("properties") or {}).get("credentials") or {}).get("credentialsType")
            == "AccountKey"
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable list must not block
        # Still not a blocker, and no longer a clean pass either: None makes the
        # caller say it could not look. See the docstring.
        log.warning("could not read datastore credentials: %s", exc)
        return None


def probe_model_store(target) -> StoreProbe:
    """Read the live posture of the workspace's default datastore.

    Free and read-only: three ARM GETs, no resource is created or touched. Two
    independent things can make the datastore unusable -- the account being
    unreachable, and the datastore presenting a credential the account refuses
    -- so both are read here and both are handed to `classify_store`.
    """
    import requests
    from azure.identity import AzureCliCredential

    cred = AzureCliCredential()
    tok = cred.get_token("https://management.azure.com/.default").token
    head = {"Authorization": f"Bearer {tok}"}
    root = (
        f"https://management.azure.com/subscriptions/{target.subscription_id}"
        f"/resourceGroups/{target.resource_group}/providers"
    )
    try:
        ws = requests.get(
            f"{root}/Microsoft.MachineLearningServices/workspaces/"
            f"{target.workspace_name}?api-version=2024-10-01",
            headers=head,
            timeout=60,
        ).json()
        account_id = ws["properties"]["storageAccount"]
        account = account_id.rsplit("/", 1)[-1]
        sa = requests.get(
            f"https://management.azure.com{account_id}?api-version=2023-05-01",
            headers=head,
            timeout=60,
        ).json()["properties"]
        return classify_store(
            account,
            sa.get("publicNetworkAccess", "Unknown"),
            len(sa.get("privateEndpointConnections") or []),
            allow_shared_key=sa.get("allowSharedKeyAccess"),
            key_based_datastores=_key_based_datastores(root, target.workspace_name, head),
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable probe must not block
        log.warning("could not read the datastore posture: %s", exc)
        return classify_store("unknown", "Unknown", 0)


def quota_family_for(sku: str | None) -> str | None:
    """Dedicated quota family `sku` bills against, or None if unknown.

    Unknown returns None rather than a guess so the caller falls back to the
    pattern's declared family -- the same reason `required_dedicated_cores`
    raises instead of assuming a core count.
    """
    if not sku:
        return None
    from ffsft.azure_ml import GPU_SKUS

    entry = GPU_SKUS.get(sku)
    return entry.get("family") if entry else None


def check_pattern(
    pattern_key: str,
    subscription_id: str,
    location: str,
    *,
    sku: str | None = None,
    instances: int = 1,
    store: StoreProbe | None = None,
    from_hub: bool = False,
) -> tuple[ServingSpec, str | None]:
    """Return the spec plus a human-readable blocker, or None if it can deploy.

    `from_hub` declares that the weights will come from the Hugging Face Hub at
    container start. For a pattern whose server resolves its own model that
    takes the datastore out of the picture entirely, so the storage check is
    skipped -- see `ServingSpec.can_serve_from_hub`.
    """
    spec = get_serving_registry().get(pattern_key)
    needs_store = spec.requires_model_asset and not (from_hub and spec.can_serve_from_hub)
    if store is not None and needs_store and not store.reachable:
        # Checked before quota on purpose: no model asset means no deployment of
        # any kind, so leading with a quota number would imply that raising the
        # quota would help.
        return spec, store.detail
    if spec.allows_low_priority or not spec.quota_family:
        return spec, None
    # A `--sku` override can cross quota families. The pattern names the family
    # of its *default* SKU, but Azure bills the family the *chosen* SKU belongs
    # to, so reading `spec.quota_family` here measures a pool the deployment
    # never touches: an A100 SKU was refused in a region with 48 A100 cores
    # granted because the A10 pool it would never use read 0.
    family = quota_family_for(sku or spec.default_sku) or spec.quota_family
    available = read_dedicated_quota(subscription_id, location, family)
    return spec, spec.blocked_reason(available, instances=instances, sku=sku, quota_family=family)
