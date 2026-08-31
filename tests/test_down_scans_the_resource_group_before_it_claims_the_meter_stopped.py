""""meter stopped." was a claim about a resource group `cmd_down` never looked at.

`read_orphans` appeared exactly once in `deploy/lifecycle.py`, at the bottom of
`cmd_status`. `cmd_down` did not call it, so the only command in this repo whose
last line asserts the meter is off asked the AML workspace client and nothing
else -- and that client structurally cannot see a managed disk or a public IP
(`read_orphans`' own docstring says so, and `collect_inventory` could never have
found them).

Executed against the pre-fix module, on ONE fake workspace holding the replayed
$41.66/month leak from JOURNAL §11.4 -- a 256 GB Premium_LRS disk left Unattached
and a Standard public IP still pointing at a NIC whose VM is gone -- plus a live
`Standard_NV36ads_A10_v5` deployment:

    $ status                          rc=0
    !!orphaned-disk      vm-a10-ffsft_OsDisk_1     Premium_LRS   0.052
    !!orphaned-public-ip vm-a10-ffsftPublicIP      Standard      0.005
    LEFTOVERS: 2 resource(s) from deleted VMs, ~$41.66/month for nothing.

    $ down --all --yes                rc=0
    removed:
      - online-endpoint ffsft-a10 (with its deployments)
    meter stopped. `ffsft lifecycle status` to confirm.

Same fake, same second. `status` names both leaks; `down` names neither and then
says the meter stopped. This is the failure mode the code's own comment above the
LEFTOVERS block already named -- "A missing LEFTOVERS block is itself a claim --
'nothing was left behind' -- and it is the claim that hid a $41.66/month leak
once already" -- reached here in its strongest form, because a missing block is a
silence and "meter stopped." is a sentence.

Three things this fix is not allowed to do, each pinned below:

- **Delete them.** `down` reports leftovers and hands over the `az` command. A
  disk cannot be un-deleted and no `up` recreates it, so that call is a human's.
- **Price them into the saving.** The orphan rows stay out of `inv.items`, which
  is what `teardown` walks and what "stops $X/hr" sums. Folding $41.66/month into
  a figure for compute this command really does stop is the same lie in the other
  direction.
- **Claim anything when the scan failed.** No figure, no count, no statement
  about the resource group -- the split `format_inventory` already applies to
  BILLING NOW and LEFTOVERS, applied to the last line of a teardown.

No network and no Azure: the ML client, the ARM credential and `requests.get` are
all faked, so `read_orphans` runs for real over invented JSON.
"""

from __future__ import annotations

import requests

import ffsft.azure_ml
from ffsft.deploy import lifecycle
from ffsft.deploy.lifecycle import EXIT_COULD_NOT_LOOK, EXIT_NOT_IDLE, ORPHANS_SECTION

PRICED_SKU = "Standard_NV36ads_A10_v5"
FORBIDDEN = "(AuthorizationFailed) no Reader on rg-ffsft-kc"

RG = "/subscriptions/s/resourceGroups/rg-ffsft-kc/providers"
#: The real leak, replayed as ARM would return it. The public IP is the trap:
#: it has a perfectly valid `ipConfiguration`, and the NIC it points at is the
#: orphan. Testing `ipConfiguration is None` calls this IP healthy.
LEAKED_DISK = {
    "name": "vm-a10-ffsft_OsDisk_1",
    "properties": {"diskState": "Unattached", "diskSizeGB": 256},
    "sku": {"name": "Premium_LRS"},
    "managedBy": None,
}
DEAD_NIC = {"name": "vm-a10-ffsftVMNic", "properties": {"virtualMachine": None}}
LEAKED_IP = {
    "name": "vm-a10-ffsftPublicIP",
    "properties": {
        "ipConfiguration": {"id": f"{RG}/Microsoft.Network/networkInterfaces/vm-a10-ffsftVMNic"}
    },
    "sku": {"name": "Standard"},
}


class HttpResponseError(Exception):
    """Spelled as azure.core spells it: the type name is what tells a reader
    whether to re-check the resource group or the identity."""


class FakeTarget:
    subscription_id = "11111111-2222-3333-4444-555555555555"
    resource_group = "rg-ffsft-kc"
    workspace_name = "mlw-ffsft"
    location = "koreacentral"


