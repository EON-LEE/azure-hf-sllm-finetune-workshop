"""`down` may not delete what it could not enumerate, and rc=0 may not mean "clean".

Round 3 gave `collect_inventory` a `SectionScan` per listing so that "could not
look" could never render as "looked, saw nothing"
(tests/test_status_cannot_report_a_failed_look_as_nothing.py). It closed that
hole on the **status** path and left it open on the **teardown** path, where the
consequence is not a misleading paragraph but a destructive call. Four defects,
each reproduced against `deploy/lifecycle.py` with fakes and no Azure:

**T-1 -- a destructive call on a failed listing.** `down --endpoint X --yes`
with `online_deployments.list` raising 403 printed "no online deployment found
for endpoint 'X'" and then issued a real
`client.online_endpoints.begin_delete(name='X')`, printed "deleted", rc=0. The
audit's proof line was `>>> REAL DELETE CALL ISSUED: () {'name': 'ffsft-a10'}`.
An unreadable workspace must never be treated as an empty one when the next
statement deletes something.

**T-2 -- the fix was built and then bypassed.** The narrowed `Inventory` that
`--endpoint` builds already carried `scans` across, with a comment saying it
does so because the report would otherwise call an unreadable workspace an empty
one -- and the branch returned before any `format_inventory` call, so the
carried scans were never rendered. `down --endpoint X --deployment blue` over a
failed listing printed "no deployment 'blue' on endpoint 'X'" / "endpoint 'X'
left alone", rc=0. A participant following lab 8's blue teardown reads "blue is
already gone" while blue bills $4.959/hr (`Standard_NC24ads_A100_v4`, the rate
in `SKU_HOURLY_PAYG` and in docs/PERFORMANCE.md).

**T-3 -- `read_orphans` swallowed everything.** Every failure became
`log.debug(...)` plus `return []`, recording no scan -- invisible at the default
log level, so it did not even get the `log.warning` the AML sections get. With
all four AML listings empty and the credential raising 403, the report printed
"BILLING NOW: nothing. No always-on compute in this workspace." with no
LEFTOVERS block. docs/JOURNAL.md §11.4 states this in as many words, and it is
the path that leaked $41.66/month.

**T-4 -- the exit code disagreed with the prose.** `down --all --yes` over four
failed listings printed "BILLING NOW: UNKNOWN -- could not look" and returned 0,
so `ffsft lifecycle down --all --yes && echo clean` printed `clean`. Scripts
read exit codes, not paragraphs. `status` had the same split.

Scope is the part that is easy to get wrong in the other direction: a failed
`jobs` listing must NOT block an endpoint teardown, or a permission gap anywhere
in the workspace makes a $4.320/hr A10 untearable and the operator reaches for
something less careful. `blind_spots` is where that line is drawn.

No network and no Azure: the ML client, the credential and `AzureTarget` are all
faked, matching tests/test_lifecycle.py.
"""

from __future__ import annotations

import argparse

import ffsft.azure_ml
from ffsft.deploy import lifecycle
from ffsft.deploy.lifecycle import (
    EXIT_COULD_NOT_LOOK,
    Inventory,
    ScanStatus,
    blind_spots,
    collect_inventory,
    format_inventory,
    read_orphans,
)

PRICED_SKU = "Standard_NV36ads_A10_v5"
#: The SKU lab 8's blue deployment runs on, and the rate a "blue is already
#: gone" message hides.
BLUE_SKU = "Standard_NC24ads_A100_v4"
FORBIDDEN = "(AuthorizationFailed) the identity cannot list deployments"


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
        # Recorded rather than raised: the assertion has to be able to say "no
        # delete was issued", and a fake that explodes only proves the test
        # aborted somewhere.
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


class ExplodingDeployments(FakeOnlineDeployments):
    """The deployments listing 403s; deleting still works, as it does on Azure --
    a read role missing does not make the delete call fail, which is the whole
    reason the empty result had to be checked before acting on it."""

    def list(self, endpoint_name):
        raise HttpResponseError(FORBIDDEN)


