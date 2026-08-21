"""Pin the real cause of the two failed deployments: endpoint identity permissions.

Measured on this subscription, on a freshly created endpoint shell:

    endpoint MI on acrffsftkc              -> NONE
    endpoint MI on mlwffsftstorage8cb451dd1 -> NONE
    workspace properties.containerRegistry  -> "" (empty: no linked ACR)

while the *workspace* identity had `AcrPull` and `Storage Blob Data Contributor`
all along. That difference is the entire bug, and it is invisible if you only
check the workspace -- which is what I did for two endpoints and roughly four
hours of A10 billing.

These tests exist so that mistake cannot be repeated silently.
"""

from __future__ import annotations

import pytest

from ffsft.deploy.identity import (
    ACR_PULL,
    STORAGE_READ,
    IdentityGrants,
    identity_blocker,
)


def grants(**kw) -> IdentityGrants:
    base = {
        "endpoint_name": "ffsft-smoke2",
        "principal_id": "87ec28b5-bcc6-43f7-abd8-617abdfc13e6",
        "acr_roles": [ACR_PULL],
        "storage_roles": [STORAGE_READ],
    }
    base.update(kw)
    return IdentityGrants(**base)


# --------------------------------------------------------------------------
# The exact configuration that failed
# --------------------------------------------------------------------------


def test_the_real_failure_is_refused():
    """No roles at all on either resource -- the measured state of both endpoints."""
    blocker = identity_blocker(grants(acr_roles=[], storage_roles=[]))
    assert blocker is not None
    assert ACR_PULL in blocker
    assert STORAGE_READ in blocker


def test_missing_acr_pull_alone_is_refused():
    """This alone is fatal: no image means the container never starts."""
    assert identity_blocker(grants(acr_roles=[])) is not None


def test_missing_storage_read_alone_is_refused():
    assert identity_blocker(grants(storage_roles=[])) is not None


def test_a_correctly_permissioned_endpoint_is_allowed():
    assert identity_blocker(grants()) is None


# --------------------------------------------------------------------------
# The distinction that was actually missed
# --------------------------------------------------------------------------


def test_workspace_linked_registry_needs_no_explicit_pull_role():
    """Azure grants pull rights itself for the workspace's own ACR.

    Blocking here would make the check useless for the common setup, where the
    role genuinely is absent and deployment genuinely works.
    """
    assert identity_blocker(grants(acr_roles=[], acr_is_workspace_linked=True)) is None


def test_a_customer_registry_does_need_the_explicit_role():
    """The measured case: workspace has no linked ACR, so nothing is automatic."""
    assert (
        identity_blocker(grants(acr_roles=[], acr_is_workspace_linked=False)) is not None
    )


def test_unknown_identity_never_blocks():
    """A check that cannot see the facts must not veto a working deployment."""
    assert identity_blocker(grants(principal_id=None, acr_roles=[], storage_roles=[])) is None


# --------------------------------------------------------------------------
# Roles that also satisfy the requirement
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["AcrPull", "Owner", "Contributor"])
def test_roles_that_confer_pull_are_accepted(role):
    assert identity_blocker(grants(acr_roles=[role])) is None


@pytest.mark.parametrize(
    "role",
    [
        "Storage Blob Data Reader",
        "Storage Blob Data Contributor",
        "Storage Blob Data Owner",
        "Owner",
    ],
)
def test_roles_that_confer_blob_read_are_accepted(role):
    assert identity_blocker(grants(storage_roles=[role])) is None


def test_a_control_plane_role_is_not_data_plane_access():
    """`Storage Account Contributor` can manage the account but not read blobs.

    This is the trap one level down from the original bug: the role list looks
    reassuringly full while the endpoint still cannot read a single byte.
    """
    assert identity_blocker(grants(storage_roles=["Storage Account Contributor"])) is not None


def test_acr_reader_is_not_enough_to_pull():
    """`Reader` sees the registry resource; it does not grant docker pull."""
    assert identity_blocker(grants(acr_roles=["Reader"])) is not None


# --------------------------------------------------------------------------
# The message has to be actionable -- an hour of GPU time rides on it
# --------------------------------------------------------------------------


def test_message_contains_a_runnable_fix():
    blocker = identity_blocker(grants(acr_roles=[], storage_roles=[]))
    assert "az role assignment create" in blocker
    assert "87ec28b5-bcc6-43f7-abd8-617abdfc13e6" in blocker


def test_message_explains_the_silent_failure_mode():
    """Whoever hits this next must not repeat the storage misdiagnosis."""
    blocker = identity_blocker(grants(acr_roles=[]))
    lowered = blocker.lower()
    assert "internalservererror" in lowered
    assert "never starts" in lowered


def test_message_warns_about_propagation_delay():
    assert "propagate" in identity_blocker(grants(acr_roles=[])).lower()


def test_message_names_the_endpoint():
    assert "ffsft-smoke2" in identity_blocker(grants(acr_roles=[]))
