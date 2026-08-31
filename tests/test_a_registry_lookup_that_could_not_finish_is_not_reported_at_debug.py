"""A registry listing that raised on page 2 was a DEBUG line reading "not there".

Round 7 gave `acr_id_for_image` three outcomes and three log lines, and made
the one that means "this guess may name the wrong resource group" a WARNING.
It made exactly ONE of the two blind outcomes a warning. Executed against the
pre-fix module with a fake `requests` (page 1 carries a `nextLink`, page 2
raises) and a fake credential returning the literal string `"fake-token"`:

    --- B: page 2 of the listing raises (mid-listing HTTP failure) ---
       DEBUG: could not resolve ACR acrffsft by name, assuming rg-fake:
              (AuthorizationFailed) fake 403 on page 2
    --- A: the page cap is hit (TruncatedListing from the paginator) ---
       WARNING: the registry listing for this subscription stopped short (...)

Both return the same constructed `assumed` id. Both established nothing about
where the registry lives. The only difference is which exception the paginator
happened to raise -- `TruncatedListing` for its own cap, the transport's own
error for a page that 403s -- and only the first was caught, because the
handler named one type. The second fell through to the outer
`except Exception` two levels down.

Two things are wrong with landing there, and the level is the smaller one:

* `log.debug` is off in every shipped entry point (`quiet_azure_sdk_logs`
  leaves the app loggers at their default WARNING), so nothing is printed at
  all. §80 is the same failure one file over: a `log.warning` -> `log.debug`
  on `deploy/endpoint.py`'s unread-grant note left the suite green and took
  the only thing an operator would ever see with it.
* The wording is a claim. "could not resolve ACR %s by name" is what you say
  after looking; a listing that stopped on page 2 did not look. That is the
  sentence this repo exists to stop printing, in the file that already carries
  the §79 fix for the sign-flipped version of it.

`assumed` is not falsy, so no swallow-guard shape fires here and none would
have. This asymmetry is only visible by reading the two branches against each
other, which is why it is pinned rather than left to the next sweep.

No network and no Azure: `requests.get` and the credential are both faked, so
the real `acr_id_for_image` runs over invented ARM-shaped JSON. The
subscription id and resource group below are obvious placeholders, not
resources that exist.
"""

from __future__ import annotations

import logging
import types

import pytest
import requests

from ffsft.deploy import identity
from ffsft.deploy.identity import acr_id_for_image

#: Placeholders. No such subscription, resource group or registry exists.
FAKE_SUB = "00000000-0000-0000-0000-000000000000"
FAKE_RG = "rg-fake-not-a-real-group"
IMAGE = "acrffsft.azurecr.io/ffsft-serve:1"
#: What the function builds when it cannot establish where the registry lives.
ASSUMED = (
    f"/subscriptions/{FAKE_SUB}/resourceGroups/{FAKE_RG}"
    f"/providers/Microsoft.ContainerRegistry/registries/acrffsft"
)
#: What ARM would return if the registry really did live somewhere else.
ELSEWHERE = (
    f"/subscriptions/{FAKE_SUB}/resourceGroups/rg-fake-shared"
    f"/providers/Microsoft.ContainerRegistry/registries/acrffsft"
)


class FakeCredential:
    """Returns the literal string "fake-token". Never reaches an identity
    provider -- `DefaultAzureCredential` in this environment returns a REAL
    token, so a test that fell through to it would be a live call."""

    def get_token(self, *scopes, **kw):
        return types.SimpleNamespace(token="fake-token")