class FakeEmpty:
    def list(self, *a, **kw):
        return []


class ExplodingJobs:
    def list(self, *a, **kw):
        raise HttpResponseError("(AuthorizationFailed) the identity cannot list jobs")


class FakeMLClient:
    def __init__(self, *, online=(), deployments=None, exploding_deployments=False, jobs=None):
        self.online_endpoints = FakeOnlineEndpoints(online)
        self.online_deployments = (
            ExplodingDeployments(deployments or {})
            if exploding_deployments
            else FakeOnlineDeployments(deployments or {})
        )
        self.compute = FakeEmpty()
        self.jobs = jobs or FakeEmpty()
        self.batch_endpoints = FakeEmpty()

    @property
    def nothing_was_deleted(self):
        return not self.online_endpoints.deleted and not self.online_deployments.deleted


class BlindClient:
    """Every listing fails -- the wrong-resource-group / missing-role case."""

    def __init__(self):
        self.deleted = []

    def __getattr__(self, name):
        raise HttpResponseError("workspace 'mlw-ffsft' not found in rg 'rg-ffsft-kc'")


def unreadable_endpoint_client():
    """An endpoint that lists, whose deployments do not. This is the shape that
    issued the real delete: `inv.items` is empty for both reasons at once."""
    return FakeMLClient(online=[FakeEndpoint("ffsft-a10")], exploding_deployments=True)


def live_endpoint_client(**deployments):
    return FakeMLClient(
        online=[FakeEndpoint("ffsft-a10")],
        deployments={
            "ffsft-a10": [FakeDeployment(name, sku) for name, sku in deployments.items()]
        },
    )


def _rg_scan_found_nothing(target, *, inv=None, **kw):
    """Stand in for the resource-group scan `cmd_down` runs since round 5.

    The real `read_orphans` builds a `DefaultAzureCredential` and calls ARM, and
    no test in this repo may do that. Recording the `SectionScan` is the load-
    bearing half: `cmd_down` prints "meter stopped." only over a scan that
    actually happened, so a stub that returned `[]` and recorded nothing would
    make every teardown here read as "could not look" -- which is exactly the
    distinction the fix is about.
    """
    if inv is not None:
        inv.scans.append(lifecycle.SectionScan(lifecycle.ORPHANS_SECTION))
    return []


