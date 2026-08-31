"""The scope header may not claim coverage of rows it does not govern.

Round 3 gave `status` a header naming the workspace that answered, and closed it
with "that triple is the whole query". That is true of `get_ml_client` and false
of the table underneath it: `cmd_status` appends `read_orphans` rows, and
`read_orphans` deliberately bypasses the AML client -- disks, NICs and public IPs
are resource-group resources, which is the structural reason `collect_inventory`
can never see them.

The overstatement runs in the one direction that hides money. A reader who
believes the triple governs everything reads the LEFTOVERS block as
workspace-scoped, so an empty one says "this workspace left nothing behind" --
and the leak that block exists for was a $41.66/month disk sitting in the
resource group, belonging to no workspace at all (JOURNAL §11.4).

These tests are about the WORDING, so they assert on the wording. A header that
says less than the report covers is a lesser bug than one that says more, so the
claim is pinned from both sides: it must not overclaim, and it must still name
the resource group the leftovers came from.
"""

from __future__ import annotations

from ffsft.azure_ml import AzureTarget
from ffsft.deploy.lifecycle import BillingItem, Inventory, format_inventory
from ffsft.deploy.preflight import AML_CLIENT_SCOPE, scope_lines

TARGET = AzureTarget(
    subscription_id="sub-participant",
    resource_group="rg-mine",
    workspace_name="mlw-mine",
    location="koreacentral",
)


def _report_with_a_leftover_disk() -> str:
    return format_inventory(
        Inventory(
            items=[
                BillingItem(
                    kind="orphaned-disk",
                    name="osdisk-from-a-deleted-vm",
                    detail="unattached",
                    bills_when_idle=True,
                    monthly_usd=41.66,
                )
            ]
        ),
        TARGET,
    )


def test_the_header_no_longer_calls_the_workspace_triple_the_whole_query():
    out = _report_with_a_leftover_disk()

    assert "the whole query" not in out
    assert "the entire query" not in out


def test_the_header_says_the_leftovers_rows_come_from_a_separate_resource_group_scan():
    out = _report_with_a_leftover_disk()

    # Both halves matter: the name of the block whose scope differs, and the
    # scope it actually has. A reader has to be able to get from one to the
    # other without opening `read_orphans`.
    assert "LEFTOVERS" in out
    header = out.split("KIND")[0]
    assert "LEFTOVERS does not" in header
    assert "rg-mine" in header
    assert "no workspace" in header


def test_the_header_still_says_the_location_is_sent_by_neither_read():
    # The true half of the original wording, and worth keeping: FFSFT_LOCATION
    # reaches neither `get_ml_client` nor the resource-group scan, so "my
    # endpoint is in another region" cannot explain a row missing from here.
    out = _report_with_a_leftover_disk()

    assert "FFSFT_LOCATION=koreacentral is sent by neither" in out


def test_the_status_header_and_the_check_header_share_their_identity_lines():
    # The two reports are entitled to different scope clauses and to exactly the
    # same statement of who answered. One helper, so a fix to the wording of
    # "who answered" cannot land in only one of them.
    from ffsft.deploy.preflight import QUOTA_SCOPE

    assert scope_lines(TARGET, AML_CLIENT_SCOPE)[:2] == scope_lines(TARGET, QUOTA_SCOPE)[:2]


def test_a_blank_resource_group_is_printed_as_unset_rather_than_as_nothing():
    # `rg=''` is the shape a blanked `~/.ffsft-env` produces, and an empty gap in
    # the header would read as a formatting quirk rather than as the cause.
    blank = AzureTarget(subscription_id="sub", resource_group="", workspace_name="mlw-mine")

    assert "resource group (unset)" in scope_lines(blank, AML_CLIENT_SCOPE)[0]
