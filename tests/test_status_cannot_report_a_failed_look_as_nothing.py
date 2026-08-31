"""`status` may not answer "we could not look" with "there is nothing here".

`deploy/logs.py` exists because a failed log read was twice mistaken for an
empty log. `collect_inventory` committed the same mistake one directory over
and on the money path: all four listings were wrapped in a bare
`except Exception` that logged a warning and returned an empty `Inventory`, and
`format_inventory` then printed

    BILLING NOW: nothing. No always-on compute in this workspace.

Driving `collect_inventory` with a client whose every attribute raises
`ResourceNotFoundError` reproduced that line byte for byte, with no Azure call.
The four warnings went to stderr as unstructured prose; the categorical claim
went to stdout. A participant who pipes the report, or screenshots it, sees
only the reassuring half -- and a wrong `FFSFT_RESOURCE_GROUP` (which defaults
rather than failing) produces exactly this state while an endpoint bills
$103/day in the workspace nobody read.

The second half of the same defect: "nothing" is a claim about one workspace,
and the report never said which. So the scope goes above the table, and it says
which values actually steered the query -- `get_ml_client` sends subscription,
resource group and workspace name and never sends the location.

No network and no Azure. The ML client is faked, matching tests/test_lifecycle.py.
"""

from __future__ import annotations

import argparse
import inspect

import ffsft.azure_ml
from ffsft.deploy import lifecycle
from ffsft.deploy.lifecycle import (
    Inventory,
    ScanStatus,
    SectionScan,
    collect_inventory,
    format_inventory,
)

PRICED_SKU = "Standard_NV36ads_A10_v5"

#: The exception the SDK raises for a workspace that is not in the resource
#: group it was asked for -- spelled as azure.core spells it, because the type
#: name is what tells a reader whether to re-check the rg or the identity.
class ResourceNotFoundError(Exception):
    pass


NOT_FOUND = "workspace 'mlw-ffsft' not found in rg 'rg-ffsft-kc'"
FORBIDDEN = "(AuthorizationFailed) the identity cannot read computes"


class FakeTarget:
    """Enough of `AzureTarget` for the header. Duck-typed on purpose: the real
    one lives in `ffsft.azure_ml`, which pulls the model registry in with it."""

    subscription_id = "11111111-2222-3333-4444-555555555555"
    resource_group = "rg-ffsft-kc"
    workspace_name = "mlw-ffsft"
    location = "koreacentral"


class FakeEndpoint:
    def __init__(self, name):
        self.name = name


class FakeDeployment:
    def __init__(self, name, instance_type=PRICED_SKU, instance_count=1):
        self.name = name
        self.instance_type = instance_type
        self.instance_count = instance_count


class FakeEmpty:
    def list(self, *a, **kw):
        return []


class Exploding:
    """A listing that raises, the way a missing workspace or a missing role does."""

    def __init__(self, message):
        self.message = message

    def list(self, *a, **kw):
        raise ResourceNotFoundError(self.message)


class FakeMLClient:
    def __init__(self, *, online=None, deployments=None, compute=None):
        self.online_endpoints = online or FakeEmpty()
        self.online_deployments = deployments or FakeEmpty()
        self.compute = compute or FakeEmpty()
        self.jobs = FakeEmpty()
        self.batch_endpoints = FakeEmpty()


class OneEndpoint:
    def list(self, *a, **kw):
        return [FakeEndpoint("ffsft-qwen")]


class OneDeployment:
    def list(self, endpoint_name, *a, **kw):
        return [FakeDeployment("blue")]


class BlindClient:
    """Every listing fails. This is the wrong-resource-group case."""

    def __getattr__(self, name):
        raise ResourceNotFoundError(NOT_FOUND)


def blind_report():
    return format_inventory(collect_inventory(BlindClient()), FakeTarget())


# --- the report may not turn a failed look into a clean workspace -----------


def test_a_workspace_that_could_not_be_listed_is_never_reported_as_nothing():
    out = blind_report()
    assert "nothing" not in out.lower(), out
    assert "could not look" in out.lower(), out


def test_a_failed_look_names_which_listings_failed_and_why():
    """"1 listing failed" sends you to the scrollback; naming it tells you
    whether a GPU cluster or a job queue is the thing you cannot see."""
    out = blind_report()
    for section in ("online endpoints", "batch endpoints", "compute clusters", "jobs"):
        assert section in out, (section, out)
    assert "ResourceNotFoundError" in out
    assert NOT_FOUND in out


def test_a_failed_look_never_implies_a_cost_of_zero():
    """The report is allowed to say it does not know. It is not allowed to
    price an unread workspace, which is what the empty table already did."""
    out = blind_report()
    assert "$0" not in out, out
    assert "0.000" not in out, out
    assert "/month" not in out, out


def test_collect_inventory_records_a_failed_listing_rather_than_only_logging_it():
    """The warning went to stderr and the claim went to stdout, so the two never
    met. The verdict has to live on the object the report is rendered from."""
    inv = collect_inventory(BlindClient())
    assert inv.items == []
    assert len(inv.failed_scans) == 4
    assert all(s.status is ScanStatus.FAILED for s in inv.failed_scans)
    assert all(NOT_FOUND in s.detail for s in inv.failed_scans)