def run_down(monkeypatch, client, argv):
    """Drive `ffsft-lifecycle down ...` against a fake client, returning the exit code.

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
    monkeypatch.setattr(lifecycle, "read_orphans", _rg_scan_found_nothing)
    monkeypatch.setattr("sys.argv", ["ffsft-lifecycle", "down", *argv])
    return lifecycle.main()


def run_status(monkeypatch, client, *, orphans=None, credential=None):
    """Drive `cmd_status` with both Azure seams faked -- the ML client and the
    ARM scan `read_orphans` makes with its own credential."""

    class StubTarget:
        @staticmethod
        def from_env():
            return FakeTarget()

    monkeypatch.setattr(ffsft.azure_ml, "AzureTarget", StubTarget)
    monkeypatch.setattr(ffsft.azure_ml, "get_ml_client", lambda target: client)
    if credential is not None:
        real = lifecycle.read_orphans
        monkeypatch.setattr(
            lifecycle,
            "read_orphans",
            lambda target, **kw: real(target, credential=credential, **kw),
        )
    else:
        monkeypatch.setattr(lifecycle, "read_orphans", lambda target, **kw: list(orphans or []))
    return lifecycle.cmd_status(argparse.Namespace())


class ForbiddenCredential:
    """A credential that cannot read ARM. The 403 the leaked disk hid behind."""

    def get_token(self, *scopes, **kw):
        raise HttpResponseError("(AuthorizationFailed) no Reader on rg-ffsft-kc")


# --- T-1: an empty result that is not evidence may not be acted on ----------


def test_down_on_an_endpoint_whose_deployments_could_not_be_listed_deletes_nothing(monkeypatch):
    """The executed proof of the defect was a real
    `online_endpoints.begin_delete(name='ffsft-a10')` reached through a printed
    "no online deployment found". Both fakes record, so an empty list here is
    the guarantee and not an aborted test."""
    client = unreadable_endpoint_client()
    run_down(monkeypatch, client, ["--endpoint", "ffsft-a10", "--yes"])
    assert client.online_endpoints.deleted == []
    assert client.online_deployments.deleted == []


def test_down_on_an_endpoint_whose_deployments_could_not_be_listed_exits_non_zero(monkeypatch):
    client = unreadable_endpoint_client()
    assert run_down(monkeypatch, client, ["--endpoint", "ffsft-a10", "--yes"]) == (
        EXIT_COULD_NOT_LOOK
    )


def test_a_failed_endpoint_listing_never_reads_as_no_deployment_found(monkeypatch, capsys):
    """"no online deployment found for endpoint 'X'" is a claim about the
    workspace. The tool is only entitled to a claim about itself."""
    run_down(monkeypatch, unreadable_endpoint_client(), ["--endpoint", "ffsft-a10", "--yes"])
    out = capsys.readouterr().out
    assert "no online deployment found" not in out, out
    assert "deleting endpoint shell" not in out, out
    assert "COULD NOT LOOK" in out, out


def test_the_refusal_names_the_listing_that_failed_and_the_error_it_raised(monkeypatch, capsys):
    """"1 listing failed" sends the reader to the scrollback. The section name
    plus the exception type is what says whether to re-check the rg or the role."""
    run_down(monkeypatch, unreadable_endpoint_client(), ["--endpoint", "ffsft-a10", "--yes"])
    out = capsys.readouterr().out
    assert "deployments of ffsft-a10" in out, out
    assert "HttpResponseError" in out, out
    assert FORBIDDEN in out, out


def test_a_blind_endpoint_teardown_says_nothing_was_deleted(monkeypatch, capsys):
    """The operator's next question is "did it do anything?", and the answer has
    to be in the output rather than inferred from the absence of "deleted"."""
    run_down(monkeypatch, unreadable_endpoint_client(), ["--endpoint", "ffsft-a10", "--yes"])
    assert "nothing was deleted" in capsys.readouterr().out


def test_a_blind_endpoint_teardown_without_yes_also_refuses(monkeypatch):
    """The `--yes` gate is not what makes this safe: a dry run that reports an
    absent endpoint teaches the operator to type `--yes` next."""
    client = unreadable_endpoint_client()
    assert run_down(monkeypatch, client, ["--endpoint", "ffsft-a10"]) == EXIT_COULD_NOT_LOOK
    assert client.nothing_was_deleted


def test_down_refuses_when_the_endpoint_listing_itself_failed(monkeypatch):
    """The other half of the same blind spot: with `online_endpoints.list`
    raising, the endpoint never reaches the inventory at all, which looked
    exactly like an endpoint with no deployments."""
    client = BlindClient()
    assert run_down(monkeypatch, client, ["--endpoint", "ffsft-a10", "--yes"]) == (
        EXIT_COULD_NOT_LOOK
    )
    assert client.deleted == []


# --- T-2: the carried scans have to be rendered, not just carried -----------


def test_down_on_a_deployment_whose_listing_failed_does_not_call_it_absent(monkeypatch, capsys):
    """lab 8 shifts traffic to green, then deletes blue. Reading "no deployment
    'blue'" over a 403 leaves an A100 at $4.959/hr serving nothing."""
    client = unreadable_endpoint_client()
    code = run_down(
        monkeypatch, client, ["--endpoint", "ffsft-a10", "--deployment", "blue", "--yes"]
    )
    out = capsys.readouterr().out
    assert "no deployment 'blue'" not in out, out
    assert "left alone" not in out, out
    assert code == EXIT_COULD_NOT_LOOK
    assert client.nothing_was_deleted