class FakePoller:
    def result(self):
        return None


class FakeEndpoint:
    def __init__(self, name):
        self.name = name


class FakeDeployment:
    def __init__(self, name, instance_type=PRICED_SKU, instance_count=1):
        self.name = name
        self.instance_type = instance_type
        self.instance_count = instance_count


class FakeOnlineEndpoints:
    def __init__(self, endpoints):
        self._endpoints = list(endpoints)
        self.deleted = []

    def list(self):
        return list(self._endpoints)

    def begin_delete(self, name):
        self.deleted.append(name)
        return FakePoller()


class FakeOnlineDeployments:
    def __init__(self, by_endpoint):
        self._by_endpoint = dict(by_endpoint)
        self.deleted = []

    def list(self, endpoint_name):
        return list(self._by_endpoint.get(endpoint_name, []))

    def begin_delete(self, name, endpoint_name):
        self.deleted.append((endpoint_name, name))
        return FakePoller()


class FakeEmpty:
    def list(self, *a, **kw):
        return []


class ExplodingJobs:
    def list(self, *a, **kw):
        raise HttpResponseError("(AuthorizationFailed) the identity cannot list jobs")


class FakeMLClient:
    def __init__(self, *, online=(), deployments=None, jobs=None):
        self.online_endpoints = FakeOnlineEndpoints(online)
        self.online_deployments = FakeOnlineDeployments(deployments or {})
        self.compute = FakeEmpty()
        self.jobs = jobs or FakeEmpty()
        self.batch_endpoints = FakeEmpty()


def live_endpoint_client(**deployments):
    return FakeMLClient(
        online=[FakeEndpoint("ffsft-a10")],
        deployments={
            "ffsft-a10": [FakeDeployment(name, sku) for name, sku in deployments.items()]
        },
    )


def empty_workspace_client():
    """No AML compute at all -- the shape where `down` has nothing to tear down
    and the resource group is the only thing still billing."""
    return FakeMLClient()


class OKCredential:
    def get_token(self, *scopes, **kw):
        return type("T", (), {"token": "t"})()


class ForbiddenCredential:
    """The 403 the leaked disk hid behind for a whole round."""

    def get_token(self, *scopes, **kw):
        raise HttpResponseError(FORBIDDEN)


class FakeResponse:
    def __init__(self, value):
        self._value = value

    def raise_for_status(self):
        return None

    def json(self):
        return {"value": self._value}


def arm_holding(*, disks=(), ips=(), nics=()):
    """Stand in for `requests.get` against ARM. Patched on the `requests` module
    itself, never on `lifecycle`: the import in `read_orphans` is function-local,
    so `lifecycle.requests` is a name nobody reads and faking it fakes nothing."""

    payload = {
        "Microsoft.Compute/disks": list(disks),
        "Microsoft.Network/publicIPAddresses": list(ips),
        "Microsoft.Network/networkInterfaces": list(nics),
    }

    def get(url, headers=None, timeout=None):
        for path, value in payload.items():
            if path in url:
                return FakeResponse(value)
        raise AssertionError(f"unexpected ARM url: {url}")

    return get


def run_down(monkeypatch, client, argv, *, credential=None, arm=None, never_scanned=False):
    """Drive `ffsft-lifecycle down ...` with both Azure seams faked -- the ML
    client, and the ARM scan `read_orphans` makes with its own credential.

    `quiet_azure_sdk_logs` is replaced rather than allowed to run: it reaches
    every `azure*` logger in the process, and with no conftest.py to restore
    global logging state that would leak levels into whatever runs next.
    """
    monkeypatch.setattr(lifecycle, "quiet_azure_sdk_logs", lambda *a, **k: None)

    class StubTarget:
        @staticmethod
        def from_env():
            return FakeTarget()

    monkeypatch.setattr(ffsft.azure_ml, "AzureTarget", StubTarget)
    monkeypatch.setattr(ffsft.azure_ml, "get_ml_client", lambda target: client)
    if never_scanned:
        # A `read_orphans` that answers [] and records nothing -- which is what a
        # `cmd_down` that never calls it looks like from the report's side.
        monkeypatch.setattr(lifecycle, "read_orphans", lambda target, **kw: [])
    else:
        monkeypatch.setattr(requests, "get", arm or arm_holding())
        real = lifecycle.read_orphans
        monkeypatch.setattr(
            lifecycle,
            "read_orphans",
            lambda target, **kw: real(target, credential=credential or OKCredential(), **kw),
        )
    monkeypatch.setattr("sys.argv", ["ffsft-lifecycle", "down", *argv])
    return lifecycle.main()