class RefusingCredential:
    """The seam BEFORE the listing: the lookup never runs at all."""

    def get_token(self, *scopes, **kw):
        raise RuntimeError("(InvalidAuthenticationTokenTenant) fake, no such tenant")


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def _pages_then(rows_per_page, *, final):
    """A fake `requests.get` serving `rows_per_page`, then `final`.

    `final` is either a list of rows for the last page (a listing that
    COMPLETED) or an exception instance to raise instead (a listing that did
    not). Every page but the last carries a `nextLink`, which is the only thing
    that puts the paginator into a second request at all.
    """
    seen = {"n": 0}

    def get(url, headers=None, params=None, timeout=None):
        n = seen["n"]
        seen["n"] += 1
        if n < len(rows_per_page):
            return FakeResponse(
                {"value": rows_per_page[n], "nextLink": f"https://management.azure.com/p{n + 1}"}
            )
        if isinstance(final, BaseException):
            raise final
        return FakeResponse({"value": final})

    return get


def _never_ends(url, headers=None, params=None, timeout=None):
    """Every page promises another one, so the paginator's own cap fires and
    it raises `TruncatedListing` -- the branch round 7 made a warning."""
    return FakeResponse({"value": [], "nextLink": url + "p"})


def _capture(monkeypatch, get, *, credential=None):
    """Run the REAL `acr_id_for_image` and return (result, [(level, msg), ...])."""
    monkeypatch.setattr(requests, "get", get)
    records: list[tuple[str, str]] = []

    class Collector(logging.Handler):
        def emit(self, record):
            records.append((record.levelname, record.getMessage()))

    log = identity.log
    monkeypatch.setattr(log, "handlers", [Collector()])
    monkeypatch.setattr(log, "propagate", False)
    monkeypatch.setattr(log, "level", logging.DEBUG)
    result = acr_id_for_image(
        IMAGE, FAKE_SUB, FAKE_RG, credential=credential or FakeCredential()
    )
    return result, records


def _levels(records):
    return {level for level, _ in records}


# --- the asymmetry ------------------------------------------------------------


@pytest.mark.parametrize(
    "how,make_get",
    [
        (
            "the paginator hit its own page cap",
            lambda: _never_ends,
        ),
        (
            "page two answered 403",
            lambda: _pages_then(
                [[]], final=RuntimeError("(AuthorizationFailed) fake 403 on page 2")
            ),
        ),
        (
            "page two never arrived",
            lambda: _pages_then([[]], final=OSError("fake connection reset")),
        ),
        (
            "page one itself failed",
            lambda: _pages_then([], final=RuntimeError("(AuthorizationFailed) fake 403")),
        ),
    ],
)
def test_a_registry_listing_that_did_not_finish_is_a_warning_however_it_failed(
    monkeypatch, how, make_get
):
    """The statement of the defect. Four ways for the listing not to finish,
    one epistemic state -- `assumed` is a guess and nobody established where
    the registry lives -- so one level. Pre-fix, only the first was a WARNING."""
    result, records = _capture(monkeypatch, make_get())
    assert result == ASSUMED, (how, result)
    assert "WARNING" in _levels(records), (how, records)


def test_the_line_names_the_exception_type_so_a_403_is_not_read_as_a_truncation(monkeypatch):
    """One level does not mean one story. `SectionScan.detail` in
    `lifecycle.py` carries `type(exc).__name__` for exactly this reason: a
    truncation says the identity was fine and the list was not finished, a 403
    says the opposite, and the operator's next move differs."""
    _, records = _capture(
        monkeypatch,
        _pages_then([[]], final=PermissionError("(AuthorizationFailed) fake 403 on page 2")),
    )
    warnings = [msg for level, msg in records if level == "WARNING"]
    assert warnings, records
    assert "PermissionError" in warnings[0], warnings


def test_the_page_cap_is_still_reported_and_still_names_the_truncation(monkeypatch):
    """The branch round 7 got right must not regress into the wider one."""
    _, records = _capture(monkeypatch, _never_ends)
    warnings = [msg for level, msg in records if level == "WARNING"]
    assert warnings, records
    assert "TruncatedListing" in warnings[0], warnings


