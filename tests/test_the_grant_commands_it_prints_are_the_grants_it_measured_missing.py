"""The blocker prescribed both role grants no matter which one was missing.

`identity_blocker` measures the two grants separately -- that separation is the
whole of §79, and `test_the_storage_half_is_judged_on_its_own_reading` pins that
a truncated registry listing must not produce a registry BULLET. The bullets
were per-scope. The `Fix it with:` footer under them was not. Executed against
the pre-fix module over hand-built `IdentityGrants` records (no Azure, no
network -- the dataclass is plain data):

    --- only AcrPull missing (storage measured present) ---
       FINDINGS : ['- AcrPull on the container registry (cannot pull the image)']
       PRESCRIBES: ['--role "AcrPull" --scope <acr-resource-id>',
                    '--role "Storage Blob Data Reader" --scope <storage-resource-id>']
    --- storage missing, registry listing NEVER READ ---
       FINDINGS : ['- Storage Blob Data Reader on the workspace storage ...']
       PRESCRIBES: ['--role "AcrPull" --scope <acr-resource-id>',
                    '--role "Storage Blob Data Reader" --scope <storage-resource-id>']

The third row is the one that matters. `acr_roles is None` means the
roleAssignments listing for the registry never completed, and `identity_blocker`
correctly refuses to call the grant missing -- then hands the operator a
copy-pasteable command granting it. "An unread field may not become a finding
either" (CLAUDE.md), and a prescribed `az role assignment create` is a finding
with an action stapled to it: the operator runs it, ARM grants a role that may
well have been there all along, and the record now says the tool found a gap it
never measured.

The first two rows are the milder half and still real. Telling an operator to
grant `Storage Blob Data Reader` on the workspace storage account when this
tool just measured that they already have it is asking for a data-plane grant
nobody needs -- in a repo whose own notes single out blob data-plane access as
the door worth keeping shut.

The bullets and the commands are now built from one decision, so they cannot
disagree: a role reaches the footer only by having reached the findings first.
"""

from __future__ import annotations

import pytest

from ffsft.deploy.identity import (
    ACR_PULL,
    STORAGE_READ,
    IdentityGrants,
    identity_blocker,
)

#: Obvious placeholders. No such principal, registry or storage account exists.
FAKE_PRINCIPAL = "00000000-0000-0000-0000-000000000000"
_RG = "/subscriptions/s/resourceGroups/rg-fake/providers"
FAKE_ACR = f"{_RG}/Microsoft.ContainerRegistry/registries/acrfake"
FAKE_SA = f"{_RG}/Microsoft.Storage/storageAccounts/safake"


def grants(**kw) -> IdentityGrants:
    kw.setdefault("principal_id", FAKE_PRINCIPAL)
    return IdentityGrants(endpoint_name="fake-endpoint", **kw)


def prescribed(blocker: str) -> set[str]:
    """The roles the message tells the operator to CREATE."""
    roles = set()
    for line in blocker.splitlines():
        if "--role" not in line:
            continue
        for role in (ACR_PULL, STORAGE_READ):
            if f'"{role}"' in line:
                roles.add(role)
    return roles


def flagged(blocker: str) -> set[str]:
    """The roles the message names as findings, i.e. the bullets."""
    roles = set()
    for line in blocker.splitlines():
        if not line.strip().startswith("- "):
            continue
        for role in (ACR_PULL, STORAGE_READ):
            if role in line:
                roles.add(role)
    return roles


# --- one decision, two renderings --------------------------------------------


def test_only_the_registry_grant_is_prescribed_when_only_it_was_measured_missing():
    blocker = identity_blocker(grants(acr_roles=[], storage_roles=[STORAGE_READ]))
    assert blocker is not None
    assert prescribed(blocker) == {ACR_PULL}, blocker


def test_only_the_storage_grant_is_prescribed_when_only_it_was_measured_missing():
    blocker = identity_blocker(grants(acr_roles=[ACR_PULL], storage_roles=[]))
    assert blocker is not None
    assert prescribed(blocker) == {STORAGE_READ}, blocker


