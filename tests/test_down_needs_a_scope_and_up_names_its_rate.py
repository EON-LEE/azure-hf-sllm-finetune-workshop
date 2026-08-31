"""Three ways this CLI told a participant something untrue about money.

**R-1 -- `--all` was decorative.** `p_down` declared it and `cmd_down` never
read `args.all`, so `down --yes` and `down --all --yes` were byte-identical:
the bare form silently tore down *every* billing resource in the workspace
while reading like a scoped command. Lab 7 presented the three forms as three
distinct scopes and Lab 5 put the bare form under a "정리 -- 반드시" header
immediately before routing the reader to Lab 6 "엔드포인트를 띄워 둔 채로" --
so the documented cleanup step destroyed the endpoint the next lab needs. The
fix is the safe direction rather than the convenient one: the bare form now
refuses and names both scopes, which leaves every `--all --yes` in the docs
true and makes the accidental teardown impossible. The `--yes` gate is
untouched -- no `--yes` still deletes nothing, `--all` included.

**R-2 -- the LEFTOVERS total priced unknowns at zero.** The line summed every
orphan regardless of whether a rate existed, so one unattached StandardSSD disk
plus one odd-SKU public IP rendered

    LEFTOVERS: 2 resource(s) from deleted VMs, ~$0.00/month for nothing.

which is the `$0.000/hr` bug from `test_cost_reporting_admits_unknown_rates.py`
one block lower, and worse: the trailing "for nothing" turns a hole in the
price table into "nothing to recover here". The BILLING NOW branch directly
above it had used `rate_is_known`/`unpriced_note` correctly the whole time.

**R-3 -- `up` said nothing at all when `--sku` was omitted.** Both branches
tested a SKU read straight off `args`, so the most common invocation printed no
billing line, while `deploy_online` went ahead on the pattern's `default_sku`
(Standard_NV12ads_A10_v5, $1.226/hr -- a rate this tool holds). The comment on
that branch already said saying nothing reads as "free" just as loudly as
$0.000 does.

No network and no Azure: the ML client and `deploy_online` are faked, matching
tests/test_lifecycle.py.
"""

from __future__ import annotations

import inspect

import ffsft.azure_ml
from ffsft.deploy import lifecycle
from ffsft.deploy.lifecycle import (
    BillingItem,
    Inventory,
    effective_sku,
    format_inventory,
    orphan_items,
)

PRICED_SKU = "Standard_NV36ads_A10_v5"
#: The pattern default `deploy_online` falls back to when `--sku` is omitted.
DEFAULT_SKU = "Standard_NV12ads_A10_v5"
#: A CPU SKU this repo prices nowhere and never will.
UNPRICED_SKU = "Standard_D8s_v5"


class FakePoller:
    def result(self):
        return None


class FakeDeployment:
    def __init__(self, name, instance_type=PRICED_SKU, instance_count=1):
        self.name = name
        self.instance_type = instance_type
        self.instance_count = instance_count


class FakeEndpoint:
    def __init__(self, name):
        self.name = name


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
    def list(self, **kwargs):
        return []


class FakeMLClient:
    def __init__(self, *, online=(), deployments=None):
        self.online_endpoints = FakeOnlineEndpoints(online)
        self.online_deployments = FakeOnlineDeployments(deployments or {})
        self.compute = FakeEmpty()
        self.jobs = FakeEmpty()
        self.batch_endpoints = FakeEmpty()


def billing_row():
    """One always-on row, enough to make `format_inventory` print its hint line."""
    return BillingItem(
        kind="online-deployment",
        name="ffsft-qwen/blue",
        detail="1 x " + PRICED_SKU,
        sku=PRICED_SKU,
        instances=1,
        bills_when_idle=True,
    )


