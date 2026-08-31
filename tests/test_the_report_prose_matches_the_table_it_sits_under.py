""""Could not look" is only honest while the sentence saying it is also true.

`format_inventory` refuses to price an unread workspace (round 3) and refuses to
exit 0 over one (round 4). What was still shipping is the same family one level
up: prose that says more than the rows under it support. Three of them, each
reproduced by rendering a fake `Inventory` -- no network, no Azure:

**P-1 -- "the empty table above" over a table with rows in it.** `collect_inventory`
emits a visible row for an endpoint whose deployments listing raised
("deployments could NOT be listed -- what runs here is unknown"), and that is
*exactly* the state that reaches the `BILLING NOW: UNKNOWN` branch, because the
row does not bill when idle so `inv.billing` is empty. The report printed

    online-endpoint  ffsft-a10  ...  deployments could NOT be listed -- what runs here is unknown
    BILLING NOW: UNKNOWN -- could not look. 1 of 5 listing(s) failed, so the empty
    table above is silence, not evidence.

An operator who can see a row being called an empty table stops believing the
rest of the paragraph, which is the half that matters.

**P-2 -- the free glyph on a row nobody priced.** The same dark-endpoint row
printed `-` in the `$/hr` column. This file defines that glyph: "'-' is the free
column and would be read as free", which is why an unknown rate already has `?`.
A dark endpoint may be serving `Standard_NC24ads_A100_v4` at $4.959/hr. The
running-job row had it too, over a note that says "consuming cluster nodes".

**P-3 -- two sentences aimed at the wrong scan.** With every AML listing fine and
only the resource-group scan failed, the report closed with "an unreadable
workspace is not an idle one" -- the workspace read fine, the resource group did
not -- and printed the identical `COULD NOT LOOK at 1 listing(s): orphaned
disks/IPs (resource group) (...)` line twice, once under BILLING NOW and once
under LEFTOVERS.
"""

from __future__ import annotations

from ffsft.deploy.lifecycle import (
    ORPHANS_SECTION,
    BillingItem,
    Inventory,
    ScanStatus,
    SectionScan,
    collect_inventory,
    format_inventory,
    read_orphans,
)

#: The `$/hr` cell as the row format lays it out, so a test asserts the glyph in
#: the column the header labels rather than anywhere in the line.
UNKNOWN_CELL = f"{'?':>8}  "
FREE_CELL = f"{'-':>8}  "

FORBIDDEN = "(AuthorizationFailed) the identity cannot list deployments"
NO_READER = "(AuthorizationFailed) no Reader on rg-ffsft-kc"
NO_COMPUTES = "(AuthorizationFailed) the identity cannot read computes"


class FakeTarget:
    subscription_id = "11111111-2222-3333-4444-555555555555"
    resource_group = "rg-ffsft-kc"
    workspace_name = "mlw-ffsft"
    location = "koreacentral"


class ForbiddenCredential:
    """`read_orphans`' seam. Raises where the ARM token is fetched, which is the
    403 a subscription-scoped identity with no Reader on the rg actually gets."""

    def get_token(self, *scopes, **kwargs):
        raise RuntimeError(NO_READER)


class FakeEndpoint:
    def __init__(self, name):
        self.name = name


class FakeJob:
    def __init__(self, name, status="Running"):
        self.name = name
        self.status = status


class FakeCompute:
    def __init__(self, name, size, min_instances=0):
        self.name = name
        self.type = "amlcompute"
        self.size = size
        self.min_instances = min_instances
        self.tier = "dedicated"


class FakeEmpty:
    def list(self, *a, **kw):
        return []


class FakeList:
    def __init__(self, rows):
        self._rows = list(rows)

    def list(self, *a, **kw):
        return list(self._rows)


class ExplodingDeployments:
    def list(self, endpoint_name, *a, **kw):
        raise RuntimeError(FORBIDDEN)


class ExplodingComputes:
    def list(self, *a, **kw):
        raise RuntimeError(NO_COMPUTES)