def test_an_empty_listing_is_evidence_only_when_the_call_that_produced_it_succeeded():
    """The `LogRead.is_evidence` rule from deploy/logs.py, applied to listings."""
    assert SectionScan("jobs").is_evidence is True
    assert SectionScan("jobs", ScanStatus.FAILED, "boom").is_evidence is False


# --- partial failure: a total may only claim what it covers ----------------


def test_a_partial_failure_says_the_total_covers_only_what_could_be_listed():
    out = format_inventory(
        collect_inventory(
            FakeMLClient(
                online=OneEndpoint(), deployments=OneDeployment(), compute=Exploding(FORBIDDEN)
            )
        ),
        FakeTarget(),
    )
    # The half that listed is still reported in full -- an honest report is not
    # a vaguer one.
    assert "BILLING NOW: 1 resource(s)" in out
    assert "$4.320/hr" in out
    # ...and the half that did not is named, not absorbed into the count.
    assert "covers only what could be listed" in out
    assert "compute clusters" in out
    assert FORBIDDEN in out


def test_an_endpoint_whose_deployments_could_not_be_listed_is_not_called_a_shell():
    """The same categorical claim scoped to one row. `no deployments (endpoint
    shell only, no compute cost)` over an endpoint whose deployments raised is
    the sentence that leaves an A10 serving."""
    inv = collect_inventory(FakeMLClient(online=OneEndpoint(), deployments=Exploding(FORBIDDEN)))
    row = next(i for i in inv.items if i.name == "ffsft-qwen")
    assert "no compute cost" not in row.detail
    assert "could NOT be listed" in row.detail
    out = format_inventory(inv, FakeTarget())
    assert "nothing" not in out.lower(), out
    assert "deployments of ffsft-qwen" in out


# --- the true negative must survive ----------------------------------------


def test_a_clean_empty_workspace_still_reports_nothing():
    """The guarantee this whole change could have quietly destroyed: a workspace
    that really is idle must still say so, or `status` stops being worth running."""
    inv = collect_inventory(FakeMLClient())
    assert inv.failed_scans == []
    out = format_inventory(inv, FakeTarget())
    assert "BILLING NOW: nothing. No always-on compute in this workspace." in out
    assert "could not look" not in out.lower()


def test_a_priced_endpoint_in_a_fully_listed_workspace_still_reports_its_cost():
    """Guard on the other side: no coverage caveat when nothing was missed."""
    out = format_inventory(
        collect_inventory(FakeMLClient(online=OneEndpoint(), deployments=OneDeployment())),
        FakeTarget(),
    )
    assert "$4.320/hr" in out
    assert "COULD NOT LOOK" not in out


# --- the report says where it looked ---------------------------------------


def test_the_report_names_the_resource_group_and_workspace_it_read():
    out = format_inventory(Inventory(), FakeTarget())
    assert "rg-ffsft-kc" in out
    assert "mlw-ffsft" in out
    assert FakeTarget.subscription_id in out


def test_the_scope_is_printed_above_the_table_rather_than_after_the_verdict():
    """Below the table it is a footnote; above it, it is the subject of the
    sentence "BILLING NOW: nothing" turns out to be about."""
    out = format_inventory(Inventory(), FakeTarget())
    assert out.index("mlw-ffsft") < out.index("KIND")
    assert out.index("KIND") < out.index("BILLING NOW")


def test_the_header_does_not_let_the_location_take_the_blame_for_an_empty_table():
    """`FFSFT_LOCATION` is the first thing a participant suspects when the table
    is empty, and it is never the cause: `get_ml_client` does not send it."""
    out = format_inventory(Inventory(), FakeTarget())
    assert "koreacentral" in out
    assert "does not scope this read" in out
    source = inspect.getsource(ffsft.azure_ml.get_ml_client)
    assert "location" not in source, source
    assert "workspace_name" in source and "resource_group_name" in source


def test_a_report_rendered_without_a_target_admits_it_cannot_name_the_workspace():
    """Silence is what this whole module is about, so the missing case says so
    rather than dropping the line."""
    out = format_inventory(Inventory())
    assert "cannot name the workspace" in out


def test_status_names_the_workspace_it_looked_in(monkeypatch, capsys):
    """End to end through `cmd_status`, with the Azure module attributes faked --
    `read_orphans` included, because it builds a credential and calls ARM."""

    class StubTarget:
        @staticmethod
        def from_env():
            return FakeTarget()

    monkeypatch.setattr(ffsft.azure_ml, "AzureTarget", StubTarget)
    monkeypatch.setattr(ffsft.azure_ml, "get_ml_client", lambda target: BlindClient())
    monkeypatch.setattr(lifecycle, "read_orphans", lambda target, **kw: [])

    # Non-zero since round 4: the prose said "could not look" while the exit
    # code said success, so `ffsft lifecycle status && echo clean` printed
    # `clean` over four failed listings. See
    # tests/test_teardown_refuses_to_act_on_a_failed_look.py.
    assert lifecycle.cmd_status(argparse.Namespace()) == lifecycle.EXIT_COULD_NOT_LOOK
    out = capsys.readouterr().out
    assert "mlw-ffsft" in out
    assert "rg-ffsft-kc" in out
    assert "nothing" not in out.lower(), out
    assert "could not look" in out.lower(), out
