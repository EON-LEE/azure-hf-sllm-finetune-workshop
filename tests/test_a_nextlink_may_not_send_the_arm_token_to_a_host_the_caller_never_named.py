"""`nextLink` chose where the ARM bearer token went next, with no constraint.

`read_all_arm_pages` follows `body["nextLink"]` and re-sends the caller's
`headers` -- which carry `Authorization: Bearer <ARM token>` at all seven call
sites -- to whatever URL that field holds. Nothing compared it against the URL
the caller actually asked for. Executed against a fake `requests` serving
`"nextLink": "http://evil.example.invalid/steal?p=2"`:

    https://management.azure.com/...  Authorization='Bearer FAKE-ARM-TOKEN'
    http://evil.example.invalid/...   Authorization='Bearer FAKE-ARM-TOKEN'
    rows returned: [{'id': 'real-row'}, {'id': 'row-from-a-host-that-is-not-arm'}]

Both hops carried the token, the second over plaintext to a host that is not
ARM, and that host's rows came back as the rest of the listing.

FRAMED HONESTLY, because two independent auditors framed it this way and the
framing is the finding: real ARM emits neither a cross-host nor an `http://`
`nextLink`, so this is a HARDENING gap with no demonstrated live trigger. It
takes a compromised or MITM'd management endpoint, or a proxy rewriting response
bodies. It is not a breach anybody has measured. What makes it worth closing:

  * Round 8 newly routed the money path (`read_orphans`) AND the RBAC path
    (`ArmRoleAuth.list_roles`, whose caller goes on to PUT a role assignment)
    through this one helper. Exposure widened; the helper did not.
  * The SDK sitting in this repo's own `.venv` already refuses half of it.
    azure-core 1.41.0, `pipeline/policies/_authentication.py:83`:

        if enforce_https and not request.http_request.url.lower().startswith("https"):
            raise ServiceRequestError(
                "Bearer token authentication is not permitted for non-TLS
                 protected (non-https) URLs."

    That was parity debt against an installed library, not a guess.

The check compares scheme and netloc against the ORIGINAL url rather than an
allowlist of ARM hostnames, which is what keeps sovereign clouds working -- a
hardcoded `management.azure.com` would break `management.usgovcloudapi.net` and
`management.chinacloudapi.cn`, both pinned below. A mismatch raises
`TruncatedListing` because that is precisely the state it names: ARM said there
was more of the list and it could not be read.

Everything here is a FAKE `requests` module. The bearer token is the invented
string "Bearer FAKE-ARM-TOKEN"; no credential is constructed and no network call
is made.
"""

from __future__ import annotations

import pytest

from ffsft.deploy.preflight import TruncatedListing, read_all_arm_pages

ARM = "https://management.azure.com"
START = f"{ARM}/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/disks"
TOKEN = {"Authorization": "Bearer FAKE-ARM-TOKEN"}


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class FakeRequests:
    """Serves page 1 with whatever `nextLink` the test wants, and records every
    URL it was asked for together with the Authorization header it received."""

    def __init__(self, next_link, *, start=START):
        self.sent: list[tuple[str, str | None]] = []
        self._next_link = next_link
        self._start = start

    def get(self, url, headers=None, timeout=None, **kw):
        self.sent.append((url, (headers or {}).get("Authorization")))
        if url == self._start:
            return FakeResponse({"value": [{"id": "page-1-row"}], "nextLink": self._next_link})
        return FakeResponse({"value": [{"id": "row-from-wherever-this-is"}]})

    @property
    def hosts(self) -> list[str]:
        from urllib.parse import urlsplit

        return [urlsplit(u).netloc for u, _ in self.sent]


# --- the refusal --------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "http://evil.example.invalid/steal?p=2",
        "https://evil.example.invalid/steal?p=2",
        "http://management.azure.com/subscriptions/s/page2",
    ],
    ids=["another host over plaintext", "another host over TLS", "same host downgraded to http"],
)
def test_a_nextlink_off_the_host_the_caller_named_is_refused_rather_than_followed(hostile):
    """All three shapes are the same decision: the next page has to come from
    the endpoint the caller chose to trust, over the scheme it chose."""
    fake = FakeRequests(hostile)
    with pytest.raises(TruncatedListing):
        read_all_arm_pages(fake, START, headers=TOKEN, timeout=30)