class FakeMLClient:
    def __init__(self, *, online=(), deployments=None, computes=(), jobs=(), batch=()):
        self.online_endpoints = FakeList([FakeEndpoint(n) for n in online])
        self.online_deployments = deployments or FakeEmpty()
        self.compute = computes if hasattr(computes, "list") else FakeList(computes)
        self.jobs = FakeList(jobs)
        self.batch_endpoints = FakeList([FakeEndpoint(n) for n in batch])


def dark_endpoint_report() -> str:
    """One endpoint that lists, whose deployments do not, plus a running job."""
    client = FakeMLClient(
        online=["ffsft-a10"], deployments=ExplodingDeployments(), jobs=[FakeJob("qlora-27b")]
    )
    return format_inventory(collect_inventory(client), FakeTarget())


def orphan_scan_only_report() -> str:
    """Every AML listing returned and returned empty; only the ARM scan failed."""
    inv = collect_inventory(FakeMLClient())
    inv.items.extend(read_orphans(FakeTarget(), credential=ForbiddenCredential(), inv=inv))
    return format_inventory(inv, FakeTarget())


# --- P-1: the sentence may not describe a table that is not there ----------


def test_a_table_with_a_dark_endpoint_row_in_it_is_never_called_the_empty_table():
    out = dark_endpoint_report()
    # The row is really there, three lines above the sentence about it.
    assert "online-endpoint    ffsft-a10" in out, out
    assert "empty table" not in out, out


def test_the_could_not_look_verdict_says_what_is_missing_from_the_table_not_that_it_is_empty():
    out = dark_endpoint_report()
    assert (
        "BILLING NOW: UNKNOWN -- could not look. 1 of 5 listing(s) failed, so what is "
        "missing from the table above is silence, not evidence." in out
    ), out


def test_the_same_verdict_still_reads_true_when_the_table_really_is_empty():
    """The wording has to survive both states or it just moved the false half."""
    inv = collect_inventory(FakeMLClient(online=["ffsft-a10"], deployments=ExplodingDeployments()))
    inv.items.clear()
    out = format_inventory(inv, FakeTarget())
    assert "so what is missing from the table above is silence, not evidence." in out, out


# --- P-2: "-" is the free column and only free rows may have it -------------


def test_an_endpoint_whose_deployments_could_not_be_listed_is_not_priced_at_free():
    out = dark_endpoint_report()
    dark = "deployments could NOT be listed -- what runs here is unknown"
    assert UNKNOWN_CELL + dark in out, out
    assert FREE_CELL + dark not in out, out


def test_the_dark_endpoint_row_does_not_claim_a_sku_it_never_read():
    """`?` earns the NOTE `(price unknown for this SKU)` on a row that has a SKU.
    This row has none -- its own note already says what was not read."""
    out = dark_endpoint_report()
    assert "price unknown for this SKU" not in out, out


def test_a_running_job_consuming_cluster_nodes_is_not_priced_at_free_either():
    out = dark_endpoint_report()
    job = "status=Running: consuming cluster nodes"
    assert UNKNOWN_CELL + job in out, out
    assert FREE_CELL + job not in out, out


def test_a_row_that_genuinely_costs_nothing_idle_keeps_the_free_glyph():
    """The guard on the other side: if everything prints `?` the column stops
    carrying the one distinction it exists for."""
    out = format_inventory(
        collect_inventory(
            FakeMLClient(computes=[FakeCompute("gpu", "Standard_NC24ads_A100_v4")], batch=["b1"])
        ),
        FakeTarget(),
    )
    assert FREE_CELL + "min_instances=0 (dedicated): idle costs nothing" in out, out
    assert FREE_CELL + "runs on AmlCompute; scales to 0 between jobs" in out, out


# --- P-3: the closing line and the duplicate COULD NOT LOOK ------------------


def test_a_failed_resource_group_scan_does_not_call_the_workspace_unreadable():
    out = orphan_scan_only_report()
    assert "Fix the errors above and re-run: an unread resource group is not a clean one." in out
    assert "unreadable workspace" not in out, out


def test_a_failed_workspace_listing_still_calls_the_workspace_unreadable():
    """The other direction, or the fix just swapped which case lies."""
    out = dark_endpoint_report()
    assert "Fix the errors above and re-run: an unreadable workspace is not an idle one." in out
    assert "unread resource group" not in out, out