def test_a_blind_deployment_teardown_names_the_deployment_it_could_not_find(monkeypatch, capsys):
    """"could not look" with no subject leaves the reader to guess which of blue
    and green the tool is talking about."""
    run_down(
        monkeypatch,
        unreadable_endpoint_client(),
        ["--endpoint", "ffsft-a10", "--deployment", "blue"],
    )
    out = capsys.readouterr().out
    assert "blue" in out and "ffsft-a10" in out, out
    assert "UNKNOWN" in out, out


def test_the_scans_carried_into_the_narrowed_inventory_actually_reach_the_report(
    monkeypatch, capsys
):
    """The round-3 comment at the narrowing site says the scans are carried over
    so the report cannot call an unreadable workspace an empty one -- and this
    branch used to return before any `format_inventory` call, so nothing
    rendered them. The scope header is the cheapest proof the report ran."""
    run_down(
        monkeypatch,
        unreadable_endpoint_client(),
        ["--endpoint", "ffsft-a10", "--deployment", "blue"],
    )
    out = capsys.readouterr().out
    assert "LOOKED IN" in out, out
    assert "mlw-ffsft" in out, out
    assert "silence, not evidence" in out, out


def test_a_blind_teardown_prints_no_dollar_figure_at_all(monkeypatch, capsys):
    """Same rule as the status table: an unread workspace is not a $0.000/hr one."""
    run_down(monkeypatch, unreadable_endpoint_client(), ["--endpoint", "ffsft-a10", "--yes"])
    out = capsys.readouterr().out
    assert "$0" not in out, out
    assert "0.000" not in out, out


# --- T-3: read_orphans records its scan like every other listing ------------


def test_a_failed_orphan_scan_is_recorded_rather_than_returning_an_empty_list(monkeypatch):
    inv = Inventory()
    items = read_orphans(FakeTarget(), credential=ForbiddenCredential(), inv=inv)
    assert items == []
    assert [s.section for s in inv.failed_scans] == [lifecycle.ORPHANS_SECTION]
    assert inv.failed_scans[0].status is ScanStatus.FAILED
    assert "AuthorizationFailed" in inv.failed_scans[0].detail


def test_a_failed_orphan_scan_reaches_the_report_as_could_not_look(monkeypatch):
    """The AML half of this report has refused to call a failed listing an empty
    workspace since round 3. The resource-group half went on doing it 60 lines
    lower, in the same file, for the scan that once leaked $41.66/month."""
    inv = collect_inventory(FakeMLClient())
    inv.items.extend(read_orphans(FakeTarget(), credential=ForbiddenCredential(), inv=inv))
    out = format_inventory(inv, FakeTarget())
    assert "No always-on compute in this workspace" not in out, out
    assert "could not look" in out.lower(), out
    assert lifecycle.ORPHANS_SECTION in out, out


def test_a_failed_orphan_scan_says_leftovers_are_unknown_rather_than_printing_none(monkeypatch):
    """A missing LEFTOVERS block is itself the claim "nothing was left behind",
    and that is the claim the leaked disk and public IP hid behind."""
    inv = collect_inventory(FakeMLClient())
    inv.items.extend(read_orphans(FakeTarget(), credential=ForbiddenCredential(), inv=inv))
    out = format_inventory(inv, FakeTarget())
    assert "LEFTOVERS: UNKNOWN" in out, out
    assert "$0" not in out, out


def test_the_orphan_scan_failure_is_logged_at_warning_and_not_debug(monkeypatch, caplog):
    """`log.debug` is invisible at the default level, so the only trace of the
    403 went nowhere at all -- worse than the `log.warning` the AML sections got."""
    with caplog.at_level("WARNING", logger="ffsft.deploy.lifecycle"):
        read_orphans(FakeTarget(), credential=ForbiddenCredential(), inv=Inventory())
    assert any("orphaned disks" in r.getMessage() for r in caplog.records), caplog.records