def test_the_arm_token_is_never_sent_to_the_host_the_nextlink_named():
    """The statement that matters. The refusal has to happen BEFORE the GET, not
    after it -- raising on the response would already have sent the token."""
    fake = FakeRequests("http://evil.example.invalid/steal?p=2")
    with pytest.raises(TruncatedListing):
        read_all_arm_pages(fake, START, headers=TOKEN, timeout=30)
    assert fake.hosts == ["management.azure.com"], fake.sent
    assert all("evil.example.invalid" not in url for url, _ in fake.sent), fake.sent


def test_the_refusal_names_both_hosts_so_the_operator_can_see_which_is_wrong():
    """`scan.detail` and every caller's could-not-look note print this message.
    "could not read the listing" sends them to the scrollback; naming the host
    tells them whether they are looking at a proxy or at a sovereign-cloud
    endpoint they typed wrong."""
    fake = FakeRequests("https://evil.example.invalid/steal?p=2")
    with pytest.raises(TruncatedListing) as caught:
        read_all_arm_pages(fake, START, headers=TOKEN, timeout=30)
    message = str(caught.value)
    assert "evil.example.invalid" in message, message
    assert "management.azure.com" in message, message


def test_a_refused_hop_is_a_truncation_and_not_some_new_kind_of_failure():
    """It reaches callers as `TruncatedListing`, so it lands in the handlers
    round 6 and round 8 already built rather than in a parallel path. The
    listing genuinely is truncated: ARM said there was more and we will not
    read it from there."""
    fake = FakeRequests("https://evil.example.invalid/p2")
    with pytest.raises(TruncatedListing):
        read_all_arm_pages(fake, START, headers=TOKEN, timeout=30)


# --- what must keep working ---------------------------------------------------


def test_an_ordinary_same_host_nextlink_is_still_followed():
    """The true negative. This is every real ARM listing, and breaking it would
    re-open every defect rounds 6 through 8 paid for."""
    fake = FakeRequests(f"{ARM}/subscriptions/s/page2")
    rows = read_all_arm_pages(fake, START, headers=TOKEN, timeout=30)
    assert [r["id"] for r in rows] == ["page-1-row", "row-from-wherever-this-is"]


@pytest.mark.parametrize(
    "cloud",
    [
        "management.usgovcloudapi.net",
        "management.chinacloudapi.cn",
        "management.azure.eaglex.ic.gov",
    ],
)
def test_a_sovereign_cloud_pages_its_own_listings_exactly_as_the_public_one_does(cloud):
    """Why the check compares against the caller's own url instead of an
    allowlist of hostnames. A hardcoded `management.azure.com` would refuse
    every page 2 in these clouds -- and a refusal here is not a silent bug, it
    is an operator being told their whole resource group is unreadable."""
    start = f"https://{cloud}/subscriptions/s/resourceGroups/rg/providers/x"
    fake = FakeRequests(f"https://{cloud}/subscriptions/s/page2", start=start)
    rows = read_all_arm_pages(fake, start, headers=TOKEN, timeout=30)
    assert [r["id"] for r in rows] == ["page-1-row", "row-from-wherever-this-is"]
    assert fake.hosts == [cloud, cloud], fake.sent


def test_the_host_comparison_does_not_care_about_letter_case():
    """Hostnames are case-insensitive, and a `nextLink` that shouts is still the
    same endpoint. Refusing it would be a false truncation reported as fact --
    the failure mode every round of this project has been about."""
    fake = FakeRequests("https://MANAGEMENT.AZURE.COM/subscriptions/s/page2")
    rows = read_all_arm_pages(fake, START, headers=TOKEN, timeout=30)
    assert len(rows) == 2, rows
