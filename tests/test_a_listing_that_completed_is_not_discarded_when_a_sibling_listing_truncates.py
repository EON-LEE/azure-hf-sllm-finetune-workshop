"""A disk that WAS read was reported as a resource group nobody could look at.

Round 7 fixed "could not look, reported as looked-and-saw-nothing" in this same
function. This is the same invariant running BACKWARDS: "looked and FOUND,
reported as could not look". `read_orphans` issued its three ARM listings as
three arguments to one call --

    items = orphan_items(
        fetch("Microsoft.Compute/disks", "2023-04-02"),
        fetch("Microsoft.Network/publicIPAddresses", "2023-09-01"),
        fetch("Microsoft.Network/networkInterfaces", "2023-09-01"),
    )

-- and Python evaluates all three before it calls, so one raising threw away the
two that had already returned. Executed against the pre-fix module with a
labelled fake: the disks listing COMPLETE and holding the 256 GB Premium_LRS
unattached disk of JOURNAL §11.4, the NIC listing 403ing on its second page:

    could not list orphaned disks/IPs (resource group): 403 fake ARM error
    read_orphans returned : []
    failed_scans          : ['orphaned disks/IPs (resource group) (RuntimeError: 403 ...)']

This fails toward alarm, not toward silence -- rc=1, no "meter stopped.", and
`down --all --yes && echo clean` stays quiet -- so it is far less dangerous than
what round 7 fixed, and the failure path is deliberately NOT weakened here to
rescue the rows. What it costs is the disk's NAME. The operator is told the scan
did not happen, when one of the three listings did happen and found the thing
they have to delete, and they lose their `az disk delete vm-a10-ffsft_OsDisk_1`.

The fix makes the three listings three independent reads. A section may hold
measured rows AND an unread listing at once, and every consumer downstream
already handles that pair: `format_inventory` prints "the count covers only what
could be listed" next to the rows, and `closing()` prints both the LEFTOVERS
block and the UNKNOWN sentence and still returns EXIT_COULD_NOT_LOOK.

The trap on the other side is `orphan_items`' transitive public-IP rule. An IP
that carries an `ipConfiguration` is leaked only when the NIC it points at has
no VM, and ONLY the NIC listing answers that. Handing the classifier an empty
NIC list *because the NIC listing did not complete* would report every attached
IP in the resource group as an orphan and print an `az network public-ip delete`
for an address that is doing its job -- this round's invariant pointing the
other way. Those rows are withheld and counted in `scan.detail` instead.

No network and no Azure: `requests.get`, the ML client and the credential are
all faked, so `read_orphans` and `cmd_down` run for real over invented JSON.
"""

from __future__ import annotations

import requests

import ffsft.azure_ml
from ffsft.deploy import lifecycle
from ffsft.deploy.lifecycle import (
    EXIT_COULD_NOT_LOOK,
    ORPHANS_SECTION,
    Inventory,
    ScanStatus,
    read_orphans,
)

#: The replayed leak, as ARM returns it -- same row as the sibling pagination
#: file, so the only thing this file changes about the resource group is which
#: of the three listings failed.
LEAKED_DISK = {
    "name": "vm-a10-ffsft_OsDisk_1",
    "properties": {"diskState": "Unattached", "diskSizeGB": 256},
    "sku": {"name": "Premium_LRS"},
    "managedBy": None,
}
NIC_ID = (
    "/subscriptions/s/resourceGroups/rg-ffsft-kc/providers"
    "/Microsoft.Network/networkInterfaces/vm-live-ffsftVMNic"
)
#: A NIC with a VM still on it. The IP below is healthy *because of this row*,
#: which is why an unread NIC listing may not judge that IP either way.
LIVE_NIC = {"name": "vm-live-ffsftVMNic", "properties": {"virtualMachine": {"id": "/vm/live"}}}
ATTACHED_IP = {
    "name": "vm-live-ffsftPublicIP",
    "properties": {"ipConfiguration": {"id": f"{NIC_ID}/ipConfigurations/c"}},
    "sku": {"name": "Standard"},
}
#: No `ipConfiguration` at all: attached to nothing, which the IP listing states
#: by itself and no NIC can contradict.
LOOSE_IP = {"name": "stray-ffsftPublicIP", "properties": {}, "sku": {"name": "Standard"}}

DISKS = "Microsoft.Compute/disks"
IPS = "Microsoft.Network/publicIPAddresses"
NICS = "Microsoft.Network/networkInterfaces"

#: Marks a listing whose second page cannot be fetched, as opposed to a list of
#: rows, which is a listing that completed.
FAILED = "the second page 403s"


class FakeTarget:
    subscription_id = "11111111-2222-3333-4444-555555555555"
    resource_group = "rg-ffsft-kc"
    workspace_name = "mlw-ffsft"
    location = "koreacentral"

    @staticmethod
    def from_env():
        return FakeTarget()


