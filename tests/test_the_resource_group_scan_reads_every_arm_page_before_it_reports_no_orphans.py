"""A leaked disk on page 2 of the ARM listing was read as "no orphans", rc=0.

Round 6 closed the shape where an `except` swallows a failure and hands the
caller an empty value. `read_orphans` was fixed for that shape in round 3 and
still had the OTHER shape underneath it: a read that SUCCEEDED and was
INCOMPLETE. ARM's list-by-resource-group is paginated -- the body carries
`value` and, when more exist, `nextLink` -- and `fetch` read only `value`:

    def fetch(path: str, api: str) -> list:
        resp = requests.get(f"{base}/{path}?api-version={api}", ...)
        resp.raise_for_status()
        return resp.json().get("value", [])

Nothing raises. HTTP 200, `raise_for_status()` returns, `_section` records the
scan as OK, and the empty page 1 is then reported as a measured fact. Executed
against the pre-fix module with a fake `requests` serving ARM-shaped JSON --
page 1 `{"value": [], "nextLink": ...}`, page 2 the 256 GB Premium_LRS disk from
JOURNAL §11.4 -- a fake empty ML client and a fake credential, running the REAL
`cmd_down --all --yes`:

    BILLING NOW: nothing. No always-on compute in this workspace.

    meter stopped. `ffsft lifecycle status` to confirm.
    >>> EXIT CODE = 0   (EXIT_NOT_IDLE=3, EXIT_COULD_NOT_LOOK=1)
    >>> shell would print 'clean'

So `ffsft lifecycle down --all --yes && echo clean` printed `clean` over a live
$41.66/month leak. Same fake, same second, the ONLY variable being whether ARM
paged the disk -- the disk moved onto page 1 and nothing else changed:

    !!orphaned-disk      vm-a10-ffsft_OsDisk_1     Premium_LRS   0.052
    NOT idle: 1 leftover resource(s) from deleted VMs are still
    >>> EXIT CODE = 3

That A/B is the whole defect: `nextLink` alone flips a teardown between "NOT
idle, go delete this disk" and "meter stopped." This is the THIRD round in which
this one money path produced a false all-clear (JOURNAL §11.4 was a 403,
round 3's was a swallowed exception, this one is a 200), so the fix routes it
through the helper round 6 built for exactly this -- `read_all_arm_pages`, which
raises `TruncatedListing` rather than returning a short list -- and the
truncation lands in `_section`'s handler as a FAILED scan. A truncated listing
is not a third `ScanStatus`: `is_evidence` asks one boolean question, "does an
absence of rows here mean anything", and a short read answers no exactly as a
403 does. The report already tells the two apart through `scan.detail`, which
carries the exception type.

No network and no Azure: `requests.get`, the ML client and the credential are
all faked, so `read_orphans` runs for real over invented JSON.
"""

from __future__ import annotations

import requests

import ffsft.azure_ml
from ffsft.deploy import lifecycle
from ffsft.deploy.lifecycle import (
    EXIT_COULD_NOT_LOOK,
    EXIT_NOT_IDLE,
    ORPHANS_SECTION,
    Inventory,
    ScanStatus,
    read_orphans,
)

RG = "/subscriptions/s/resourceGroups/rg-ffsft-kc/providers"
#: The replayed leak, as ARM returns it. Same rows as
#: tests/test_down_scans_the_resource_group_before_it_claims_the_meter_stopped.py
#: on purpose: the only thing this file changes about that resource group is
#: which PAGE the rows arrive on.
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

#: The three ARM providers `read_orphans` lists, in the order it lists them.
DISKS = "Microsoft.Compute/disks"
IPS = "Microsoft.Network/publicIPAddresses"
NICS = "Microsoft.Network/networkInterfaces"


class FakeTarget:
    subscription_id = "11111111-2222-3333-4444-555555555555"
    resource_group = "rg-ffsft-kc"
    workspace_name = "mlw-ffsft"
    location = "koreacentral"


class FakeResponse:
    """200 with an ARM-shaped body. `raise_for_status` returns, because the
    defect this file is about arrives with a perfectly successful HTTP call."""

    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def paged_arm(pages, *, log=None):
    """Stand in for `requests.get` against ARM, serving multi-page listings.

    `pages` maps a provider path to the list of PAGES for it, each page a list
    of resources. A `nextLink` is emitted for every page but the last, so a
    one-element list is the un-paginated listing every other test in the suite
    already serves and a two-element list is the defect.

    Patched on the `requests` module itself, never on `lifecycle`: the import in
    `read_orphans` is function-local, so `lifecycle.requests` is a name nobody
    reads and faking it fakes nothing.
    """
    bodies: dict[str, dict] = {}
    first: dict[str, str] = {}
    for path, page_list in pages.items():
        urls = [f"https://management.azure.com/{path}/page{n}" for n in range(len(page_list))]
        for n, (url, value) in enumerate(zip(urls, page_list, strict=True)):
            body: dict = {"value": list(value)}
            if n + 1 < len(urls):
                body["nextLink"] = urls[n + 1]
            bodies[url] = body
        first[path] = urls[0]

    def get(url, headers=None, timeout=None):
        if log is not None:
            log.append(url)
        if url in bodies:
            return FakeResponse(bodies[url])
        for path, page1 in first.items():
            # The first request is built by `read_orphans` itself
            # (`{base}/{path}?api-version=...`); every later one is a
            # `nextLink` this fake handed out.
            if path in url:
                return FakeResponse(bodies[page1])
        raise AssertionError(f"unexpected ARM url: {url}")

    return get


