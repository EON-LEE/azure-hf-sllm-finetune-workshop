"""Pinning the tenant, because the subscription id does not pin it.

`FFSFT_SUBSCRIPTION_ID` names the subscription and says nothing about which
directory to authenticate against. The tenant comes from whatever the Azure CLI
happens to have selected, so on a workstation signed in to more than one
directory the two can drift apart -- and they did, twice in one session, in the
middle of a run:

    (InvalidAuthenticationTokenTenant) The access token is from the wrong issuer
    'https://sts.windows.net/<tenant-B>/'. It must match one of the tenants
    '...<tenant-A>/' associated with this subscription.

(Real GUIDs redacted; the shape is what matters. Tenant A is the directory
the subscription lives in, tenant B the one the CLI had selected.)

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

# An opaque fixture. These tests care that the value is carried through
# unchanged, never what it is, so a real directory id has no business here.
TENANT = "00000000-0000-0000-0000-0000000000aa"


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


class FakeChained:
    def __init__(self, *credentials):
        self.credentials = credentials


@pytest.fixture
def fake_identity(monkeypatch):
    """Replace all three credential classes at once.

    Returns the module so a test can assert on what was constructed without any
    of it touching Entra.
    """
    import ffsft.azure_ml as mod

    monkeypatch.setattr(mod, "_credential_class", lambda: FakeCredential)
    monkeypatch.setattr(mod, "_cli_credential_class", lambda: FakeCredential)
    monkeypatch.setattr(mod, "_chained_credential_class", lambda: FakeChained)
    return mod


def test_default_credential_never_receives_a_tenant_id(fake_identity):
    """The discovery that shaped this function.

    `DefaultAzureCredential(tenant_id=...)` is not merely ineffective, it raises:

        TypeError: 'tenant_id' is not supported in DefaultAzureCredential.

    It only accepts per-credential variants (`shared_cache_tenant_id` and
    friends), none of which reach `AzureCliCredential` -- which is precisely the
    credential doing the work on a developer workstation. So the tenant has to
    be pinned on the CLI credential directly.
    """
    chained = fake_identity.build_credential(AzureTarget("sub", "rg", "ws", tenant_id=TENANT))

    default = chained.credentials[-1]
    assert "tenant_id" not in default.kwargs


def test_credential_pins_the_tenant_on_the_cli_credential(fake_identity):
    chained = fake_identity.build_credential(AzureTarget("sub", "rg", "ws", tenant_id=TENANT))

    assert chained.credentials[0].kwargs["tenant_id"] == TENANT


def test_the_pinned_cli_credential_is_tried_first(fake_identity):
    """Order is the whole mechanism.

    Behind the CLI credential, `DefaultAzureCredential` would once again resolve
    the tenant from ambient state -- the exact behaviour being corrected.
    """
    chained = fake_identity.build_credential(AzureTarget("sub", "rg", "ws", tenant_id=TENANT))

    assert len(chained.credentials) == 2
    assert chained.credentials[0].kwargs.get("tenant_id") == TENANT


def test_the_default_credential_remains_as_a_fallback(fake_identity):
    """Pinning must not break authentication where there is no Azure CLI.

    The same code runs on a compute node, where the CLI does not exist and a
    managed identity does. Dropping `DefaultAzureCredential` to pin a tenant
    would fix the workstation by breaking the cluster.
    """
    chained = fake_identity.build_credential(AzureTarget("sub", "rg", "ws", tenant_id=TENANT))

    assert isinstance(chained.credentials[-1], FakeCredential)


def test_credential_allows_the_cli_to_authenticate_for_other_tenants(fake_identity):
    """Pinning must not lock out the cached login that does the real work.

    `AzureCliCredential` declines to serve a tenant other than the CLI's active
    one unless additionally allowed -- which would turn this fix into a
    different failure wearing the same symptom.
    """
    chained = fake_identity.build_credential(AzureTarget("sub", "rg", "ws", tenant_id=TENANT))

    assert chained.credentials[0].kwargs["additionally_allowed_tenants"] == ["*"]


def test_no_chain_is_built_when_no_tenant_is_known(fake_identity):
    """Unpinned callers keep exactly the behaviour they had.

    A plain `DefaultAzureCredential`, no chain, no extra failure modes -- the
    ambiguity being solved here does not exist on a single-directory machine.
    """
    credential = fake_identity.build_credential(AzureTarget("sub", "rg", "ws"))

    assert isinstance(credential, FakeCredential)
    assert credential.kwargs == {}


@pytest.mark.parametrize("tenant", [None, TENANT])
def test_get_ml_client_builds_its_client_from_the_same_credential(
    fake_identity, monkeypatch, tenant
):
    """One credential path, so a fix here reaches every caller.

    `get_ml_client` used to construct `DefaultAzureCredential()` inline, which
    is why pinning the tenant anywhere else would have had no effect on it.
    """
    seen = {}

    class FakeMLClient:
        def __init__(self, credential, subscription_id, resource_group_name, workspace_name):
            seen["credential"] = credential
            seen["subscription_id"] = subscription_id

    monkeypatch.setattr(fake_identity, "_ml_client_class", lambda: FakeMLClient)

    fake_identity.get_ml_client(AzureTarget("sub", "rg", "ws", tenant_id=tenant))

    assert seen["subscription_id"] == "sub"
    if tenant:
        assert seen["credential"].credentials[0].kwargs["tenant_id"] == tenant
    else:
        assert isinstance(seen["credential"], FakeCredential)
        assert seen["credential"].kwargs == {}


def test_build_credential_is_exported_for_direct_use():
    """Scripts that build their own ARM calls need the same credential.

    Several live checks in this project talk to ARM directly rather than through
    `MLClient`; if they construct their own credential they reintroduce the bug.
    """
    assert callable(build_credential)