class FakeResponse:
    """200 with an ARM-shaped body. `raise_for_status` returns."""

    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def arm(*, disks, ips, nics):
    """Stand in for `requests.get`. Each argument is either a list of rows --
    one COMPLETE page, no `nextLink` -- or `FAILED`, a page 1 that promises a
    page 2 the fake then refuses. That second shape is the whole point: it
    arrives as HTTP 200 followed by a failure, which is how a listing stops
    short without anything having gone wrong on page 1.

    Patched on the `requests` module itself, never on `lifecycle`: the import in
    `read_orphans` is function-local.
    """
    plan = {DISKS: disks, IPS: ips, NICS: nics}

    def get(url, headers=None, timeout=None, **kw):
        for path, rows in plan.items():
            if path not in url:
                continue
            if rows is not FAILED:
                return FakeResponse({"value": list(rows)})
            if "/page2" not in url:
                return FakeResponse({"value": [], "nextLink": f"{url}/page2"})
            raise RuntimeError("(AuthorizationFailed) no Reader on the second page")
        raise AssertionError(f"unexpected ARM url: {url}")

    return get


class OKCredential:
    def get_token(self, *scopes, **kw):
        return type("T", (), {"token": "fake-token"})()


class FakeEmpty:
    def list(self, *a, **kw):
        return []


class FakeMLClient:
    """A workspace with nothing in it, so the resource-group scan is the only
    thing in the report that can say anything at all."""

    def __init__(self):
        self.online_endpoints = FakeEmpty()
        self.online_deployments = FakeEmpty()
        self.compute = FakeEmpty()
        self.jobs = FakeEmpty()
        self.batch_endpoints = FakeEmpty()


def scan_rg(monkeypatch, *, disks, ips, nics):
    """Run the REAL `read_orphans` over a faked ARM. Returns (rows, scan)."""
    monkeypatch.setattr(requests, "get", arm(disks=disks, ips=ips, nics=nics))
    inv = Inventory()
    rows = read_orphans(FakeTarget(), credential=OKCredential(), inv=inv)
    (scan,) = [s for s in inv.scans if s.section == ORPHANS_SECTION]
    return rows, scan


def run_down(monkeypatch, *, disks, ips, nics):
    """Drive the real `ffsft-lifecycle down --all --yes` with every seam faked."""
    monkeypatch.setattr(lifecycle, "quiet_azure_sdk_logs", lambda *a, **k: None)
    monkeypatch.setattr(ffsft.azure_ml, "AzureTarget", FakeTarget)
    monkeypatch.setattr(ffsft.azure_ml, "get_ml_client", lambda target: FakeMLClient())
    monkeypatch.setattr(requests, "get", arm(disks=disks, ips=ips, nics=nics))
    real = lifecycle.read_orphans
    monkeypatch.setattr(
        lifecycle,
        "read_orphans",
        lambda target, **kw: real(target, credential=OKCredential(), **kw),
    )
    monkeypatch.setattr("sys.argv", ["ffsft-lifecycle", "down", "--all", "--yes"])
    return lifecycle.main()


# --- the defect: a completed listing thrown away by a sibling ----------------


def test_a_disk_from_a_completed_listing_survives_a_sibling_listing_that_stopped_short(
    monkeypatch,
):
    """The money statement. The disks listing returned one complete page holding
    the leak; the NIC listing stopped short. Pre-fix this returned []."""
    rows, _ = scan_rg(monkeypatch, disks=[LEAKED_DISK], ips=[], nics=FAILED)
    assert [r.name for r in rows] == ["vm-a10-ffsft_OsDisk_1"]


def test_the_section_still_reports_it_could_not_look_while_it_names_the_disk(monkeypatch):
    """Both halves at once, which is the shape the fix exists to allow. Rows are
    evidence AND the section is not, so nothing downstream may read the absence
    of a second row as a clean resource group."""
    rows, scan = scan_rg(monkeypatch, disks=[LEAKED_DISK], ips=[], nics=FAILED)
    assert rows, "the disk was read and must be reported"
    assert scan.status is ScanStatus.FAILED
    assert scan.is_evidence is False


def test_the_recorded_detail_names_which_of_the_three_listings_did_not_complete(monkeypatch):
    """"1 listing failed" sends the operator to the scrollback; naming the NIC
    listing tells them which `az` call to make. The exception TYPE stays first,
    because that is what separates an unfinished list from a 403."""
    _, scan = scan_rg(monkeypatch, disks=[LEAKED_DISK], ips=[], nics=FAILED)
    assert scan.detail.startswith("RuntimeError:"), scan.detail
    assert "network interfaces" in scan.detail, scan.detail
    assert "disks" not in scan.detail, "the disks listing completed; do not accuse it"