def empty_rg(**overrides):
    """A resource group ARM reports as holding nothing, one page per provider."""
    pages = {DISKS: [[]], IPS: [[]], NICS: [[]]}
    pages.update(overrides)
    return pages


class FakeEmpty:
    def list(self, *a, **kw):
        return []


class FakeMLClient:
    """An Azure ML workspace with nothing in it, so the resource-group scan is
    the only thing in the report that can say anything."""

    def __init__(self):
        self.online_endpoints = FakeEmpty()
        self.online_deployments = FakeEmpty()
        self.compute = FakeEmpty()
        self.jobs = FakeEmpty()
        self.batch_endpoints = FakeEmpty()


class OKCredential:
    def get_token(self, *scopes, **kw):
        return type("T", (), {"token": "t"})()


def scan_rg(monkeypatch, pages, *, log=None):
    """Run the REAL `read_orphans` over a faked ARM. Returns (rows, inventory)."""
    monkeypatch.setattr(requests, "get", paged_arm(pages, log=log))
    inv = Inventory()
    rows = read_orphans(FakeTarget(), credential=OKCredential(), inv=inv)
    return rows, inv


def run_down(monkeypatch, pages, argv=("--all", "--yes")):
    """Drive the real `ffsft-lifecycle down ...` with every Azure seam faked."""
    monkeypatch.setattr(lifecycle, "quiet_azure_sdk_logs", lambda *a, **k: None)

    class StubTarget:
        @staticmethod
        def from_env():
            return FakeTarget()

    monkeypatch.setattr(ffsft.azure_ml, "AzureTarget", StubTarget)
    monkeypatch.setattr(ffsft.azure_ml, "get_ml_client", lambda target: FakeMLClient())
    monkeypatch.setattr(requests, "get", paged_arm(pages))
    real = lifecycle.read_orphans
    monkeypatch.setattr(
        lifecycle,
        "read_orphans",
        lambda target, **kw: real(target, credential=OKCredential(), **kw),
    )
    monkeypatch.setattr("sys.argv", ["ffsft-lifecycle", "down", *argv])
    return lifecycle.main()


# --- the defect: page 2 was never requested ----------------------------------


def test_the_orphan_scan_requests_the_next_page_instead_of_stopping_at_the_first(monkeypatch):
    """The mechanical statement of the bug: with a `nextLink` on the table, the
    pre-fix `fetch` issued exactly three GETs and none of them was page 2."""
    seen: list[str] = []
    scan_rg(monkeypatch, empty_rg(**{DISKS: [[], [LEAKED_DISK]]}), log=seen)
    assert any(url.endswith("/page1") for url in seen), seen


def test_a_disk_arm_hands_back_on_page_two_is_not_reported_as_an_empty_resource_group(
    monkeypatch,
):
    """The money statement. Page 1 is empty and page 2 holds the leak, so a
    reader that stops at page 1 reports a clean resource group with a 200."""
    rows, inv = scan_rg(monkeypatch, empty_rg(**{DISKS: [[], [LEAKED_DISK]]}))
    assert [r.name for r in rows] == ["vm-a10-ffsft_OsDisk_1"]
    # The scan really did complete: the last page carried no `nextLink`, so this
    # is a measurement and the report is allowed to act on it.
    assert inv.failed_scans == []


def test_a_public_ip_and_a_nic_on_later_pages_are_read_too_not_only_the_disks(monkeypatch):
    """All three `fetch` calls, not just the one the reproduction used. The NIC
    is the load-bearing one: `_orphaned_nic_names` decides whether the IP counts
    as leaked, so a NIC stranded on page 2 turns a real orphan into a healthy
    IP -- an under-report produced by a listing that returned 200."""
    rows, inv = scan_rg(
        monkeypatch,
        {DISKS: [[]], IPS: [[], [LEAKED_IP]], NICS: [[], [DEAD_NIC]]},
    )
    assert [r.name for r in rows] == ["vm-a10-ffsftPublicIP"]
    assert inv.failed_scans == []


# --- a listing that stopped short is not a listing that was short ------------