def one_endpoint_client(sku=PRICED_SKU):
    return FakeMLClient(
        online=[FakeEndpoint("ffsft-qwen")],
        deployments={"ffsft-qwen": [FakeDeployment("blue", sku)]},
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
    global logging state that would leak ERROR levels into whatever runs next.
    """
    monkeypatch.setattr(lifecycle, "quiet_azure_sdk_logs", lambda *a, **k: None)

    class StubTarget:
        @staticmethod
        def from_env():
            return "target"

    monkeypatch.setattr(ffsft.azure_ml, "AzureTarget", StubTarget)
    monkeypatch.setattr(ffsft.azure_ml, "get_ml_client", lambda target: client)
    monkeypatch.setattr(lifecycle, "read_orphans", _rg_scan_found_nothing)
    monkeypatch.setattr("sys.argv", ["ffsft-lifecycle", "down", *argv])
    return lifecycle.main()


def run_down_with_no_client(monkeypatch, argv):
    """Same, except reaching Azure at all is the failure being tested for."""
    monkeypatch.setattr(lifecycle, "quiet_azure_sdk_logs", lambda *a, **k: None)

    def explode(*a, **k):
        raise AssertionError("a scope-less teardown reached the Azure client")

    monkeypatch.setattr(ffsft.azure_ml, "AzureTarget", explode)
    monkeypatch.setattr(ffsft.azure_ml, "get_ml_client", explode)
    monkeypatch.setattr("sys.argv", ["ffsft-lifecycle", "down", *argv])
    return lifecycle.main()


def run_up(monkeypatch, argv):
    """Drive `ffsft-lifecycle up ...` with `deploy_online` faked out."""
    from ffsft.deploy import endpoint

    monkeypatch.setattr(lifecycle, "quiet_azure_sdk_logs", lambda *a, **k: None)
    monkeypatch.setattr(
        endpoint, "deploy_online", lambda *a, **k: "https://example.invalid/score"
    )
    monkeypatch.setattr("sys.argv", ["ffsft-lifecycle", "up", *argv])
    assert lifecycle.main() == 0


# --- R-1: `--all` is a scope, and a scope is required -----------------------


def test_down_without_a_scope_refuses_instead_of_deleting_everything(monkeypatch, capsys):
    code = run_down_with_no_client(monkeypatch, ["--yes"])
    assert code == 2
    assert capsys.readouterr().out.strip(), "refused without saying why"


def test_the_scope_refusal_names_both_scopes_rather_than_just_complaining(monkeypatch, capsys):
    """"needs a scope" with no vocabulary sends the reader back to --help."""
    run_down_with_no_client(monkeypatch, ["--yes"])
    out = capsys.readouterr().out
    assert "--endpoint" in out
    assert "--all" in out


def test_the_scope_refusal_happens_before_any_azure_client_is_built(monkeypatch):
    """Both fakes raise, so this passing at all is the guarantee: a mistyped
    teardown cannot reach the workspace, the way `--deployment` alone cannot."""
    assert run_down_with_no_client(monkeypatch, ["--yes"]) == 2


def test_a_scope_less_down_deletes_nothing_even_though_yes_was_passed(monkeypatch):
    client = one_endpoint_client()
    assert run_down(monkeypatch, client, ["--yes"]) == 2
    assert client.online_endpoints.deleted == []
    assert client.online_deployments.deleted == []


def test_down_all_yes_still_tears_down_every_billing_resource(monkeypatch):
    """The form five docs teach. Making the flag real must not have made it inert."""
    client = one_endpoint_client()
    assert run_down(monkeypatch, client, ["--all", "--yes"]) == 0
    assert client.online_endpoints.deleted == ["ffsft-qwen"]


def test_down_all_without_yes_still_deletes_nothing(monkeypatch, capsys):
    """Lab 7 makes this claim about `--all` specifically and calls it measured."""
    client = one_endpoint_client()
    assert run_down(monkeypatch, client, ["--all"]) == 0
    assert client.online_endpoints.deleted == []
    assert client.online_deployments.deleted == []
    assert "dry run" in capsys.readouterr().out


def test_down_on_a_named_endpoint_is_a_scope_and_needs_no_all_flag(monkeypatch):
    """Composition guard: the new gate must not have broken the narrow form."""
    client = one_endpoint_client()
    assert run_down(monkeypatch, client, ["--endpoint", "ffsft-qwen", "--yes"]) == 0
    assert client.online_endpoints.deleted == ["ffsft-qwen"]


def test_a_deployment_scope_is_still_refused_without_its_endpoint(monkeypatch, capsys):
    """The older, more specific refusal still wins -- naming `--all` at someone
    who typed `--deployment blue` would answer a question they did not ask."""
    assert run_down_with_no_client(monkeypatch, ["--deployment", "blue", "--yes"]) == 2
    assert "needs --endpoint" in capsys.readouterr().out


def test_the_all_flag_the_status_report_recommends_is_the_one_down_accepts(monkeypatch):
    """`format_inventory` ends with "Run `ffsft lifecycle down --all --yes`".
    Recommending a spelling the parser rejects would be its own bug."""
    hint = format_inventory(Inventory(items=[billing_row()]))
    assert "down --all --yes" in hint
    assert run_down(monkeypatch, one_endpoint_client(), ["--all"]) == 0


# --- R-2: the leftovers total may not price an unknown at zero --------------


def unattached_disk(name="osdisk", sku="Premium_LRS", gb=128):
    return {
        "name": name,
        "managedBy": None,
        "properties": {"diskState": "Unattached", "diskSizeGB": gb},
        "sku": {"name": sku},
    }


def loose_public_ip(name="leaked-ip", sku="Standard"):
    return {"name": name, "properties": {}, "sku": {"name": sku}}


def leftovers(disks, ips):
    return format_inventory(Inventory(items=orphan_items(disks, ips, [])))


def leftovers_block(disks, ips):
    """Only the LEFTOVERS section of the report.

    The BILLING NOW block above it counts the same orphans and has always
    carried its own correct EXCLUDES line, so asserting against the whole
    report would let that one stand in for the note this block was missing --
    and every one of these tests would pass against the broken version.
    """
    out = leftovers(disks, ips)
    _, sep, tail = out.partition("LEFTOVERS:")
    assert sep, out
    return sep + tail


def test_leftovers_never_price_an_orphan_this_tool_cannot_price_at_zero():
    """The reproducer: 128 GB StandardSSD plus a public IP of an unknown SKU
    rendered "~$0.00/month for nothing", where the last two words read as
    "nothing to recover here" over a disk that bills until someone deletes it."""
    out = leftovers_block(
        [unattached_disk(sku="StandardSSD_LRS")], [loose_public_ip(sku="Weird")]
    )
    assert "LEFTOVERS: 2 resource(s)" in out
    assert "$0.00" not in out
    assert "0.00/month for nothing" not in out
    assert "cost UNKNOWN -- no rate for any of them, which is not the same as free" in out


def test_leftovers_that_can_price_nothing_name_the_orphans_they_left_out():
    """"cost UNKNOWN" tells you to go looking; naming them tells you which disk."""
    out = leftovers_block(
        [unattached_disk(sku="StandardSSD_LRS")], [loose_public_ip(sku="Weird")]
    )
    assert "EXCLUDES 2 resource(s) whose rate is unknown" in out
    assert "osdisk [StandardSSD_LRS]" in out
    assert "leaked-ip [Weird]" in out


def test_a_mixed_leftovers_total_covers_only_the_orphans_it_could_price():
    """The quieter half of the same bug: a 2-resource count against a
    1-resource total, with nothing on the line saying so."""
    out = leftovers_block([unattached_disk()], [loose_public_ip(sku="Weird")])
    assert "LEFTOVERS: 2 resource(s)" in out
    # P10, the tier a 128 GB Premium disk bills at -- the IP adds nothing
    # because no rate for "Weird" exists, and that is stated rather than summed.
    assert "~$19.71/month for nothing" in out
    excludes = [ln for ln in out.splitlines() if "EXCLUDES" in ln]
    assert excludes, out
    for line in excludes:
        assert "leaked-ip [Weird]" in line
        # The disk is priced. Naming it here would send someone hunting for a
        # gap that is not there.
        assert "osdisk" not in line


def test_a_fully_priced_leftovers_block_still_states_one_monthly_total():
    """Guard: the honesty branch must not have made the ordinary report vaguer.
    19.71 (P10) + 3.65 (Standard IP at $0.005/hr x 730)."""
    out = leftovers_block([unattached_disk()], [loose_public_ip()])
    assert "~$23.36/month for nothing" in out
    assert "EXCLUDES" not in out
    assert "UNKNOWN" not in out


def test_the_leftovers_block_still_prints_a_delete_command_for_every_orphan():
    """Guard: the unpriced orphan is the one most likely to be forgotten, so it
    must still come with the command that removes it."""
    out = leftovers([unattached_disk(sku="StandardSSD_LRS")], [loose_public_ip(sku="Weird")])
    assert "az disk delete -g <rg> -n osdisk --yes" in out
    assert "az network public-ip delete -g <rg> -n leaked-ip" in out


def test_an_unpriced_orphan_is_still_counted_as_a_leftover():
    """The gap is in the price, not in the fact that the resource is there."""
    out = leftovers_block([unattached_disk(sku="StandardSSD_LRS")], [])
    assert "LEFTOVERS: 1 resource(s)" in out


# --- R-3: `up` prices the SKU it actually deployed --------------------------


def test_up_reports_the_default_sku_when_none_was_asked_for(monkeypatch, capsys):
    """The common invocation. It used to print no billing line at all."""
    run_up(monkeypatch, ["--endpoint", "e", "--hf-model", "org/repo"])
    out = capsys.readouterr().out
    assert DEFAULT_SKU in out
    assert "$1.226/hr" in out
    assert "~$895/month if left up" in out


def test_the_sku_up_reports_is_the_one_deploy_online_will_actually_use():
    """Pins the seam rather than the number: `up` reads the pattern default and
    `deploy_online` falls back to the same field. Two places deriving the same
    SKU is what let the report and the deployment disagree in the first place."""
    from ffsft.deploy import endpoint
    from ffsft.deploy.registry import get_serving

    assert effective_sku(None, "aml_online_vllm") == get_serving("aml_online_vllm").default_sku
    assert "sku or spec.default_sku" in inspect.getsource(endpoint.deploy_online)


def test_an_explicit_sku_still_wins_over_the_pattern_default(monkeypatch, capsys):
    run_up(monkeypatch, ["--endpoint", "e", "--hf-model", "org/repo", "--sku", PRICED_SKU])
    out = capsys.readouterr().out
    assert "$4.320/hr" in out
    assert DEFAULT_SKU not in out


def test_up_admits_an_unknown_rate_rather_than_printing_nothing(monkeypatch, capsys):
    run_up(monkeypatch, ["--endpoint", "e", "--hf-model", "org/repo", "--sku", UNPRICED_SKU])
    out = capsys.readouterr().out
    assert "UNKNOWN" in out
    assert "billing anyway" in out
    assert "$0.000" not in out
    assert "$0" not in out


def test_up_says_something_about_cost_even_when_the_pattern_cannot_be_resolved(
    monkeypatch, capsys
):
    """Silence is the failure mode being fixed, so the fallback path may not
    reintroduce it. A registry that cannot answer is a gap, not a zero."""
    monkeypatch.setattr(lifecycle, "effective_sku", lambda explicit, pattern: "")
    run_up(monkeypatch, ["--endpoint", "e", "--hf-model", "org/repo"])
    out = capsys.readouterr().out
    assert "UNKNOWN" in out
    assert "billing anyway" in out
    assert "$" not in out.split("billing")[1].splitlines()[0]


def test_resolving_the_default_sku_never_breaks_an_up_that_already_succeeded():
    """The endpoint is live by the time this line runs; a registry read that
    throws must cost the participant a price, not the scoring URI."""
    assert effective_sku(None, "no-such-pattern-key") == ""
    assert effective_sku(PRICED_SKU, "no-such-pattern-key") == PRICED_SKU


def test_up_always_prints_a_teardown_command_next_to_the_rate(monkeypatch, capsys):
    run_up(monkeypatch, ["--endpoint", "e", "--hf-model", "org/repo"])
    out = capsys.readouterr().out
    assert "ffsft lifecycle down --endpoint e --yes" in out
