"""A cost report may not answer "we have no price" with the number zero.

`ffsft-lifecycle status` was observed live printing

    BILLING NOW: 1 resource(s)  $0.000/hr  ~$0/month if left running

over a running `Standard_NV6ads_A10_v5`. The SKU was simply missing from
`SKU_HOURLY_PAYG`, `hourly_rate` returned 0.0, and the report asserted in one
sentence that the resource *is* billing and that it costs nothing. A
participant who reads that leaves an A10 up. The disk half of this module has
always handled the same gap honestly -- `disk_monthly_usd` returns 0.0 for a
SKU it cannot price and the caller appends "(price unknown for this SKU)" --
so the fix is to make the VM half say what the disk half says.

The teardown tests here are the other end of the same money leak. Lab 8's
blue/green cutover ends with "blue 를 지우세요" and, before `--deployment`, the
only teardown this CLI offered deleted the whole endpoint -- which would take
green, the deployment that just received 100% of the traffic, with it. Facing
that choice the realistic outcome is that blue is left running at $119/day. So
the guarantees pinned below are: one deployment can go without its endpoint,
its siblings survive, and none of it happens without `--yes`.

No network and no Azure. The ML client is faked, matching tests/test_lifecycle.py.
"""

from __future__ import annotations

import ast
import inspect

import pytest

import ffsft.azure_ml
from ffsft.deploy import lifecycle
from ffsft.deploy.lifecycle import (
    SKU_HOURLY_PAYG,
    BillingItem,
    Inventory,
    collect_inventory,
    format_inventory,
    hourly_rate,
    rate_is_known,
    teardown_deployment,
)

#: Not in the table and never will be -- a CPU SKU nobody prices here.
UNPRICED_SKU = "Standard_D8s_v5"
PRICED_SKU = "Standard_NV36ads_A10_v5"


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