def leaking_rg():
    return arm_holding(disks=[LEAKED_DISK], ips=[LEAKED_IP], nics=[DEAD_NIC])


# --- the defect: `down` never looked -----------------------------------------


def test_down_scans_the_resource_group_and_not_only_the_aml_workspace(monkeypatch):
    """The grep that proved the defect: `read_orphans` was called once in the
    file, inside `cmd_status`. The call has to exist on the teardown path too."""
    seen = {}
    monkeypatch.setattr(lifecycle, "quiet_azure_sdk_logs", lambda *a, **k: None)

    class StubTarget:
        @staticmethod
        def from_env():
            return FakeTarget()

    client = live_endpoint_client(blue=PRICED_SKU)
    monkeypatch.setattr(ffsft.azure_ml, "AzureTarget", StubTarget)
    monkeypatch.setattr(ffsft.azure_ml, "get_ml_client", lambda target: client)

    def spy(target, **kw):
        seen["target"] = target
        seen["inv"] = kw.get("inv")
        return []

    monkeypatch.setattr(lifecycle, "read_orphans", spy)
    monkeypatch.setattr("sys.argv", ["ffsft-lifecycle", "down", "--all", "--yes"])
    lifecycle.main()
    assert seen["target"].resource_group == "rg-ffsft-kc"
    # `inv=inv` is the part that matters: it is what puts the resource-group
    # listing in the same scan list as the four AML ones, so one failed listing
    # is reported by one convention rather than two.
    assert seen["inv"] is not None


def test_down_all_yes_over_a_leaking_resource_group_never_says_the_meter_stopped(
    monkeypatch, capsys
):
    """The executed line this file exists for: "meter stopped." rc=0 printed over
    $41.66/month of disks and IPs that are still billing."""
    client = live_endpoint_client(blue=PRICED_SKU)
    run_down(monkeypatch, client, ["--all", "--yes"], arm=leaking_rg())
    out = capsys.readouterr().out
    assert "meter stopped." not in out, out
    assert "NOT idle" in out, out


def test_down_all_yes_names_the_orphaned_disk_and_public_ip_it_is_leaving_behind(
    monkeypatch, capsys
):
    """Naming them is the difference between "go looking" and "delete this disk"
    -- the same rule `unpriced_note` and `unlisted_note` already follow."""
    run_down(monkeypatch, live_endpoint_client(blue=PRICED_SKU), ["--all", "--yes"],
             arm=leaking_rg())
    out = capsys.readouterr().out
    assert "vm-a10-ffsft_OsDisk_1" in out, out
    assert "vm-a10-ffsftPublicIP" in out, out


def test_down_all_yes_states_what_the_leftovers_cost_rather_than_only_counting_them(
    monkeypatch, capsys
):
    """$38.01 + $3.65 = $41.66/month, from the Retail Prices API rows in
    `PREMIUM_DISK_TIERS_USD` and `PUBLIC_IP_HOURLY_USD`."""
    run_down(monkeypatch, live_endpoint_client(blue=PRICED_SKU), ["--all", "--yes"],
             arm=leaking_rg())
    out = capsys.readouterr().out
    assert "$41.66/month for nothing" in out, out


def test_down_all_yes_prints_the_az_command_for_each_leftover_it_will_not_delete(
    monkeypatch, capsys
):
    """Reporting without the command is a bug report, not a teardown: the whole
    reason `down` may not delete these is that a human has to decide."""
    run_down(monkeypatch, live_endpoint_client(blue=PRICED_SKU), ["--all", "--yes"],
             arm=leaking_rg())
    out = capsys.readouterr().out
    assert "az disk delete -g <rg> -n vm-a10-ffsft_OsDisk_1 --yes" in out, out
    assert "az network public-ip delete -g <rg> -n vm-a10-ffsftPublicIP" in out, out


# --- what the fix is not allowed to do ---------------------------------------