def test_a_grant_whose_listing_was_never_read_is_not_prescribed_either():
    """The §79 sign-flip with an action attached. `acr_roles is None` is "the
    listing did not finish"; the bullets already refuse to call it missing, and
    the command that grants it is the same claim in an executable form."""
    blocker = identity_blocker(grants(acr_roles=None, storage_roles=[]))
    assert blocker is not None
    assert prescribed(blocker) == {STORAGE_READ}, blocker
    assert "<acr-resource-id>" not in blocker, blocker


def test_both_are_prescribed_when_both_were_measured_missing():
    """The non-regression. Nothing above may cost the case the footer was
    written for, where the endpoint has neither grant."""
    blocker = identity_blocker(grants(acr_roles=[], storage_roles=[]))
    assert prescribed(blocker) == {ACR_PULL, STORAGE_READ}, blocker


@pytest.mark.parametrize(
    "acr_roles,storage_roles",
    [
        ([], []),
        ([], [STORAGE_READ]),
        ([ACR_PULL], []),
        (None, []),
        ([], None),
        (["Owner"], []),
        (["Reader"], ["Storage Account Contributor"]),
    ],
)
def test_every_command_it_prints_has_a_bullet_above_it_naming_the_same_gap(
    acr_roles, storage_roles
):
    """The structural property, stated once over every shape of the record:
    the findings and the remedies are the same set. A role can only reach the
    footer by having reached the bullets, which is what makes the footer a
    consequence of a measurement rather than a fixed paragraph."""
    blocker = identity_blocker(grants(acr_roles=acr_roles, storage_roles=storage_roles))
    if blocker is None:
        return
    assert prescribed(blocker) == flagged(blocker), blocker


def test_a_workspace_linked_registry_is_never_prescribed_a_pull_grant():
    """Azure grants pull rights itself for the workspace-linked ACR, so
    `can_pull_image` is True without any role in the list. Prescribing a grant
    there is telling the operator to fix something Azure already did."""
    blocker = identity_blocker(
        grants(acr_roles=[], acr_is_workspace_linked=True, storage_roles=[])
    )
    assert prescribed(blocker) == {STORAGE_READ}, blocker


# --- what the command has to remain ------------------------------------------


def test_the_scope_is_filled_in_when_the_record_carries_the_resource_id():
    """`acr_scope` and `storage_scope` are carried "so the `az` command that
    closes the gap can be printed with the scope filled in" (the dataclass says
    so). A pasteable command is the point of this message: the failure it
    guards against costs an hour of GPU billing before it says anything."""
    blocker = identity_blocker(
        grants(acr_roles=[], storage_roles=[], acr_scope=FAKE_ACR, storage_scope=FAKE_SA)
    )
    assert f"--scope {FAKE_ACR}" in blocker, blocker
    assert f"--scope {FAKE_SA}" in blocker, blocker
    assert "<acr-resource-id>" not in blocker, blocker


def test_a_record_with_no_scope_still_prints_a_placeholder_rather_than_an_empty_flag():
    """Hand-built records carry `""`. `--scope ` with nothing after it is a
    command that fails in a confusing way; the placeholder fails in an obvious
    one."""
    blocker = identity_blocker(grants(acr_roles=[], storage_roles=[STORAGE_READ]))
    assert "--scope <acr-resource-id>" in blocker, blocker
    assert "--scope \n" not in blocker, blocker


def test_the_principal_id_still_rides_on_every_command_that_is_printed():
    for acr, sa in (([], [STORAGE_READ]), ([ACR_PULL], []), ([], [])):
        blocker = identity_blocker(grants(acr_roles=acr, storage_roles=sa))
        commands = [ln for ln in blocker.splitlines() if "az role assignment create" in ln]
        assert commands, blocker
        assert all(FAKE_PRINCIPAL in ln for ln in commands), blocker


def test_the_slow_failure_explanation_and_the_escape_hatch_are_untouched():
    """Everything this message says besides the commands is why it is worth
    reading at all, and none of it is per-role."""
    blocker = identity_blocker(grants(acr_roles=[], storage_roles=[STORAGE_READ]))
    lowered = blocker.lower()
    assert "internalservererror" in lowered
    assert "never starts" in lowered
    assert "few minutes to propagate" in lowered
    assert "force=true" in lowered
