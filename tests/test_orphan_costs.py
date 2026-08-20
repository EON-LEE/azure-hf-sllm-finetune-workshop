"""A deleted VM leaves paid-for debris behind, and nothing was looking for it.

This file exists because of a real, measured leak. A spot A10 VM was deleted
during this project, and Azure kept billing for what it left behind:

    vm-a10-ffsft_OsDisk_...   256 GB Premium_LRS, Unattached   $38.01/month
    vm-a10-ffsftPublicIP      Standard static IPv4             $ 3.65/month

That is **$41.66/month for resources doing nothing**, and it survived every
teardown because `collect_inventory` talks to the AML workspace client, which
cannot see resource-group-level resources at all. The blind spot was structural,
not an oversight in any one call.

The prices below are not estimates. They came from the Azure Retail Prices API
for koreacentral:

    P15 LRS Disk                    38.012142 USD / 1 Month
    Standard IPv4 Static Public IP   0.005    USD / 1 Hour

**The trap that makes the naive check wrong.** The leaked public IP was *not*
unattached in the API's eyes -- it had a perfectly valid `ipConfiguration`
pointing at `vm-a10-ffsftVMNic`. Testing `ipConfiguration is None` would have
declared it healthy and left it billing forever. The NIC was the orphan; the IP
was attached to a corpse. So orphan detection has to be transitive, and
`test_public_ip_attached_to_orphaned_nic_is_still_an_orphan` is the whole
reason this module is not three lines long.
"""

from __future__ import annotations

import pytest

from ffsft.deploy.lifecycle import (
    disk_monthly_usd,
    orphan_items,
    public_ip_monthly_usd,
)

SUB = "/subscriptions/s/resourceGroups/rg-ffsft-kc/providers"
NIC_ID = f"{SUB}/Microsoft.Network/networkInterfaces/vm-a10-ffsftVMNic"


def disk(name="d", *, state="Unattached", managed_by=None, gb=256, sku="Premium_LRS"):
    return {
        "name": name,
        "properties": {"diskState": state, "diskSizeGB": gb},
        "sku": {"name": sku},
        "managedBy": managed_by,
    }


def nic(name="vm-a10-ffsftVMNic", *, vm=None):
    return {"name": name, "properties": {"virtualMachine": vm}}


def public_ip(name="ip", *, ip_config=None, sku="Standard"):
    props = {}
    if ip_config is not None:
        props["ipConfiguration"] = {"id": ip_config}
    return {"name": name, "properties": props, "sku": {"name": sku}}


# --------------------------------------------------------------------------
# Disks
# --------------------------------------------------------------------------


def test_unattached_disk_is_reported():
    items = orphan_items([disk("osdisk")], [], [])
    assert [i.name for i in items] == ["osdisk"]
    assert items[0].kind == "orphaned-disk"


def test_unattached_disk_bills_when_idle():
    """The whole point: it costs money while doing nothing."""
    (item,) = orphan_items([disk()], [], [])
    assert item.bills_when_idle is True
    assert item.monthly > 0


def test_attached_disk_is_not_reported():
    d = disk(state="Attached", managed_by=f"{SUB}/Microsoft.Compute/virtualMachines/vm")
    assert orphan_items([d], [], []) == []


def test_disk_with_owner_but_stale_state_is_not_reported():
    """`managedBy` set means some VM still claims it -- do not offer to delete."""
    d = disk(state="Unattached", managed_by=f"{SUB}/Microsoft.Compute/virtualMachines/v")
    assert orphan_items([d], [], []) == []


def test_the_real_leaked_disk_is_priced_from_the_retail_api():
    """256 GB Premium_LRS is a P15; the retail API said 38.012142 USD/month."""
    assert disk_monthly_usd(256, "Premium_LRS") == pytest.approx(38.012142)


@pytest.mark.parametrize(
    ("gb", "tier_price"),
    [
        (32, 5.2795),  # P4
        (64, 10.207),  # P6
        (128, 19.71),  # P10
        (256, 38.012142),  # P15 -- the one actually measured
        (512, 73.22),  # P20
        (1024, 135.17),  # P30
    ],
)
def test_premium_disk_price_follows_the_tier_not_the_byte(gb, tier_price):
    """Premium disks bill per *tier*, so 200 GB costs the same as 256 GB."""
    assert disk_monthly_usd(gb, "Premium_LRS") == pytest.approx(tier_price)


def test_disk_size_rounds_up_to_the_next_tier():
    """A 200 GB disk is billed as P15, exactly like a 256 GB one."""
    assert disk_monthly_usd(200, "Premium_LRS") == disk_monthly_usd(256, "Premium_LRS")


def test_unknown_disk_sku_is_reported_but_not_priced():
    """Report it -- an unpriced orphan is still an orphan. Never invent a price."""
    (item,) = orphan_items([disk(sku="UltraSSD_LRS")], [], [])
    assert item.monthly == 0.0
    assert "unknown" in item.detail.lower()


