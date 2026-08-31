"""An exported-but-empty `FFSFT_*` variable means unset, not ''.

`AzureTarget.from_env` used to read every value but the tenant with a bare
`os.environ.get(NAME, default)`, whose default fires only when the variable is
*absent*. An exported-but-empty one is present, so it won through as '':

    AzureTarget(subscription_id='sub-real', resource_group='', workspace_name='',
                location='', compute_name='', compute_sku='', ...)

That is not a hypothetical shape. lab0 §4 appends to `~/.ffsft-env` with an
unquoted heredoc, `export FFSFT_RESOURCE_GROUP=$FFSFT_RESOURCE_GROUP`, and the
"create one" branch above it never exports those variables -- so that branch
bakes `export FFSFT_RESOURCE_GROUP=` into the file the participant then sources
in every later shell. `ffsft-lifecycle status` there queries a workspace named
'', fails inside `collect_inventory`, and prints `BILLING NOW: nothing` while an
endpoint bills $4.959/hr in a region nobody is looking at -- the reading lab7's
opening warning exists to prevent, arriving through the environment rather than
through the wrong profile.

The doc half of that is fixed in lab0. This is the durable half: nothing in
Azure is named '', so a blank value can only be a shell accident, and the
documented default is the most useful thing to do with it.
"""

from __future__ import annotations

import pytest

from ffsft.azure_ml import AzureTarget

#: Every variable `from_env` reads. Cleared before each case so a developer
#: workstation that legitimately exports these cannot decide the result.
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


def clear_the_environment(monkeypatch):
    for name in FFSFT_VARS:
        monkeypatch.delenv(name, raising=False)


def test_an_exported_but_empty_resource_group_falls_back_to_the_documented_default(monkeypatch):
    clear_the_environment(monkeypatch)
    monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "sub")
    monkeypatch.setenv("FFSFT_RESOURCE_GROUP", "")

    assert AzureTarget.from_env().resource_group == "rg-ffsft-kc"


def test_a_whitespace_only_resource_group_falls_back_to_the_documented_default(monkeypatch):
    """A blank with a space in it is the same accident, one keystroke wider."""
    clear_the_environment(monkeypatch)
    monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "sub")
    monkeypatch.setenv("FFSFT_RESOURCE_GROUP", "   ")

    assert AzureTarget.from_env().resource_group == "rg-ffsft-kc"


def test_a_real_resource_group_still_wins_over_the_default(monkeypatch):
    clear_the_environment(monkeypatch)
    monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "sub")
    monkeypatch.setenv("FFSFT_RESOURCE_GROUP", "rg-participant")

    assert AzureTarget.from_env().resource_group == "rg-participant"


def test_an_exported_but_empty_workspace_falls_back_to_the_documented_default(monkeypatch):
    """The workspace is the expensive one: it is what `status` walks."""
    clear_the_environment(monkeypatch)
    monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "sub")
    monkeypatch.setenv("FFSFT_WORKSPACE", "")

    assert AzureTarget.from_env().workspace_name == "mlw-ffsft"


def test_a_real_workspace_still_wins_over_the_default(monkeypatch):
    clear_the_environment(monkeypatch)
    monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "sub")
    monkeypatch.setenv("FFSFT_WORKSPACE", "mlw-participant")

    assert AzureTarget.from_env().workspace_name == "mlw-participant"


def test_the_whole_blanked_lab0_profile_resolves_to_the_documented_defaults(monkeypatch):
    """Sourcing a `~/.ffsft-env` written by the broken branch blanks all six.

    Asserted together rather than one per test because the failure arrived
    together: a single `source` set every one of them to ''.
    """
    clear_the_environment(monkeypatch)
    monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "sub")
    for name in ("FFSFT_RESOURCE_GROUP", "FFSFT_WORKSPACE", "FFSFT_LOCATION",
                 "FFSFT_COMPUTE", "FFSFT_SKU", "FFSFT_VM_PRIORITY"):
        monkeypatch.setenv(name, "")

    target = AzureTarget.from_env()

    assert target.resource_group == "rg-ffsft-kc"
    assert target.workspace_name == "mlw-ffsft"
    assert target.location == "koreacentral"
    assert target.compute_name == "gpu-a100-lp"
    assert target.compute_sku == "Standard_NC24ads_A100_v4"
    # LowPriority must survive this path too: '' as a tier reaches AmlCompute as
    # a dedicated request, which the tenant N-series deny policy refuses with a
    # message about the SKU.
    assert target.vm_priority == "LowPriority"


def test_surrounding_whitespace_is_stripped_from_a_real_value(monkeypatch):
    """A pasted name with a trailing space 404s with the name looking correct."""
    clear_the_environment(monkeypatch)
    monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "sub")
    monkeypatch.setenv("FFSFT_WORKSPACE", "  mlw-participant  ")

    assert AzureTarget.from_env().workspace_name == "mlw-participant"


def test_an_exported_but_empty_subscription_id_still_raises(monkeypatch):
    """The one value with no default keeps refusing to guess.

    Blank-is-unset does not soften this: unset here means raise, because a
    subscription is account-specific and any default would be someone else's.
    """
    clear_the_environment(monkeypatch)
    monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "")

    with pytest.raises(RuntimeError, match="FFSFT_SUBSCRIPTION_ID"):
        AzureTarget.from_env()


def test_a_whitespace_only_subscription_id_also_raises(monkeypatch):
    """This one used to pass the guard: '   ' is truthy.

    It then reached Azure as a subscription id and failed somewhere further in,
    with an error naming the id rather than the profile that supplied it.
    """
    clear_the_environment(monkeypatch)
    monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "   ")

    with pytest.raises(RuntimeError, match="FFSFT_SUBSCRIPTION_ID"):
        AzureTarget.from_env()


def test_a_blank_ffsft_subscription_id_falls_through_to_the_azure_one(monkeypatch):
    """Blank means unset, so the generic variable is still allowed to answer."""
    clear_the_environment(monkeypatch)
    monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "")
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-from-azure-var")

    assert AzureTarget.from_env().subscription_id == "sub-from-azure-var"


def test_a_blank_tenant_id_still_falls_back_to_none_and_not_to_a_default(monkeypatch):
    """The asymmetry this change had to preserve.

    Absent is legitimate for the tenant -- `None` means "whatever the Azure CLI
    has selected", which is correct on a single-directory workstation. Giving it
    a default the way the resource group has one would pin every user to the
    authors' directory, which is the opposite of what the fallback is for.
    """
    clear_the_environment(monkeypatch)
    monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "sub")
    monkeypatch.setenv("FFSFT_TENANT_ID", "")

    assert AzureTarget.from_env().tenant_id is None


def test_a_real_tenant_id_still_wins_after_the_helper_took_over(monkeypatch):
    clear_the_environment(monkeypatch)
    monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "sub")
    monkeypatch.setenv("FFSFT_TENANT_ID", "00000000-0000-0000-0000-0000000000aa")

    assert AzureTarget.from_env().tenant_id == "00000000-0000-0000-0000-0000000000aa"
