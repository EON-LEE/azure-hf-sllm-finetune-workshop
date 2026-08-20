"""A deployment that cannot possibly work should fail in seconds, not 90 minutes.

Two managed online endpoints were created in this workspace and neither ever
reached `Succeeded`. Both sat in `Creating` for over an hour and were deleted
without ever producing a container log, because Azure withholds deployment logs
until the deployment reaches a terminal state -- so the slow failure hid its own
cause, twice, at $2.16/hr each time.

The cause was not the model, the image, or the probes. It was the workspace's
default storage account:

    publicNetworkAccess        Disabled
    networkAcls.ipRules        []
    networkAcls.vnetRules      []
    privateEndpointConnections []
    workspace managedNetwork   isolationMode: Disabled

There is no public path and no private path. An Azure ML managed online
deployment stages its artifacts through that account, so the rollout retries
until it times out. The same root cause already blocked code-snapshot upload for
training jobs (docs/VERIFIED.md 2.2); it was not recognised as the same problem
because the two symptoms look nothing alike.

It is also not fixable in place: `az storage account update
--public-network-access Enabled` returns 0 and the value stays `Disabled`,
which is the signature of a policy `modify` effect reverting it.

All of that is knowable from two ARM reads before anything is created. This
module tests the check that turns a 90-minute silent hang into an immediate,
actionable message.
"""

from __future__ import annotations

import pytest

from ffsft.deploy.preflight import StorageReachability, storage_blocker


def make(
    *,
    public_access="Enabled",
    ip_rules=(),
    vnet_rules=(),
    private_endpoints=(),
    isolation_mode="Disabled",
) -> StorageReachability:
    return StorageReachability(
        account_name="mlwffsftstorage8cb451dd1",
        public_network_access=public_access,
        ip_rules=list(ip_rules),
        vnet_rules=list(vnet_rules),
        private_endpoints=list(private_endpoints),
        workspace_isolation_mode=isolation_mode,
    )


# --- the configuration that actually burned two hours ----------------------


def test_the_observed_configuration_is_rejected():
    """Public access off, no private endpoint, workspace not isolated."""
    blocker = storage_blocker(make(public_access="Disabled"))
    assert blocker is not None


def test_the_message_names_the_account_and_the_setting():
    """A blocker nobody can act on is only a faster failure, not a better one."""
    blocker = storage_blocker(make(public_access="Disabled"))
    assert "mlwffsftstorage8cb451dd1" in blocker
    assert "publicNetworkAccess" in blocker


def test_the_message_says_what_would_fix_it():
    """The two real options are a private endpoint or re-enabling public access."""
    blocker = storage_blocker(make(public_access="Disabled"))
    assert "private endpoint" in blocker.lower()


# --- configurations that are genuinely fine --------------------------------


def test_public_access_enabled_is_reachable():
    assert storage_blocker(make(public_access="Enabled")) is None


def test_a_private_endpoint_plus_an_isolated_workspace_is_reachable():
    """The supported way to run with public access off."""
    got = storage_blocker(
        make(
            public_access="Disabled",
            private_endpoints=["pe-storage-blob"],
            isolation_mode="AllowInternetOutbound",
        )
    )
    assert got is None


def test_case_is_not_load_bearing():
    """ARM has returned both 'Enabled' and 'enabled' across api-versions."""
    assert storage_blocker(make(public_access="enabled")) is None


# --- the near-misses, which are the whole reason this is a function --------


def test_a_private_endpoint_without_an_isolated_workspace_is_still_blocked():
    """Managed compute outside the VNet cannot use the private endpoint.

    This is the trap: the account looks 'fixed' because a private endpoint
    exists, but nothing that runs the deployment is on that network.
    """
    got = storage_blocker(
        make(
            public_access="Disabled",
            private_endpoints=["pe-storage-blob"],
            isolation_mode="Disabled",
        )
    )
    assert got is not None
    assert "isolation" in got.lower()


def test_an_isolated_workspace_without_a_private_endpoint_is_still_blocked():
    got = storage_blocker(
        make(public_access="Disabled", isolation_mode="AllowInternetOutbound")
    )
    assert got is not None
    assert "private endpoint" in got.lower()


def test_ip_rules_do_not_help_when_public_access_is_off():
    """`publicNetworkAccess: Disabled` overrides networkAcls entirely.

    The observed account had defaultAction 'Allow' and still refused every
    connection, which is exactly why reading networkAcls alone is misleading.
    """
    got = storage_blocker(make(public_access="Disabled", ip_rules=["203.0.113.7"]))
    assert got is not None


def test_unknown_state_does_not_block():
    """A read that failed must not be reported as a misconfiguration.

    Guessing 'blocked' from missing data would make the preflight itself the
    thing that stops legitimate deployments.
    """
    assert storage_blocker(make(public_access=None)) is None


@pytest.mark.parametrize("mode", ["AllowInternetOutbound", "AllowOnlyApprovedOutbound"])
def test_both_managed_vnet_modes_count_as_isolated(mode):
    got = storage_blocker(
        make(
            public_access="Disabled",
            private_endpoints=["pe-storage-blob"],
            isolation_mode=mode,
        )
    )
    assert got is None