def test_down_deletes_the_endpoint_and_leaves_every_orphan_where_it_is(monkeypatch):
    """A disk cannot be un-deleted and there is no `up` that recreates it, so
    finding them may not turn `down` into the thing that removes them."""
    client = live_endpoint_client(blue=PRICED_SKU)
    run_down(monkeypatch, client, ["--all", "--yes"], arm=leaking_rg())
    assert client.online_endpoints.deleted == ["ffsft-a10"]
    assert client.online_deployments.deleted == []


def test_the_savings_figure_counts_only_the_compute_this_command_actually_stops(
    monkeypatch, capsys
):
    """The A10 is $4.320/hr and the leftovers add $0.057/hr that `down` does not
    stop. Summing all three would put $41.66/month of disk into a saving that
    never arrives -- the same false total, pointed the other way."""
    run_down(monkeypatch, live_endpoint_client(blue=PRICED_SKU), ["--all", "--yes"],
             arm=leaking_rg())
    out = capsys.readouterr().out
    assert "stops $4.320/hr" in out, out
    assert "4.377" not in out, out


def test_the_orphans_never_reach_the_will_remove_list(monkeypatch, capsys):
    """`teardown` walks `inv.billing` and handles neither orphan kind, so an
    orphan in there would be printed as removed and then not removed."""
    run_down(monkeypatch, live_endpoint_client(blue=PRICED_SKU), ["--all", "--yes"],
             arm=leaking_rg())
    plan = capsys.readouterr().out.split("will remove:")[1].split("stops")[0]
    assert "online-endpoint ffsft-a10" in plan, plan
    assert "vm-a10-ffsft" not in plan, plan


# --- a scan that did not happen claims nothing --------------------------------


def test_a_failed_resource_group_scan_stops_down_all_from_claiming_the_meter_stopped(
    monkeypatch, capsys
):
    client = live_endpoint_client(blue=PRICED_SKU)
    code = run_down(monkeypatch, client, ["--all", "--yes"], credential=ForbiddenCredential())
    out = capsys.readouterr().out
    assert "meter stopped. `ffsft lifecycle status` to confirm." not in out, out
    assert "LEFTOVERS: UNKNOWN" in out, out
    # It still tore down what it could see: refusing that would leave a
    # $4.320/hr A10 running to protect a sentence nobody had to print.
    assert client.online_endpoints.deleted == ["ffsft-a10"]
    assert code == EXIT_COULD_NOT_LOOK


def test_a_failed_resource_group_scan_is_named_in_the_teardown_output(monkeypatch, capsys):
    """"1 listing failed" sends the reader to the scrollback; the section name
    plus the exception type says whether to re-check the rg or the role."""
    run_down(monkeypatch, live_endpoint_client(blue=PRICED_SKU), ["--all", "--yes"],
             credential=ForbiddenCredential())
    out = capsys.readouterr().out
    assert ORPHANS_SECTION in out, out
    assert "HttpResponseError" in out, out
    assert FORBIDDEN in out, out


def test_a_failed_resource_group_scan_puts_no_leftover_figure_or_count_in_the_output(
    monkeypatch, capsys
):
    """The house split: an unread resource group gets no figure, no count, and no
    verdict. "0 leftovers" is the sentence that hid the leak."""
    run_down(monkeypatch, live_endpoint_client(blue=PRICED_SKU), ["--all", "--yes"],
             credential=ForbiddenCredential())
    out = capsys.readouterr().out
    assert "0 leftover" not in out, out
    assert "$0" not in out, out
    assert "LEFTOVERS: 0" not in out, out


def test_a_named_endpoint_teardown_over_a_failed_scan_still_deletes_and_still_claims_nothing(
    monkeypatch, capsys
):
    """`blind_spots` deliberately scopes the refusal: a resource-group 403 must
    not make a $4.320/hr A10 untearable, because the operator's next move is
    `--yes` somewhere less careful. rc stays 0 -- and the last line therefore has
    to stop claiming the meter is off, or the prose and the exit code disagree."""
    client = live_endpoint_client(blue=PRICED_SKU)
    code = run_down(
        monkeypatch, client, ["--endpoint", "ffsft-a10", "--yes"], credential=ForbiddenCredential()
    )
    out = capsys.readouterr().out
    assert client.online_endpoints.deleted == ["ffsft-a10"]
    assert code == 0
    assert "meter stopped." not in out, out
    assert "UNKNOWN" in out, out