def test_each_of_the_three_listings_is_named_when_all_three_stop_short(monkeypatch):
    """The detail is per listing, not per section, so three failures read as
    three failures rather than as one anonymous one."""
    _, scan = scan_rg(monkeypatch, disks=FAILED, ips=FAILED, nics=FAILED)
    for listing in ("disks", "public IPs", "network interfaces"):
        assert listing in scan.detail, scan.detail


def test_a_failure_in_the_disks_listing_still_leaves_the_public_ip_it_never_touched(
    monkeypatch,
):
    """Independence in the other direction: the IP listing is not collateral
    damage from the disks listing, which is the same defect transposed."""
    rows, scan = scan_rg(monkeypatch, disks=FAILED, ips=[LOOSE_IP], nics=[])
    assert [r.name for r in rows] == ["stray-ffsftPublicIP"]
    assert scan.is_evidence is False
    assert "disks" in scan.detail, scan.detail


# --- the over-correction this must not commit --------------------------------


def test_a_public_ip_on_a_nic_nobody_could_list_is_not_called_an_orphan(monkeypatch):
    """The trap. `orphan_items` clears an attached IP only by finding its NIC
    alive, so an empty NIC list turns every attached IP into a leak. Reporting
    this IP would print `az network public-ip delete` for a live address --
    this round's invariant pointing the other way."""
    rows, scan = scan_rg(monkeypatch, disks=[], ips=[ATTACHED_IP], nics=FAILED)
    assert rows == [], "an IP whose NIC was never listed is not a measured orphan"
    assert scan.is_evidence is False


def test_the_public_ips_withheld_for_that_reason_are_counted_rather_than_dropped(monkeypatch):
    """Withholding is only honest if it is said out loud. The count rides on the
    NIC listing's own failure note, because that failure is what caused it."""
    _, scan = scan_rg(monkeypatch, disks=[], ips=[ATTACHED_IP], nics=FAILED)
    assert "1 attached public IP(s) could not be judged" in scan.detail, scan.detail


def test_a_public_ip_attached_to_nothing_survives_an_unread_nic_listing(monkeypatch):
    """The other half: an IP with no `ipConfiguration` is attached to nothing at
    all, which the IP listing states by itself, so no NIC can contradict it and
    withholding it would be the discard defect all over again."""
    rows, _ = scan_rg(monkeypatch, disks=[], ips=[LOOSE_IP], nics=FAILED)
    assert [r.name for r in rows] == ["stray-ffsftPublicIP"]


def test_a_live_public_ip_is_still_cleared_when_the_nic_listing_did_complete(monkeypatch):
    """The true negative the withholding rule must not destroy: with the NICs
    read, the transitive rule works exactly as it always did."""
    rows, scan = scan_rg(monkeypatch, disks=[], ips=[ATTACHED_IP], nics=[LIVE_NIC])
    assert rows == []
    assert scan.is_evidence is True, "nothing failed; this is a measurement"


# --- the money path: what the operator and the shell are told -----------------


def test_down_all_yes_names_the_disk_and_still_refuses_to_say_the_meter_stopped(
    monkeypatch, capsys
):
    """The executed line this file exists for. Both facts reach the operator in
    one run: the disk they must delete, and the listing that did not finish."""
    code = run_down(monkeypatch, disks=[LEAKED_DISK], ips=[], nics=FAILED)
    out = capsys.readouterr().out
    assert "vm-a10-ffsft_OsDisk_1" in out, out
    assert "az disk delete" in out, out
    assert "network interfaces" in out, out
    assert "meter stopped." not in out, out
    assert code == EXIT_COULD_NOT_LOOK, out


def test_an_unread_listing_still_outranks_a_leftover_that_was_found(monkeypatch, capsys):
    """Round 5's priority, unchanged by this round. The operator cannot act on a
    leak list that is admittedly incomplete, so could-not-look beats not-idle
    even now that both are true at once."""
    code = run_down(monkeypatch, disks=[LEAKED_DISK], ips=[], nics=FAILED)
    out = capsys.readouterr().out
    assert code == EXIT_COULD_NOT_LOOK, out
    assert code != lifecycle.EXIT_NOT_IDLE


def test_the_unknown_sentence_no_longer_says_a_scan_that_happened_did_not(monkeypatch, capsys):
    """`closing()` printed "the scan above did not happen" directly under the
    `az disk delete` for a disk that had just been read. That was written when
    one raising listing discarded the other two, so there was never anything
    above it to contradict."""
    run_down(monkeypatch, disks=[LEAKED_DISK], ips=[], nics=FAILED)
    out = capsys.readouterr().out
    assert "the scan above did not happen" not in out, out
    assert "at least one of the" in out, out


def test_a_resource_group_whose_three_listings_all_completed_is_still_a_measurement(
    monkeypatch, capsys
):
    """The true negative the whole change must not destroy: nothing failed, so
    the empty table is evidence and `down` is still allowed its strongest
    sentence."""
    code = run_down(monkeypatch, disks=[], ips=[], nics=[])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "meter stopped." in out, out
    assert "COULD NOT LOOK" not in out, out