def test_absurdly_large_disk_does_not_crash():
    assert disk_monthly_usd(99999, "Premium_LRS") == 0.0


# --------------------------------------------------------------------------
# Public IPs -- including the transitive case that actually bit
# --------------------------------------------------------------------------


def test_public_ip_with_no_ip_configuration_is_an_orphan():
    items = orphan_items([], [public_ip("ip1")], [])
    assert [i.name for i in items] == ["ip1"]
    assert items[0].kind == "orphaned-public-ip"


def test_public_ip_attached_to_a_live_nic_is_left_alone():
    live = nic(vm={"id": f"{SUB}/Microsoft.Compute/virtualMachines/vm"})
    assert orphan_items([], [public_ip(ip_config=f"{NIC_ID}/ipConfigurations/c")], [live]) == []


def test_public_ip_attached_to_orphaned_nic_is_still_an_orphan():
    """The exact shape of the real leak.

    The IP had an `ipConfiguration`, so it looked attached. It pointed at a NIC
    whose `virtualMachine` was null -- the VM had been deleted out from under it.
    Both were billing. A non-transitive check reports zero orphans here, which is
    precisely the wrong answer.
    """
    items = orphan_items(
        [],
        [public_ip("vm-a10-ffsftPublicIP", ip_config=f"{NIC_ID}/ipConfigurations/c")],
        [nic(vm=None)],
    )
    assert [i.name for i in items] == ["vm-a10-ffsftPublicIP"]


def test_public_ip_pointing_at_a_nic_that_no_longer_exists_is_an_orphan():
    """Dangling reference: the NIC is not in the list at all."""
    ip = public_ip(ip_config=f"{NIC_ID}/ipConfigurations/c")
    assert len(orphan_items([], [ip], [])) == 1


def test_nic_lookup_is_case_insensitive():
    """ARM resource ids vary in case between APIs; matching must not be brittle."""
    ip = public_ip(ip_config=f"{NIC_ID.upper()}/ipConfigurations/c")
    live = nic(vm={"id": "/subscriptions/s/.../vm"})
    assert orphan_items([], [ip], [live]) == []


def test_standard_static_ip_price_comes_from_the_retail_api():
    """0.005 USD/hr x 730 hr."""
    assert public_ip_monthly_usd("Standard") == pytest.approx(0.005 * 730)


def test_basic_ip_is_cheaper_than_standard():
    assert public_ip_monthly_usd("Basic") < public_ip_monthly_usd("Standard")


def test_unknown_ip_sku_is_reported_but_not_priced():
    (item,) = orphan_items([], [public_ip(sku="Weird")], [])
    assert item.monthly == 0.0


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def test_the_measured_leak_totals_what_azure_actually_charged():
    """Reproduce the real incident end to end: $38.01 + $3.65 = $41.66/month."""
    items = orphan_items(
        [disk("vm-a10-ffsft_OsDisk_1_7954682c")],
        [public_ip("vm-a10-ffsftPublicIP", ip_config=f"{NIC_ID}/ipConfigurations/c")],
        [nic(vm=None)],
    )
    assert len(items) == 2
    assert sum(i.monthly for i in items) == pytest.approx(41.66, abs=0.05)


def test_detail_names_the_size_so_the_report_is_actionable():
    (item,) = orphan_items([disk(gb=256)], [], [])
    assert "256" in item.detail


def test_no_orphans_produces_no_items():
    assert orphan_items([], [], []) == []


def test_malformed_payloads_are_skipped_rather_than_raising():
    """A cost report must never be the reason you cannot see your costs."""
    assert orphan_items([{}, {"properties": None}], [{}], [{}]) is not None


# --------------------------------------------------------------------------
# `down` must not quietly delete these
# --------------------------------------------------------------------------


def test_teardown_never_touches_orphans():
    """Orphans bill while idle, so they land in `inv.billing` -- but `down` is
    for compute you intend to recreate. A disk deletion cannot be undone and no
    `up` command would bring it back, so teardown must ignore them entirely.
    """
    from ffsft.deploy.lifecycle import Inventory, teardown

    inv = Inventory(items=orphan_items([disk("osdisk")], [public_ip("ip")], []))
    assert inv.billing, "guard: these should count as billing"

    class ExplodingClient:
        def __getattr__(self, name):
            raise AssertionError(f"teardown touched the client via .{name}")

    assert teardown(ExplodingClient(), inv, dry_run=False) == []


def test_report_prints_the_delete_command_for_each_orphan():
    from ffsft.deploy.lifecycle import Inventory, format_inventory

    out = format_inventory(
        Inventory(items=orphan_items([disk("osdisk")], [public_ip("ip1")], []))
    )
    assert "az disk delete" in out
    assert "osdisk" in out
    assert "az network public-ip delete" in out
    assert "LEFTOVERS" in out


def test_report_stays_quiet_when_there_are_no_orphans():
    from ffsft.deploy.lifecycle import Inventory, format_inventory

    assert "LEFTOVERS" not in format_inventory(Inventory(items=[]))