def test_a_failed_listing_is_never_reported_as_the_registry_not_being_there(monkeypatch):
    """The wording half, which is the part that survives a level change.

    `ACR %s is not in subscription %s` is a measured negative -- every page was
    read and it is genuinely absent. It may not be printed over a listing that
    stopped, and neither may `could not resolve ACR %s by name`, which reads as
    a resolution that ran.
    """
    _, records = _capture(
        monkeypatch, _pages_then([[]], final=RuntimeError("(AuthorizationFailed) fake 403"))
    )
    joined = " | ".join(msg for _, msg in records)
    assert "is not in subscription" not in joined, joined
    assert "could not resolve ACR" not in joined, joined


def test_the_operator_is_told_which_resource_group_the_guess_names(monkeypatch):
    """A warning that does not name the guess is a warning nobody can act on:
    the whole failure this replaces was ARM answering 404 because the
    constructed id named the wrong resource group (§78.4)."""
    _, records = _capture(monkeypatch, _never_ends)
    warnings = [msg for level, msg in records if level == "WARNING"]
    assert FAKE_RG in warnings[0], warnings
    assert "acrffsft" in warnings[0], warnings


# --- the over-correction this must not become --------------------------------


def test_a_lookup_that_never_started_is_still_the_quiet_documented_fallback(monkeypatch):
    """The seam that has to stay where it is. A credential that refuses is the
    case the fallback was designed around -- the lookup did not run, and on
    both call paths (`deploy_online`, `submit`) the very next Azure call fails
    loudly with the same credential. Turning THAT into a warning would put a
    second line under every real auth failure and teach the operator to skip
    the one above."""
    result, records = _capture(monkeypatch, _never_ends, credential=RefusingCredential())
    assert result == ASSUMED
    assert "WARNING" not in _levels(records), records
    assert records, "a lookup that never ran must still leave a record"


def test_a_complete_listing_that_finds_the_registry_returns_the_real_id_and_stays_quiet(
    monkeypatch,
):
    """The success path. A listing that completed and found the registry
    somewhere else is a measurement, and it is the reason this lookup exists at
    all -- it must not now come with a warning attached."""
    rows = [{"name": "acrffsft", "id": ELSEWHERE}]
    result, records = _capture(monkeypatch, _pages_then([[]], final=rows))
    assert result == ELSEWHERE
    assert "WARNING" not in _levels(records), records


def test_a_complete_listing_that_does_not_hold_the_registry_is_an_info_not_a_warning(
    monkeypatch,
):
    """Scanned every page and it is genuinely not there. That is a measured
    negative and it keeps its own level: if a warning fires here too, the
    warning stops meaning "nobody looked"."""
    result, records = _capture(monkeypatch, _pages_then([[]], final=[]))
    assert result == ASSUMED
    assert "WARNING" not in _levels(records), records
    assert "INFO" in _levels(records), records


def test_a_failed_listing_still_returns_an_id_so_it_never_blocks_a_deployment(monkeypatch):
    """The constraint the outer handler was written for, restated as a test so
    raising the volume cannot quietly turn into raising an exception. Both
    callers (`deploy/endpoint.py`, `azure_ml.py`) pass the result straight into
    a role-assignment grant; a lookup that raises would take the deploy down."""
    result, _ = _capture(
        monkeypatch, _pages_then([[]], final=RuntimeError("(AuthorizationFailed) fake 403"))
    )
    assert result == ASSUMED


def test_an_image_that_is_not_in_an_acr_at_all_still_short_circuits_before_any_read(monkeypatch):
    """`mcr.microsoft.com/...` returns "" without a credential, a token or a
    GET. Pinned here because this file makes the failure path louder, and a
    warning over every non-ACR image would be noise on the busiest path."""

    def explode(*a, **kw):
        raise AssertionError("no ARM read may happen for a non-ACR image")

    monkeypatch.setattr(requests, "get", explode)
    assert acr_id_for_image("mcr.microsoft.com/azureml/base:1", FAKE_SUB, FAKE_RG) == ""