def test_the_failed_resource_group_scan_is_named_once_rather_than_twice():
    out = orphan_scan_only_report()
    named = f"COULD NOT LOOK at 1 listing(s): {ORPHANS_SECTION} (RuntimeError: {NO_READER})"
    assert out.count(named) == 1, out
    # ...and the one copy is the LEFTOVERS one, next to the block it explains.
    assert out.index(named) > out.index("LEFTOVERS: UNKNOWN"), out


def test_the_billing_verdict_points_at_the_leftovers_block_instead_of_repeating_it():
    out = orphan_scan_only_report()
    assert "  The resource-group scan is what failed; LEFTOVERS names it below." in out, out


def test_a_failure_the_leftovers_block_will_not_name_is_still_named_under_billing_now():
    """Dropping the duplicate may not drop the naming: an AML listing has no
    second block to be named in."""
    out = dark_endpoint_report()
    named = (
        f"  COULD NOT LOOK at 1 listing(s): deployments of ffsft-a10 (RuntimeError: {FORBIDDEN})"
    )
    assert named in out, out


def test_a_count_that_a_failed_orphan_scan_undercuts_still_says_it_is_a_floor():
    """The billing half of the same split: rows were listed, so there is a count,
    and the resource-group scan that failed is what makes it a floor."""
    inv = Inventory(
        items=[
            BillingItem(
                kind="online-deployment",
                name="ffsft-a10/blue",
                detail="managed online endpoint: NO scale-to-zero, bills 24/7",
                sku="Standard_NC24ads_A100_v4",
                instances=1,
                bills_when_idle=True,
            )
        ]
    )
    read_orphans(FakeTarget(), credential=ForbiddenCredential(), inv=inv)
    out = format_inventory(inv, FakeTarget())
    assert (
        "  the count covers only what could be listed. The resource-group scan is what "
        "failed; LEFTOVERS names it below." in out
    ), out
    assert out.count("COULD NOT LOOK") == 1, out


def test_both_halves_failing_names_the_workspace_listing_and_points_at_the_other():
    """The count says two listings failed, so exactly two must be accounted for:
    one named here, one handed to the block that names it in context."""
    inv = collect_inventory(FakeMLClient(computes=ExplodingComputes()))
    read_orphans(FakeTarget(), credential=ForbiddenCredential(), inv=inv)
    out = format_inventory(inv, FakeTarget())
    assert "2 of 5 listing(s) failed" in out, out
    aml = f"  COULD NOT LOOK at 1 listing(s): compute clusters (RuntimeError: {NO_COMPUTES})"
    assert aml in out, out
    assert "  The resource-group scan failed too; LEFTOVERS names it below." in out, out
    # Named once each, not once here and once again below.
    assert out.count(ORPHANS_SECTION) == 1, out
    assert out.count("compute clusters") == 1, out
    # And the workspace really was unreadable this time, so that wording stands.
    assert "Fix the errors above and re-run: an unreadable workspace is not an idle one." in out


def test_an_orphan_scan_failure_the_leftovers_block_does_not_mention_is_still_named():
    """Dropping the duplicate is conditional on the other block actually printing
    it. With rows to show, LEFTOVERS shows those instead and says nothing about
    the scan -- so BILLING NOW has to keep naming it or the failure vanishes."""
    inv = Inventory(
        items=[
            BillingItem(
                kind="orphaned-disk",
                name="osdisk-from-a-deleted-vm",
                detail="128 GB Premium_LRS, unattached",
                sku="Premium_LRS",
                bills_when_idle=True,
                monthly_usd=19.71,
            )
        ],
        scans=[
            SectionScan(ORPHANS_SECTION, ScanStatus.FAILED, "RuntimeError: 403 on the second page")
        ],
    )
    out = format_inventory(inv, FakeTarget())
    named = (
        f"COULD NOT LOOK at 1 listing(s): {ORPHANS_SECTION} "
        "(RuntimeError: 403 on the second page)"
    )
    assert out.count(named) == 1, out
    assert out.index(named) < out.index("LEFTOVERS: 1 resource(s)"), out
    assert "LEFTOVERS names it below" not in out, out