def test_a_nextlink_that_cannot_be_followed_records_a_failed_scan_not_an_empty_one(monkeypatch):
    """`TruncatedListing` reaches `_section` and is recorded by the one
    convention this report has for "this listing did not happen". No new
    `ScanStatus`: `is_evidence` asks whether an absence of rows means anything,
    and a short read answers no exactly as a 403 does."""

    def truncated(url, headers=None, timeout=None):
        # Page 1 promises more and the link 404s -- the shape that arrives as a
        # 200 followed by a failure, which is why the first page's `[]` must not
        # survive as an answer.
        if url.endswith("/page1"):
            raise RuntimeError("(AuthorizationFailed) no Reader on the second page")
        return FakeResponse({"value": [], "nextLink": f"{url}/page1"})

    monkeypatch.setattr(requests, "get", truncated)
    inv = Inventory()
    rows = read_orphans(FakeTarget(), credential=OKCredential(), inv=inv)
    assert rows == []
    assert [s.section for s in inv.failed_scans] == [ORPHANS_SECTION]
    assert inv.failed_scans[0].status is ScanStatus.FAILED
    assert inv.failed_scans[0].is_evidence is False


def test_the_recorded_detail_names_the_truncation_so_it_is_not_read_as_a_403(monkeypatch):
    """`scan.detail` is what the report prints after the section name, and the
    exception TYPE is the part that tells the operator where to look:
    `TruncatedListing` says the identity was fine and the list was not
    finished, `HttpResponseError: 403` says the opposite."""
    monkeypatch.setattr(requests, "get", paged_arm({DISKS: [[]] * 60, IPS: [[]], NICS: [[]]}))
    inv = Inventory()
    read_orphans(FakeTarget(), credential=OKCredential(), inv=inv)
    detail = inv.failed_scans[0].detail
    assert detail.startswith("TruncatedListing:"), detail


# --- the money path: what the operator and the shell are told -----------------


def test_down_all_yes_over_a_truncated_resource_group_never_says_the_meter_stopped(
    monkeypatch, capsys
):
    """The executed line this file exists for. `meter stopped.` is the strongest
    sentence in the tool and it was printed over a listing that stopped short."""

    def truncated(url, headers=None, timeout=None):
        if url.endswith("/page1"):
            raise RuntimeError("(AuthorizationFailed) no Reader on the second page")
        return FakeResponse({"value": [], "nextLink": f"{url}/page1"})

    monkeypatch.setattr(lifecycle, "quiet_azure_sdk_logs", lambda *a, **k: None)

    class StubTarget:
        @staticmethod
        def from_env():
            return FakeTarget()

    monkeypatch.setattr(ffsft.azure_ml, "AzureTarget", StubTarget)
    monkeypatch.setattr(ffsft.azure_ml, "get_ml_client", lambda target: FakeMLClient())
    monkeypatch.setattr(requests, "get", truncated)
    real = lifecycle.read_orphans
    monkeypatch.setattr(
        lifecycle,
        "read_orphans",
        lambda target, **kw: real(target, credential=OKCredential(), **kw),
    )
    monkeypatch.setattr("sys.argv", ["ffsft-lifecycle", "down", "--all", "--yes"])
    code = lifecycle.main()
    out = capsys.readouterr().out
    # rc first: scripts read exit codes, not paragraphs, and `down --all --yes
    # && echo clean` is the caller this defect lied to.
    assert code == EXIT_COULD_NOT_LOOK, out
    assert "meter stopped." not in out, out
    assert "BILLING NOW: nothing. No always-on compute in this workspace." not in out, out
    assert "an unread resource group is not a clean one." in out, out


def test_down_all_yes_finds_the_page_two_disk_and_reports_it_as_not_idle(monkeypatch, capsys):
    """The other half of the A/B in the docstring: once every page is read, the
    same fake resource group reports the leak and the `az` command for it, and
    the exit code says NOT idle rather than could-not-look."""
    code = run_down(monkeypatch, empty_rg(**{DISKS: [[], [LEAKED_DISK]]}))
    out = capsys.readouterr().out
    assert code == EXIT_NOT_IDLE, out
    assert "vm-a10-ffsft_OsDisk_1" in out, out
    assert "meter stopped." not in out, out
    assert "az disk delete" in out, out


# --- the true negative this must not destroy ---------------------------------


def test_a_single_page_empty_resource_group_is_still_a_measurement_and_still_exits_zero(
    monkeypatch, capsys
):
    """`{"value": []}` with no `nextLink` is ARM saying the collection is empty.
    If that stops reading as a clean resource group, `down` can never again say
    the meter is off and every refusal above stops meaning anything."""
    code = run_down(monkeypatch, empty_rg())
    out = capsys.readouterr().out
    assert code == 0, out
    assert "meter stopped. `ffsft lifecycle status` to confirm." in out, out
    assert "COULD NOT LOOK" not in out, out
