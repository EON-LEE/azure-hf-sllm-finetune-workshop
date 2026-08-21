"""The AcrPull preflight could never fire on the deployment it was written for.

`read_identity_grants` returns None on HTTP 404, reasoning that an endpoint
which does not exist yet has no identity to check. That is true. The problem is
where it was called from: `deploy_online` ran the identity preflight *before*
creating the endpoint, so on a brand-new endpoint name the probe always 404'd,
always returned None, and never blocked.

A brand-new endpoint name is exactly the case where the grant is guaranteed to
be missing. Azure wires up AcrPull only for the workspace-linked registry, and
this workspace has none. So the check was structurally blind in the only
situation it existed to catch.

Measured, 2026-08-21: endpoint `ffsft-a10` created fresh, deployment `blue`
refused ~10 minutes later with

    (BadArgument) Endpoint identity does not have pull permission on the registry.

while the preflight logged nothing at all. Its identity held `AzureML Metrics
Writer` and `Storage Blob Data Reader` and no ACR role whatsoever.
"""

from __future__ import annotations

import pytest

from ffsft.deploy.identity import (
    ACR_PULL,
    IdentityGrants,
    ensure_acr_pull,
    identity_blocker,
)


def _grants(**kw) -> IdentityGrants:
    base = dict(
        endpoint_name="ffsft-a10",
        principal_id="fbd167d1-f592-470d-8d57-25ff85790033",
        acr_roles=[],
        storage_roles=["Storage Blob Data Reader"],
        acr_is_workspace_linked=False,
    )
    base.update(kw)
    return IdentityGrants(**base)


def test_the_measured_failure_is_a_blocker():
    """The exact grant set ffsft-a10 had when Azure refused the pull."""
    blocker = identity_blocker(_grants())
    assert blocker is not None
    assert ACR_PULL in blocker


def test_granting_acr_pull_clears_it():
    assert identity_blocker(_grants(acr_roles=[ACR_PULL])) is None


class FakeAuth:
    """Stands in for the ARM role-assignment API."""

    def __init__(self, existing=()):
        self.existing = list(existing)
        self.created = []

    def list_roles(self, scope, principal_id):
        return list(self.existing)

    def create_role(self, scope, principal_id, role):
        self.created.append((scope, principal_id, role))
        self.existing.append(role)


ACR_SCOPE = (
    "/subscriptions/s/resourceGroups/rg/providers"
    "/Microsoft.ContainerRegistry/registries/acrffsftkc"
)


def test_ensure_acr_pull_creates_the_missing_assignment():
    auth = FakeAuth()
    result = ensure_acr_pull(ACR_SCOPE, "principal-1", auth=auth)
    assert result.granted is True
    assert auth.created == [(ACR_SCOPE, "principal-1", ACR_PULL)]


def test_ensure_acr_pull_is_idempotent():
    """Re-deploying must not pile up duplicate role assignments."""
    auth = FakeAuth(existing=[ACR_PULL])
    result = ensure_acr_pull(ACR_SCOPE, "principal-1", auth=auth)
    assert result.granted is False
    assert result.already_had is True
    assert auth.created == []


def test_ensure_acr_pull_accepts_a_superset_role():
    auth = FakeAuth(existing=["Owner"])
    result = ensure_acr_pull(ACR_SCOPE, "principal-1", auth=auth)
    assert result.granted is False and auth.created == []


def test_ensure_acr_pull_reports_a_refusal_instead_of_raising():
    """A credential without RBAC write rights must produce advice, not a crash."""

    class Refusing(FakeAuth):
        def create_role(self, scope, principal_id, role):
            raise PermissionError("AuthorizationFailed")

    result = ensure_acr_pull(ACR_SCOPE, "principal-1", auth=Refusing())
    assert result.granted is False
    assert result.error is not None
    assert "az role assignment create" in (result.manual_fix or "")
    assert "principal-1" in (result.manual_fix or "")


def test_ensure_acr_pull_needs_a_principal():
    auth = FakeAuth()
    result = ensure_acr_pull(ACR_SCOPE, None, auth=auth)
    assert result.granted is False and auth.created == []


@pytest.mark.parametrize("scope", ["", None])
def test_ensure_acr_pull_needs_a_scope(scope):
    auth = FakeAuth()
    result = ensure_acr_pull(scope, "principal-1", auth=auth)
    assert result.granted is False and auth.created == []