def test_status_carries_the_orphan_scan_into_the_same_list_as_the_aml_listings(monkeypatch):
    """One report, one convention. A second place to look for "did this listing
    happen" is how the first one stayed broken for a round."""
    assert run_status(
        monkeypatch, FakeMLClient(), credential=ForbiddenCredential()
    ) == EXIT_COULD_NOT_LOOK


def test_status_over_an_unreadable_workspace_exits_non_zero(monkeypatch):
    assert run_status(monkeypatch, BlindClient()) == EXIT_COULD_NOT_LOOK


# --- T-4: the exit code has to agree with the prose -------------------------


def test_down_all_yes_exits_non_zero_when_every_listing_failed(monkeypatch):
    """`ffsft lifecycle down --all --yes && echo clean` printed `clean` here."""
    assert run_down(monkeypatch, BlindClient(), ["--all", "--yes"]) == EXIT_COULD_NOT_LOOK


def test_down_all_yes_deletes_nothing_when_every_listing_failed(monkeypatch):
    client = BlindClient()
    run_down(monkeypatch, client, ["--all", "--yes"])
    assert client.deleted == []


def test_a_partially_blind_down_all_still_tears_down_what_it_could_see(monkeypatch):
    """Refusing everything because the jobs listing 403s would leave the A10
    running, which is the failure this whole file is about, inverted."""
    client = live_endpoint_client(blue=PRICED_SKU)
    client.jobs = ExplodingJobs()
    run_down(monkeypatch, client, ["--all", "--yes"])
    assert client.online_endpoints.deleted == ["ffsft-a10"]


def test_a_partially_blind_down_all_exits_non_zero_because_it_cannot_claim_idle(monkeypatch):
    """It removed what it saw; it may not report that the workspace is now idle."""
    client = live_endpoint_client(blue=PRICED_SKU)
    client.jobs = ExplodingJobs()
    assert run_down(monkeypatch, client, ["--all", "--yes"]) == EXIT_COULD_NOT_LOOK


def test_a_partially_blind_teardown_plan_says_what_it_could_not_cover(monkeypatch, capsys):
    client = live_endpoint_client(blue=PRICED_SKU)
    client.jobs = ExplodingJobs()
    run_down(monkeypatch, client, ["--all", "--yes"])
    out = capsys.readouterr().out
    assert "covers only what could be listed" in out, out
    assert "jobs" in out, out
    assert "meter stopped. `ffsft lifecycle status` to confirm." not in out, out


# --- the scope line: a blind spot elsewhere is not a blind spot here ---------


def test_a_named_endpoint_is_not_blocked_by_a_listing_it_does_not_depend_on(monkeypatch):
    """The over-correction that would cost money: a 403 on `jobs` making a
    $4.320/hr A10 untearable, so the operator reaches for the portal instead."""
    client = live_endpoint_client(blue=PRICED_SKU)
    client.jobs = ExplodingJobs()
    assert run_down(monkeypatch, client, ["--endpoint", "ffsft-a10", "--yes"]) == 0
    assert client.online_endpoints.deleted == ["ffsft-a10"]


def test_blind_spots_for_an_endpoint_covers_only_that_endpoints_listings():
    inv = collect_inventory(FakeMLClient(online=[FakeEndpoint("ffsft-a10")], jobs=ExplodingJobs()))
    assert [s.section for s in inv.failed_scans] == ["jobs"]
    assert blind_spots(inv, "ffsft-a10") == []
    # `--all` claims something about the whole workspace, so every scan counts.
    assert [s.section for s in blind_spots(inv, None)] == ["jobs"]


def test_blind_spots_for_an_endpoint_includes_its_own_deployment_listing():
    inv = collect_inventory(unreadable_endpoint_client())
    assert [s.section for s in blind_spots(inv, "ffsft-a10")] == ["deployments of ffsft-a10"]


def test_blind_spots_does_not_confuse_one_endpoints_failure_with_anothers():
    """`deployments of X` and `deployments of Y` are different claims; a shared
    prefix match would have made a 403 on either block both."""
    inv = collect_inventory(unreadable_endpoint_client())
    assert blind_spots(inv, "ffsft-a10-green") == []