def test_meter_stopped_is_refused_when_the_resource_group_was_never_scanned_at_all(
    monkeypatch, capsys
):
    """The defect itself, pinned as a property rather than as a call site: with
    no scan recorded, there is no failed listing to find, and that absence is
    exactly what used to read as a clean resource group. Deleting the
    `read_orphans` call again has to break this, not just the spy above."""
    code = run_down(
        monkeypatch, live_endpoint_client(blue=PRICED_SKU), ["--all", "--yes"], never_scanned=True
    )
    out = capsys.readouterr().out
    assert "meter stopped." not in out, out
    # rc moved 0 -> 1 in JOURNAL §75. A scan that never ran is the definition of
    # "could not look", and `--all` is the scope that makes the resource group
    # part of the claim; leaving rc at 0 while the prose refused meant a script
    # reading only the number was told the workspace was verified idle.
    assert code == EXIT_COULD_NOT_LOOK


# --- the true negatives this could quietly have destroyed ---------------------


def test_meter_stopped_still_prints_when_the_scan_ran_and_the_resource_group_was_clean(
    monkeypatch, capsys
):
    """If a clean teardown stops saying so, the new sentences mean nothing --
    and rc=0 has to keep meaning something too."""
    client = live_endpoint_client(blue=PRICED_SKU)
    code = run_down(monkeypatch, client, ["--all", "--yes"])
    out = capsys.readouterr().out
    assert "meter stopped. `ffsft lifecycle status` to confirm." in out, out
    assert "LEFTOVERS" not in out, out
    assert code == 0


def test_finding_leftovers_exits_not_idle_and_not_could_not_look(monkeypatch):
    """`EXIT_COULD_NOT_LOOK` means the command could not see what it is talking
    about, which is the opposite of having found these -- a second meaning for 1
    would make both unreadable. But 0 was not free either: `down --all --yes`
    ends by asserting the meter is off, and rc=0 over two resources that are
    demonstrably still billing said so to every script that reads the number
    instead of the paragraph. Hence a third code (§75), not a second meaning."""
    code = run_down(
        monkeypatch, live_endpoint_client(blue=PRICED_SKU), ["--all", "--yes"], arm=leaking_rg()
    )
    assert code == EXIT_NOT_IDLE
    assert EXIT_NOT_IDLE != EXIT_COULD_NOT_LOOK


def test_a_partially_blind_teardown_still_reports_the_leftovers_it_did_manage_to_read(
    monkeypatch, capsys
):
    """A failed `jobs` listing must not swallow a resource-group scan that
    worked: the two listings answer different questions."""
    client = live_endpoint_client(blue=PRICED_SKU)
    client.jobs = ExplodingJobs()
    code = run_down(monkeypatch, client, ["--all", "--yes"], arm=leaking_rg())
    out = capsys.readouterr().out
    assert "vm-a10-ffsft_OsDisk_1" in out, out
    assert "meter stopped." not in out, out
    assert code == EXIT_COULD_NOT_LOOK


def test_a_teardown_with_no_compute_to_remove_still_reports_a_leaking_resource_group(
    monkeypatch, capsys
):
    """The branch that returns before any plan is built. `down --all --yes` on a
    workspace whose only cost is $41.66/month of debris printed a report that
    named none of it."""
    code = run_down(monkeypatch, empty_workspace_client(), ["--all", "--yes"], arm=leaking_rg())
    out = capsys.readouterr().out
    assert "vm-a10-ffsft_OsDisk_1" in out, out
    assert "$41.66/month for nothing" in out, out
    assert "No always-on compute in this workspace" not in out, out
    # Nothing was torn down and two resources are still billing: "could not
    # look" is wrong here (the scan worked) and so is 0.
    assert code == EXIT_NOT_IDLE


def test_a_teardown_with_no_compute_does_not_tell_the_operator_to_re_run_itself(
    monkeypatch, capsys
):
    """Orphan rows push BILLING NOW off zero, which makes `format_inventory`
    print "Run `ffsft lifecycle down --all --yes` to stop the meter" -- advice
    printed by that very command, about resources it may not delete."""
    run_down(monkeypatch, empty_workspace_client(), ["--all", "--yes"], arm=leaking_rg())
    out = capsys.readouterr().out
    assert "there was no always-on compute to tear down" in out, out
    assert "`down` does not delete those; the `az` commands do." in out, out


