"""A new cluster gets a new identity and inherits nothing.

`ensure_compute` created the cluster with a system-assigned identity and then
described, in a comment, the two data-plane roles that identity still needed:

    # The identity still needs data-plane roles
    # (Storage Blob Data Contributor, and AcrPull for a custom image).

It made neither. On 2026-08-26 a cluster `gpu-a100-ded` was created by hand to
escape LowPriority preemption. The storage role was granted -- deliberately,
remembering the datastore conversion -- and the registry role was not. The first
job died in 75 seconds:

    Failed to pull Docker image `acrffsftkc.azurecr.io/ffsft-train:12` due to:
    DockerResponseServerError { status_code: 401,
      message: "error from registry: authentication required" }

The role assignments on the registry said it plainly: the working cluster's
principal was in the AcrPull list and the new one was not.

So the fix is not "remember both". It is that whatever creates the identity
makes the grants, because the count of things to remember is exactly the thing
that was got wrong.
"""

from __future__ import annotations

import pytest

from ffsft.deploy.identity import (
    ACR_PULL,
    STORAGE_READ,
    STORAGE_WRITE,
    ArmRoleAuth,
    GrantResult,
    compute_role_grants,
    ensure_role,
)


class FakeAuth:
    """Records writes instead of making them."""

    def __init__(self, existing=()):
        self.existing = list(existing)
        self.created: list[tuple[str, str, str]] = []

    def list_roles(self, scope, principal_id):
        return list(self.existing)

    def create_role(self, scope, principal_id, role):
        self.created.append((scope, principal_id, role))


# --- what a training cluster needs ------------------------------------------


def test_a_training_cluster_needs_both_scopes():
    got = compute_role_grants(storage_id="/sub/s/sa", acr_id="/sub/s/acr")
    assert got == [("/sub/s/sa", STORAGE_WRITE), ("/sub/s/acr", ACR_PULL)]


def test_the_storage_role_is_the_write_role_not_the_serving_one():
    """A cluster uploads. Reader is what an endpoint needs and it is not enough.

    A cluster holding only Reader does not fail at start -- it runs, then
    finishes with `artifacts: 0` and nothing to register.
    """
    roles = [role for _, role in compute_role_grants(storage_id="/sa", acr_id=None)]
    assert STORAGE_WRITE in roles
    assert STORAGE_READ not in roles


def test_an_unresolved_scope_is_dropped_not_guessed():
    """A guessed ARM id 404s, and a 404 is reported as a permissions problem.

    `acr_id_for_image` returns "" when the image is not in an ACR at all. Turning
    that into a constructed id would send `ensure_role` at a resource that does
    not exist and produce a warning about role-assignment reads that is entirely
    misleading.
    """
    assert compute_role_grants(storage_id=None, acr_id=None) == []
    assert compute_role_grants(storage_id="/sa", acr_id="") == [("/sa", STORAGE_WRITE)]


# --- the grant itself --------------------------------------------------------


def test_the_requested_role_is_the_role_written():
    """`create_role` took a `role` argument and hardcoded the AcrPull GUID.

    A caller asking for the storage role would have been given a registry role
    and told it succeeded -- the single most expensive shape of bug available
    here, because the verification step passes.
    """
    auth = FakeAuth()
    ensure_role("/subscriptions/s/rg/x", "pid", STORAGE_WRITE, auth=auth)
    assert auth.created == [("/subscriptions/s/rg/x", "pid", STORAGE_WRITE)]


def test_an_unknown_role_is_refused_rather_than_written_as_acrpull():
    with pytest.raises(ValueError, match="no role definition GUID"):
        ArmRoleAuth().create_role("/subscriptions/s/rg/x", "pid", "Made Up Role")


def test_a_role_already_held_is_not_re_granted():
    auth = FakeAuth(existing=[STORAGE_WRITE])
    got = ensure_role("/sa", "pid", STORAGE_WRITE, auth=auth)
    assert got.already_had and not got.granted
    assert auth.created == []


def test_contributor_does_not_substitute_for_the_storage_data_role():
    """The subtlety that makes `allowSharedKeyAccess=false` bite.

    `Contributor` on a storage account grants no blob data-plane access. What it
    grants is the right to read the account keys -- the exact door that setting
    closes. Accepting it here would make this check pass on the configuration it
    exists to catch.
    """
    auth = FakeAuth(existing=["Contributor"])
    got = ensure_role("/sa", "pid", STORAGE_WRITE, auth=auth)
    assert got.granted
    assert auth.created == [("/sa", "pid", STORAGE_WRITE)]


def test_contributor_still_counts_for_the_registry():
    """It does imply pull rights, so re-granting there would be noise."""
    auth = FakeAuth(existing=["Contributor"])
    assert ensure_role("/acr", "pid", ACR_PULL, auth=auth).already_had
    assert auth.created == []


def test_an_unreadable_role_list_hands_back_the_command_for_that_role():
    """Not for AcrPull, which is what `_manual_fix` used to print regardless."""

    class Broken(FakeAuth):
        def list_roles(self, scope, principal_id):
            raise RuntimeError("403")

    got = ensure_role("/sa", "pid", STORAGE_WRITE, auth=Broken())
    assert got.error and STORAGE_WRITE in got.manual_fix
    assert ACR_PULL not in got.manual_fix


def test_a_failed_grant_is_reported_not_raised():
    """A cluster must still come into existence when the grant cannot be made."""

    class Refuses(FakeAuth):
        def create_role(self, scope, principal_id, role):
            raise RuntimeError("AuthorizationFailed")

    got = ensure_role("/sa", "pid", STORAGE_WRITE, auth=Refuses())
    assert isinstance(got, GrantResult)
    assert got.error and not got.granted
    assert "--role \"Storage Blob Data Contributor\"" in got.manual_fix


def test_nothing_to_grant_to_is_not_an_error_about_the_scope():
    assert "principal" in (ensure_role("/sa", "", STORAGE_WRITE).error or "")
