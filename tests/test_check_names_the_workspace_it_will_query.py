"""`ffsft-deploy check` must name the workspace it is about to ask.

Lab 5 §2 sends participants to `check --probe` BEFORE they deploy, which makes
it the command whose scope a participant trusts. It printed one line:

    subscription <id> / koreacentral

-- the subscription, and then the location, which `get_ml_client` never sends.
It named neither the resource group nor the workspace, and those are what the
next eight lines actually query. A participant whose `~/.ffsft-env` had blanked
`FFSFT_WORKSPACE` (lab0 §4's unquoted heredoc, see
`test_env_target_treats_empty_as_unset.py`) therefore read an answer about
`rg-ffsft-kc` / `mlw-ffsft` -- the AUTHORS' resources -- as an answer about
theirs, with nothing on screen to say otherwise.

`status` grew that header in round 3. These tests pin the two properties that
make the fix a fix rather than a second copy: `check` prints the same identity
lines from the same helper, and the clause about the location tells the truth
about THIS report, where dedicated quota really is read per region.
"""

from __future__ import annotations

import argparse

import pytest

import ffsft.deploy.endpoint as endpoint
import ffsft.deploy.probes as probes
from ffsft.azure_ml import AzureTarget
from ffsft.deploy.preflight import AML_CLIENT_SCOPE, QUOTA_SCOPE, scope_lines
from ffsft.deploy.probes import StoreProbe

FFSFT_VARS = (
    "FFSFT_SUBSCRIPTION_ID",
    "AZURE_SUBSCRIPTION_ID",
    "FFSFT_TENANT_ID",
    "AZURE_TENANT_ID",
    "FFSFT_RESOURCE_GROUP",
    "FFSFT_WORKSPACE",
    "FFSFT_LOCATION",
    "FFSFT_COMPUTE",
    "FFSFT_SKU",
    "FFSFT_VM_PRIORITY",
)


@pytest.fixture
def check_output(monkeypatch, capsys):
    """Run `cmd_check` with the participant's profile and no Azure at all.

    Both fakes are set on `endpoint`, because `cmd_check` calls both as
    `endpoint` globals. That is the seam `tests/test_deploy_module_split.py`
    exists for: patching `probes.check_pattern` here would fake a name this
    caller does not read, and the real call would leave the machine.
    """

    def run(**env):
        for name in FFSFT_VARS:
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr(
            endpoint,
            "probe_model_store",
            lambda target: StoreProbe(
                account="stub", public_access="Enabled", private_endpoints=0,
                reachable=True, detail="",
            ),
        )
        monkeypatch.setattr(
            endpoint,
            "check_pattern",
            lambda key, sub, loc, **kw: (endpoint.get_serving_registry().get(key), None),
        )
        # `cmd_check` also calls `read_dedicated_quota` itself, for the patterns
        # that cannot use LowPriority. That call resolves on `endpoint`, so this
        # is the seam for it -- and the first draft of this test, which faked
        # only `check_pattern`, went out to management.azure.com for real and
        # came back with a 400.
        monkeypatch.setattr(endpoint, "read_dedicated_quota", lambda sub, loc, family: 0)

        def no_network(*a, **kw):
            raise AssertionError("the suite must not reach management.azure.com")

        # If the seam above ever moves back onto `probes`, this fails loudly
        # instead of dialling out.
        monkeypatch.setattr(probes, "read_dedicated_quota", no_network)
        capsys.readouterr()
        assert endpoint.cmd_check(argparse.Namespace(probe=False)) == 0
        return capsys.readouterr().out

    return run


def test_check_names_the_resource_group_and_workspace_it_is_about_to_query(check_output):
    out = check_output(
        FFSFT_SUBSCRIPTION_ID="sub-participant",
        FFSFT_RESOURCE_GROUP="rg-mine",
        FFSFT_WORKSPACE="mlw-mine",
    )

    assert "rg-mine" in out
    assert "mlw-mine" in out
    assert "sub-participant" in out


def test_check_no_longer_prints_the_location_as_a_bare_slash_separated_scope(check_output):
    # The exact old line. A slash between a subscription and a region reads as
    # "subscription, then region" -- a scope this command does not have.
    out = check_output(
        FFSFT_SUBSCRIPTION_ID="sub-participant",
        FFSFT_LOCATION="koreacentral",
    )

    assert "subscription sub-participant / koreacentral" not in out


def test_check_prints_the_same_identity_lines_status_prints(check_output, monkeypatch):
    for name in FFSFT_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "sub-participant")
    monkeypatch.setenv("FFSFT_RESOURCE_GROUP", "rg-mine")
    monkeypatch.setenv("FFSFT_WORKSPACE", "mlw-mine")
    target = AzureTarget.from_env()

    out = check_output(
        FFSFT_SUBSCRIPTION_ID="sub-participant",
        FFSFT_RESOURCE_GROUP="rg-mine",
        FFSFT_WORKSPACE="mlw-mine",
    )

    # The first two lines are the shared half: whatever `status` says about who
    # answered, `check` says the same way, from the same helper.
    identity = scope_lines(target, AML_CLIENT_SCOPE)[:2]
    assert "\n".join(identity) in out


def test_check_says_the_location_scopes_its_quota_rows_rather_than_calling_it_unsent(
    check_output,
):
    # The two reports differ in exactly this clause, and copying `status`'s
    # wording here would be a new false statement rather than a shared one:
    # `read_dedicated_quota` hits /locations/{location}/providers/Microsoft.Quota
    # and returns 0 on a 404, so a grant held in another region reads as no
    # quota at all -- which is the first thing to check, not the last.
    out = check_output(FFSFT_SUBSCRIPTION_ID="sub", FFSFT_LOCATION="japaneast")

    assert "japaneast" in out
    assert "is not sent" not in out
    assert "\n".join(f"           {ln}" for ln in QUOTA_SCOPE).format(
        loc="japaneast", rg="rg-ffsft-kc"
    ) in out


def test_a_report_with_no_target_says_so_instead_of_naming_a_workspace():
    # Unchanged by the move, and the reason the helper is not simply "print the
    # target": a hand-built report that cannot name its scope must say that.
    assert scope_lines(None, AML_CLIENT_SCOPE) == [
        "LOOKED IN: unrecorded -- this report cannot name the workspace it read."
    ]