def blue_green_client(**deployments):
    """An endpoint `ffsft-qwen` carrying the named deployments."""
    return FakeMLClient(
        online=[FakeEndpoint("ffsft-qwen")],
        deployments={
            "ffsft-qwen": [FakeDeployment(name, sku) for name, sku in deployments.items()]
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
    global logging state that would leak ERROR levels into whatever runs next.
    """
    calls = []
    monkeypatch.setattr(lifecycle, "quiet_azure_sdk_logs", lambda *a, **k: calls.append(True))

    class StubTarget:
        @staticmethod
        def from_env():
            return "target"

    monkeypatch.setattr(ffsft.azure_ml, "AzureTarget", StubTarget)
    monkeypatch.setattr(ffsft.azure_ml, "get_ml_client", lambda target: client)
    monkeypatch.setattr(lifecycle, "read_orphans", _rg_scan_found_nothing)
    monkeypatch.setattr("sys.argv", ["ffsft-lifecycle", "down", *argv])
    code = lifecycle.main()
    return code, calls


def unpriced_inventory():
    return collect_inventory(blue_green_client(blue=UNPRICED_SKU))


# --- D-1: an unknown rate is not a rate of zero -----------------------------


def test_a_sku_with_no_known_rate_is_never_rendered_as_a_dollar_figure():
    out = format_inventory(unpriced_inventory())
    assert "$0" not in out, out
    assert "0.000" not in out, out
    assert "price unknown for this SKU" in out


def test_a_report_that_can_price_nothing_says_so_instead_of_printing_a_total():
    out = format_inventory(unpriced_inventory())
    assert "BILLING NOW: 1 resource(s)" in out
    assert "UNKNOWN" in out
    assert "not the same as free" in out


def test_a_resource_with_no_known_rate_is_still_flagged_as_billing():
    """The gap is in the price, not in the fact. `down` must still be offered."""
    inv = unpriced_inventory()
    assert len(inv.billing) == 1
    assert "down --all --yes" in format_inventory(inv)


def test_the_total_names_the_resources_it_excluded_rather_than_absorbing_them():
    client = FakeMLClient(
        online=[FakeEndpoint("ffsft-qwen")],
        deployments={
            "ffsft-qwen": [
                FakeDeployment("blue", PRICED_SKU),
                FakeDeployment("green", UNPRICED_SKU),
            ]
        },
    )
    inv = collect_inventory(client)
    out = format_inventory(inv)
    # The priced half is still a total, and it is the priced half only.
    assert inv.hourly == pytest.approx(4.320)
    assert "EXCLUDES 1 resource(s)" in out
    assert "ffsft-qwen/green" in out
    assert UNPRICED_SKU in out


def test_a_rate_of_zero_and_a_missing_rate_stop_being_the_same_answer():
    # `hourly_rate` keeps returning 0.0 so no caller crashes on a new SKU; the
    # question "do we actually have a price" now has its own function.
    assert hourly_rate(UNPRICED_SKU) == 0.0
    assert rate_is_known(UNPRICED_SKU) is False
    assert rate_is_known(PRICED_SKU) is True


def test_a_resource_that_genuinely_costs_nothing_is_not_reported_as_a_price_gap():
    """An idle row is free, not unpriced -- flagging it would train people to ignore the flag."""
    idle = BillingItem(kind="compute-cluster", name="gpu", detail="min_instances=0", sku=PRICED_SKU)
    assert idle.rate_known is True
    out = format_inventory(Inventory(items=[idle]))
    assert "price unknown" not in out


def test_a_priced_resource_still_reports_its_hourly_and_monthly_cost():
    """Guard: the honesty path must not have made the ordinary report vaguer."""
    out = format_inventory(collect_inventory(blue_green_client(blue=PRICED_SKU)))
    assert "$4.320/hr" in out
    assert "EXCLUDES" not in out


# --- the rates added from the Retail Prices API -----------------------------


def test_the_a10_that_was_observed_reporting_zero_dollars_an_hour_now_has_a_price():
    assert SKU_HOURLY_PAYG["Standard_NV6ads_A10_v5"] == pytest.approx(0.613)
    out = format_inventory(collect_inventory(blue_green_client(blue="Standard_NV6ads_A10_v5")))
    assert "0.613" in out
    assert "$0.000/hr" not in out


def test_the_five_rates_that_were_already_measured_were_not_overwritten():
    """The 2026-08-27 API query reproduced all five to the digit; a re-query is
    not a licence to edit a measured value, so this pins them against drift."""
    assert SKU_HOURLY_PAYG["Standard_NC16as_T4_v3"] == pytest.approx(1.481)
    assert SKU_HOURLY_PAYG["Standard_NV18ads_A10_v5"] == pytest.approx(2.160)
    assert SKU_HOURLY_PAYG["Standard_NV36ads_A10_v5"] == pytest.approx(4.320)
    assert SKU_HOURLY_PAYG["Standard_NC24ads_A100_v4"] == pytest.approx(4.959)
    assert SKU_HOURLY_PAYG["Standard_NC40ads_H100_v5"] == pytest.approx(9.423)


def test_the_table_holds_payg_rates_and_not_the_cheaper_tiers():
    """Managed online endpoints cannot use LowPriority at all and are not Spot.

    Both cheaper tiers exist for these SKUs and both are roughly a fifth of the
    price, so a Spot or LowPriority figure here would under-report the one
    resource in this repo that bills 24/7 by about 5x. The values below are the
    measured Spot (0.113282) and LowPriority (0.123) rates for the A10 whose
    PAYG rate is 0.613 -- if either ever appears, the wrong tier was filed.
    """
    rates = set(SKU_HOURLY_PAYG.values())
    assert 0.113282 not in rates
    assert 0.123 not in rates
    assert 0.916423 not in rates  # NC24ads_A100_v4 Spot, against its PAYG 4.959


def test_a_sku_whose_price_could_not_be_sourced_is_left_out_of_the_table():
    """koreacentral publishes no LowPriority meter for ND96isr_H100_v5 at all.

    Its PAYG row does exist and is in the table; the missing tier stays missing
    rather than being derived from the 0.20 rule that fits the other fifteen.
    """
    assert SKU_HOURLY_PAYG["Standard_ND96isr_H100_v5"] == pytest.approx(132.732)
    assert not any(v == pytest.approx(26.5464) for v in SKU_HOURLY_PAYG.values())


# --- P0-7: deleting one deployment ------------------------------------------


def test_down_refuses_a_deployment_name_with_no_endpoint(monkeypatch, capsys):
    """`blue` names a deployment on every endpoint this workshop builds."""

    def explode(target):
        raise AssertionError("built an ML client for an ambiguous request")

    monkeypatch.setattr(ffsft.azure_ml, "get_ml_client", explode)
    monkeypatch.setattr("sys.argv", ["ffsft-lifecycle", "down", "--deployment", "blue"])
    monkeypatch.setattr(lifecycle, "quiet_azure_sdk_logs", lambda *a, **k: None)
    assert lifecycle.main() == 2
    assert "needs --endpoint" in capsys.readouterr().out


def test_down_with_a_deployment_and_no_yes_deletes_nothing(monkeypatch, capsys):
    """Verified live on the endpoint paths already shipped; the new flag obeys the same gate."""
    client = blue_green_client(blue=PRICED_SKU, green=PRICED_SKU)
    code, _ = run_down(monkeypatch, client, ["--endpoint", "ffsft-qwen", "--deployment", "blue"])
    assert code == 0
    assert client.online_deployments.deleted == []
    assert client.online_endpoints.deleted == []
    assert "dry run" in capsys.readouterr().out


def test_down_deletes_only_the_named_deployment_and_keeps_the_endpoint(monkeypatch):
    client = blue_green_client(blue=PRICED_SKU, green=PRICED_SKU)
    code, _ = run_down(
        monkeypatch, client, ["--endpoint", "ffsft-qwen", "--deployment", "blue", "--yes"]
    )
    assert code == 0
    assert client.online_deployments.deleted == [("ffsft-qwen", "blue")]
    assert client.online_endpoints.deleted == []


def test_down_leaves_a_sibling_deployment_alone(monkeypatch):
    """Green holds 100% of the traffic by the time blue is deleted."""
    client = blue_green_client(blue=PRICED_SKU, green=PRICED_SKU)
    run_down(monkeypatch, client, ["--endpoint", "ffsft-qwen", "--deployment", "blue", "--yes"])
    assert ("ffsft-qwen", "green") not in client.online_deployments.deleted


def test_deleting_the_last_deployment_does_not_take_the_endpoint_with_it(monkeypatch):
    """Nobody asked for the endpoint. An empty endpoint shell costs nothing to keep."""
    client = blue_green_client(blue=PRICED_SKU)
    run_down(monkeypatch, client, ["--endpoint", "ffsft-qwen", "--deployment", "blue", "--yes"])
    assert client.online_deployments.deleted == [("ffsft-qwen", "blue")]
    assert client.online_endpoints.deleted == []


def test_down_on_a_deployment_that_is_not_there_does_not_delete_the_endpoint_shell(
    monkeypatch, capsys
):
    """The shell-delete path exists for an endpoint whose deployment failed to
    create. Reaching it from `--deployment` would delete the sibling that is
    serving traffic, on the strength of a typo in the deployment name."""
    client = blue_green_client(green=PRICED_SKU)
    code, _ = run_down(
        monkeypatch, client, ["--endpoint", "ffsft-qwen", "--deployment", "blue", "--yes"]
    )
    assert code == 0
    assert client.online_endpoints.deleted == []
    assert client.online_deployments.deleted == []
    assert "left alone" in capsys.readouterr().out


def test_down_on_an_endpoint_alone_still_deletes_the_whole_endpoint(monkeypatch):
    """Composition guard: the new flag must not have changed the default path."""
    client = blue_green_client(blue=PRICED_SKU, green=PRICED_SKU)
    run_down(monkeypatch, client, ["--endpoint", "ffsft-qwen", "--yes"])
    assert client.online_endpoints.deleted == ["ffsft-qwen"]
    # Deleting the endpoint takes its deployments with it -- deleting them one
    # by one first would only be slower.
    assert client.online_deployments.deleted == []


def test_down_on_an_endpoint_with_no_deployments_still_removes_the_blocking_shell(monkeypatch):
    client = FakeMLClient(online=[FakeEndpoint("ffsft-qwen")], deployments={})
    run_down(monkeypatch, client, ["--endpoint", "ffsft-qwen", "--yes"])
    assert client.online_endpoints.deleted == ["ffsft-qwen"]


def test_the_deployment_teardown_plan_touches_no_client_until_it_is_confirmed():
    class ExplodingClient:
        def __getattr__(self, name):
            raise AssertionError(f"dry run reached the client via .{name}")

    planned = teardown_deployment(ExplodingClient(), "ffsft-qwen", "blue", dry_run=True)
    assert planned == ["online-deployment ffsft-qwen/blue (endpoint ffsft-qwen kept)"]


def test_the_teardown_plan_never_prices_a_deployment_it_cannot_price(monkeypatch, capsys):
    client = blue_green_client(blue=UNPRICED_SKU)
    run_down(monkeypatch, client, ["--endpoint", "ffsft-qwen", "--deployment", "blue"])
    out = capsys.readouterr().out
    assert "$0.000/hr" not in out
    assert "UNKNOWN amount per hour" in out
    assert "ffsft-qwen/blue" in out


# --- D-2: the table is worthless if the SDK buries it -----------------------


def test_the_lifecycle_cli_quiets_the_azure_sdk_logs(monkeypatch):
    client = blue_green_client(blue=PRICED_SKU)
    _, calls = run_down(monkeypatch, client, ["--endpoint", "ffsft-qwen"])
    assert calls, "main() never called quiet_azure_sdk_logs"


def test_the_sdk_is_quieted_after_logging_is_configured_and_not_before():
    """The filter half attaches to the root handlers `basicConfig` creates, so
    calling it first silences the HTTP dumps by level and lets the `azure.ai.ml`
    lines through. Order is the whole fix, and it is invisible at runtime."""
    body = ast.parse(inspect.getsource(lifecycle.main)).body[0].body
    called = [
        ast.unparse(node.value.func)
        for node in body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
    ]
    assert "logging.basicConfig" in called
    assert "quiet_azure_sdk_logs" in called
    assert called.index("quiet_azure_sdk_logs") > called.index("logging.basicConfig")