# --- the exit code says which of the three things happened (§75) --------------


def run_status(monkeypatch, client, *, arm=None, credential=None):
    """`status` over the same seams `run_down` fakes, so the two commands can be
    compared on one workspace in one test."""
    monkeypatch.setattr(lifecycle, "quiet_azure_sdk_logs", lambda *a, **k: None)

    class StubTarget:
        @staticmethod
        def from_env():
            return FakeTarget()

    monkeypatch.setattr(ffsft.azure_ml, "AzureTarget", StubTarget)
    monkeypatch.setattr(ffsft.azure_ml, "get_ml_client", lambda target: client)
    monkeypatch.setattr(requests, "get", arm or arm_holding())
    real = lifecycle.read_orphans
    monkeypatch.setattr(
        lifecycle,
        "read_orphans",
        lambda target, **kw: real(target, credential=credential or OKCredential(), **kw),
    )
    monkeypatch.setattr("sys.argv", ["ffsft-lifecycle", "status"])
    return lifecycle.main()


def test_status_returns_zero_on_the_workspace_where_down_returns_not_idle(monkeypatch, capsys):
    """The reason `down` needed a third code rather than reusing 1: the two
    commands answer different questions about the same second. `status` asks
    "did I manage to read the workspace", and over a readable leaking resource
    group the honest answer is yes -- the leak is in the report it printed.
    `down` ends by asserting the meter is off, so its code has to answer "is it
    off". 0 from one and 3 from the other is not a contradiction; it is the two
    questions. Collapsing them would leave the operator unable to tell "I could
    not see" from "I saw, and it is not idle" -- opposite next moves."""
    assert run_status(monkeypatch, live_endpoint_client(blue=PRICED_SKU), arm=leaking_rg()) == 0
    capsys.readouterr()
    # Both runners wrap the real `read_orphans` to inject a credential, so the
    # second would wrap the first's wrapper and pass `credential` twice.
    monkeypatch.undo()
    assert (
        run_down(
            monkeypatch, live_endpoint_client(blue=PRICED_SKU), ["--all", "--yes"], arm=leaking_rg()
        )
        == EXIT_NOT_IDLE
    )


def test_a_scoped_endpoint_teardown_over_a_leaking_resource_group_still_exits_zero(
    monkeypatch, capsys
):
    """The other half of `blind_spots`' scope rule, applied to the exit code.
    `--endpoint X` claims one endpoint is gone and claims nothing about the
    resource group, so leftovers there are printed and do not set rc: a script
    that tears down one endpoint per run must not read a disk it is forbidden to
    delete as a failed teardown, because the next move after a failed teardown
    is `--yes` somewhere less careful."""
    client = live_endpoint_client(blue=PRICED_SKU)
    code = run_down(monkeypatch, client, ["--endpoint", "ffsft-a10", "--yes"], arm=leaking_rg())
    out = capsys.readouterr().out
    assert client.online_endpoints.deleted == ["ffsft-a10"]
    assert "vm-a10-ffsft_OsDisk_1" in out, out
    assert code == 0


def test_a_scoped_teardown_never_claims_the_workspace_meter_stopped(monkeypatch, capsys):
    """rc=0 on a scoped run means "the endpoint you named is gone", and the last
    line has to mean the same thing. `inv` on this path holds one endpoint's
    deployments, so "meter stopped." would be a whole-workspace sentence printed
    over a one-endpoint measurement -- the cluster it never listed bills on."""
    client = live_endpoint_client(blue=PRICED_SKU)
    code = run_down(monkeypatch, client, ["--endpoint", "ffsft-a10", "--yes"])
    out = capsys.readouterr().out
    assert "meter stopped." not in out, out
    assert "that is the scope you asked for" in out, out
    assert code == 0


def test_down_all_over_a_clean_readable_workspace_is_the_only_path_to_meter_stopped(
    monkeypatch, capsys
):
    """The true negative the third code could quietly have destroyed: if the one
    sentence that asserts the meter is off stops printing when it really is off,
    every other refusal above means nothing."""
    code = run_down(monkeypatch, live_endpoint_client(blue=PRICED_SKU), ["--all", "--yes"])
    out = capsys.readouterr().out
    assert "meter stopped. `ffsft lifecycle status` to confirm." in out, out
    assert code == 0
