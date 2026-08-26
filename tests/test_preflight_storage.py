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

from ffsft.deploy.preflight import (
    StorageReachability,
    _network_blocker,
    storage_blocker,
)


def make(
    *,
    public_access="Enabled",
    ip_rules=(),
    vnet_rules=(),
    private_endpoints=(),
    isolation_mode="Disabled",
    allow_shared_key=None,
    key_based_datastores=(),
) -> StorageReachability:
    return StorageReachability(
        account_name="mlwffsftstorage8cb451dd1",
        public_network_access=public_access,
        ip_rules=list(ip_rules),
        vnet_rules=list(vnet_rules),
        private_endpoints=list(private_endpoints),
        workspace_isolation_mode=isolation_mode,
        allow_shared_key=allow_shared_key,
        key_based_datastores=list(key_based_datastores),
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


# --------------------------------------------------------------------------
# Correction. The tests above encoded a diagnosis that was wrong.
#
# `networkAcls.bypass: AzureServices` lets trusted Microsoft services through
# regardless of `publicNetworkAccess`, and Azure ML is a trusted service:
#
#   "access to a storage account from trusted services takes the highest
#    precedence over other network access restrictions ... exceptions that you
#    previously configured ... will remain in effect."
#   -- storage/common/storage-network-security-limitations
#
# The account that supposedly caused two failed deployments had that bypass set
# the whole time. Left uncorrected, this preflight refuses every deployment on
# this subscription for a reason that is not real.
# --------------------------------------------------------------------------


def test_trusted_service_bypass_is_not_a_blocker():
    """The measured live configuration. It must deploy."""
    state = StorageReachability(
        account_name="mlwffsftstorage8cb451dd1",
        public_network_access="Disabled",
        bypass="AzureServices",
    )
    assert storage_blocker(state) is None


def test_bypass_none_with_public_access_off_is_still_a_blocker():
    """Without the bypass the original reasoning does hold."""
    state = StorageReachability(
        account_name="sa",
        public_network_access="Disabled",
        bypass="None",
    )
    assert storage_blocker(state) is not None


def test_logging_only_bypass_does_not_help_azure_ml():
    """`Logging, Metrics` is a bypass value that excludes AzureServices."""
    state = StorageReachability(
        account_name="sa",
        public_network_access="Disabled",
        bypass="Logging, Metrics",
    )
    assert storage_blocker(state) is not None


def test_bypass_is_matched_case_insensitively_within_a_list():
    state = StorageReachability(
        account_name="sa",
        public_network_access="Disabled",
        bypass="Logging, Metrics, azureservices",
    )
    assert storage_blocker(state) is None


def test_unread_bypass_does_not_manufacture_a_pass():
    """None means "not read". It must not be treated as permission to deploy."""
    state = StorageReachability(
        account_name="sa",
        public_network_access="Disabled",
        bypass=None,
    )
    assert storage_blocker(state) is not None


# --- The credential axis ----------------------------------------------------
#
# Everything above decides whether the account can be *reached*. polandcentral
# showed that is only half the question. `mlw-ffsft-plc` had two private
# endpoints and an isolated workspace -- the third arrangement `storage_blocker`
# calls fine, and it was right about the network -- while every write returned:
#
#     (KeyBasedAuthenticationNotPermitted) Key based authentication is not
#     permitted on this storage account.
#
# All four of its datastores carried `credentialsType: AccountKey` against an
# account with `allowSharedKeyAccess: false`. The account refuses the key its
# own datastores present. No private endpoint and no role assignment reaches
# that, because the key is refused before either is consulted -- and the
# symptom, artifacts=0 on a finished run, is the one the network check already
# claims to explain. See docs/VERIFIED.md 58.

KEYED = ["workspaceblobstore", "workspaceartifactstore"]


def test_key_based_datastore_on_a_no_key_account_is_blocked():
    """A green network answer says nothing about this axis."""
    state = make(
        public_access="Disabled",
        private_endpoints=["pe-blob"],
        isolation_mode="AllowInternetOutbound",
        allow_shared_key=False,
        key_based_datastores=KEYED,
    )
    assert _network_blocker(state) is None
    blocker = storage_blocker(state)
    assert blocker is not None
    assert "credentialsType=AccountKey" in blocker


def test_key_mismatch_blocks_even_when_public_access_is_on():
    state = make(
        public_access="Enabled",
        allow_shared_key=False,
        key_based_datastores=KEYED,
    )
    assert _network_blocker(state) is None
    assert storage_blocker(state) is not None


def test_a_hardened_account_with_identity_datastores_is_fine():
    """`allowSharedKeyAccess: false` on its own is the designed posture."""
    state = make(
        public_access="Disabled",
        private_endpoints=["pe-blob"],
        isolation_mode="AllowInternetOutbound",
        allow_shared_key=False,
        key_based_datastores=[],
    )
    assert storage_blocker(state) is None


def test_key_based_datastores_are_fine_when_the_account_allows_keys():
    state = make(
        public_access="Enabled",
        allow_shared_key=True,
        key_based_datastores=KEYED,
    )
    assert storage_blocker(state) is None


def test_an_unread_key_policy_is_never_a_blocker():
    """`None` means not read, and an unread property must not stop a deployment."""
    state = make(
        public_access="Enabled",
        allow_shared_key=None,
        key_based_datastores=KEYED,
    )
    assert state.key_auth_refused is False
    assert storage_blocker(state) is None


def test_both_blockers_are_reported_together():
    """`mlw-ffsft-jpe`: 0 private endpoints AND four AccountKey datastores."""
    state = make(
        public_access="Disabled",
        private_endpoints=[],
        allow_shared_key=False,
        key_based_datastores=KEYED,
    )
    blocker = storage_blocker(state)
    assert "credentialsType=AccountKey" in blocker
    assert "no private endpoint and no public path" in blocker
