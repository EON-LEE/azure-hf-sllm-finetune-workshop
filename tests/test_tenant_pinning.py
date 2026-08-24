"""Pinning the tenant, because the subscription id does not pin it.

`FFSFT_SUBSCRIPTION_ID` names the subscription and says nothing about which
directory to authenticate against. The tenant comes from whatever the Azure CLI
happens to have selected, so on a workstation signed in to more than one
directory the two can drift apart -- and they did, twice in one session, in the
middle of a run:

    (InvalidAuthenticationTokenTenant) The access token is from the wrong issuer
    'https://sts.windows.net/2573db8c-.../'. It must match one of the tenants
    '...4510ec63-0634-4550-9f93-2dc7de6cecec/' associated with this subscription.

Nothing in the code changed between the call that worked and the call that
failed. The default subscription moved underneath it.

That error is worth being precise about, because it reads like three things it
is not: an expired login, a missing role assignment, or a bug in the caller. It
is none of them -- the credential is valid, it is simply for the wrong
directory. The remedy is to state the tenant instead of inferring it.

So `AzureTarget` carries an optional `tenant_id`, and the credential is built
with it when it is known. Optional rather than required: a single-tenant
workstation has nothing to disambiguate, and forcing everyone to set another
environment variable to fix someone else's ambiguity is a bad trade.
"""

from __future__ import annotations

import pytest

from ffsft.azure_ml import AzureTarget, build_credential

TENANT = "4510ec63-0634-4550-9f93-2dc7de6cecec"


# --------------------------------------------------------------------------
# AzureTarget.tenant_id
# --------------------------------------------------------------------------


def test_tenant_id_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "sub")
    monkeypatch.setenv("FFSFT_TENANT_ID", TENANT)

    assert AzureTarget.from_env().tenant_id == TENANT


def test_tenant_id_falls_back_to_the_standard_azure_variable(monkeypatch):
    """`AZURE_TENANT_ID` is what the Azure SDKs already read.

    Someone who has set it for another tool should not have to set ours too.
    """
    monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "sub")
    monkeypatch.delenv("FFSFT_TENANT_ID", raising=False)
    monkeypatch.setenv("AZURE_TENANT_ID", TENANT)

    assert AzureTarget.from_env().tenant_id == TENANT


def test_ffsft_tenant_id_wins_over_the_generic_one(monkeypatch):
    """The project-specific variable is the more deliberate statement."""
    monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "sub")
    monkeypatch.setenv("FFSFT_TENANT_ID", TENANT)
    monkeypatch.setenv("AZURE_TENANT_ID", "other-tenant")

    assert AzureTarget.from_env().tenant_id == TENANT


def test_tenant_id_is_none_when_nothing_says_otherwise(monkeypatch):
    """Unset must stay unset.

    Defaulting to any particular tenant would break every other subscription
    this asset could be pointed at, which is the opposite of the swappability
    the whole project is built around.
    """
    monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "sub")
    monkeypatch.delenv("FFSFT_TENANT_ID", raising=False)
    monkeypatch.delenv("AZURE_TENANT_ID", raising=False)

    assert AzureTarget.from_env().tenant_id is None


def test_a_blank_tenant_id_is_treated_as_unset(monkeypatch):
    """`export FFSFT_TENANT_ID=` is how a shell says "never mind"."""
    monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "sub")
    monkeypatch.setenv("FFSFT_TENANT_ID", "   ")
    monkeypatch.delenv("AZURE_TENANT_ID", raising=False)

    assert AzureTarget.from_env().tenant_id is None


# --------------------------------------------------------------------------
# build_credential
# --------------------------------------------------------------------------


class FakeCredential:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def test_credential_passes_the_tenant_when_one_is_known(monkeypatch):
    import ffsft.azure_ml as mod

    monkeypatch.setattr(mod, "_credential_class", lambda: FakeCredential)
    target = AzureTarget("sub", "rg", "ws", tenant_id=TENANT)

    assert build_credential(target).kwargs["tenant_id"] == TENANT


def test_credential_omits_the_tenant_when_none_is_known(monkeypatch):
    """`tenant_id=None` is not the same as not passing it.

    DefaultAzureCredential inspects which keyword arguments were supplied, so
    handing it an explicit None is a different request from staying quiet.
    """
    import ffsft.azure_ml as mod

    monkeypatch.setattr(mod, "_credential_class", lambda: FakeCredential)
    target = AzureTarget("sub", "rg", "ws")

    assert "tenant_id" not in build_credential(target).kwargs


def test_credential_allows_the_cli_to_authenticate_for_other_tenants(monkeypatch):
    """Pinning must not lock out the CLI credential that does the real work.

    `DefaultAzureCredential` refuses to use a cached CLI login for a tenant
    other than its default unless additionally allowed, which would turn this
    fix into a different failure with the same symptom.
    """
    import ffsft.azure_ml as mod

    monkeypatch.setattr(mod, "_credential_class", lambda: FakeCredential)
    target = AzureTarget("sub", "rg", "ws", tenant_id=TENANT)

    assert build_credential(target).kwargs["additionally_allowed_tenants"] == ["*"]


@pytest.mark.parametrize("tenant", [None, TENANT])
def test_get_ml_client_builds_its_client_from_the_same_credential(monkeypatch, tenant):
    """One credential path, so a fix here reaches every caller.

    `get_ml_client` used to construct `DefaultAzureCredential()` inline, which
    is why pinning the tenant anywhere else would have had no effect on it.
    """
    import ffsft.azure_ml as mod

    seen = {}

    class FakeMLClient:
        def __init__(self, credential, subscription_id, resource_group_name, workspace_name):
            seen["credential"] = credential
            seen["subscription_id"] = subscription_id

    monkeypatch.setattr(mod, "_credential_class", lambda: FakeCredential)
    monkeypatch.setattr(mod, "_ml_client_class", lambda: FakeMLClient)

    target = AzureTarget("sub", "rg", "ws", tenant_id=tenant)
    mod.get_ml_client(target)

    assert isinstance(seen["credential"], FakeCredential)
    assert seen["subscription_id"] == "sub"
    expected = {"tenant_id": tenant} if tenant else {}
    assert seen["credential"].kwargs.get("tenant_id") == expected.get("tenant_id")