# --- the true negatives, which this change could quietly have destroyed -----


def test_a_genuinely_empty_workspace_still_reports_nothing_and_exits_zero(monkeypatch, capsys):
    """If a clean workspace stops saying so, `status` stops being worth running
    casually, and the endpoint nobody checked on is the whole cost risk here."""
    assert run_status(monkeypatch, FakeMLClient(), orphans=[]) == 0
    out = capsys.readouterr().out
    assert "BILLING NOW: nothing. No always-on compute in this workspace." in out
    assert "could not look" not in out.lower(), out
    assert "LEFTOVERS:" not in out, out


def test_a_successful_orphan_scan_records_an_ok_scan_and_leaves_the_report_quiet():
    class OKCredential:
        def get_token(self, *scopes, **kw):
            return type("T", (), {"token": "t"})()

    class NoResources:
        @staticmethod
        def get(url, headers=None, timeout=None):
            return type(
                "R", (), {"raise_for_status": lambda self: None, "json": lambda self: {"value": []}}
            )()

    import requests

    inv = collect_inventory(FakeMLClient())
    # Patching the module `read_orphans` reaches for, not a re-export: the
    # import is function-local, so `lifecycle.requests` is a name nobody reads.
    saved = requests.get
    requests.get = NoResources.get
    try:
        inv.items.extend(read_orphans(FakeTarget(), credential=OKCredential(), inv=inv))
    finally:
        requests.get = saved
    assert inv.failed_scans == []
    out = format_inventory(inv, FakeTarget())
    assert "BILLING NOW: nothing. No always-on compute in this workspace." in out
    assert "LEFTOVERS:" not in out, out


def test_a_real_endpoint_teardown_still_deletes_the_endpoint(monkeypatch):
    """The command has to keep working, or the $103/day resource stays up."""
    client = live_endpoint_client(blue=PRICED_SKU)
    assert run_down(monkeypatch, client, ["--endpoint", "ffsft-a10", "--yes"]) == 0
    assert client.online_endpoints.deleted == ["ffsft-a10"]


def test_a_real_deployment_teardown_still_deletes_only_that_deployment(monkeypatch):
    """lab 8's blue teardown, on a workspace that answered."""
    client = live_endpoint_client(blue=BLUE_SKU, green=BLUE_SKU)
    code = run_down(
        monkeypatch, client, ["--endpoint", "ffsft-a10", "--deployment", "blue", "--yes"]
    )
    assert code == 0
    assert client.online_deployments.deleted == [("ffsft-a10", "blue")]
    assert client.online_endpoints.deleted == []


def test_a_deployment_that_really_is_absent_is_still_reported_as_absent(monkeypatch, capsys):
    """The listing happened and returned green only. That absence IS evidence,
    and refusing to say so would make the safe answer useless."""
    client = live_endpoint_client(green=BLUE_SKU)
    code = run_down(
        monkeypatch, client, ["--endpoint", "ffsft-a10", "--deployment", "blue", "--yes"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "no deployment 'blue'" in out, out
    assert "left alone" in out, out
    assert client.nothing_was_deleted


def test_an_endpoint_shell_that_really_has_no_deployments_is_still_removed(monkeypatch):
    """The listing succeeded and returned nothing: an endpoint whose deployment
    failed to create still exists and still blocks the name."""
    client = FakeMLClient(online=[FakeEndpoint("ffsft-a10")], deployments={})
    assert run_down(monkeypatch, client, ["--endpoint", "ffsft-a10", "--yes"]) == 0
    assert client.online_endpoints.deleted == ["ffsft-a10"]


def test_down_all_yes_over_a_readable_workspace_still_exits_zero(monkeypatch, capsys):
    """rc=0 has to keep meaning something, or the new non-zero means nothing."""
    client = live_endpoint_client(blue=PRICED_SKU)
    assert run_down(monkeypatch, client, ["--all", "--yes"]) == 0
    assert "meter stopped. `ffsft lifecycle status` to confirm." in capsys.readouterr().out
