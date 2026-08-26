"""`egressPublicNetworkAccess` must not be set on a managed-VNet workspace.

Setting it there is not a preference Azure tolerates -- it is a 400 at submit
time, returned before any node is allocated::

    (InferencingClientCallFailed) Validation:
      "Deployments with enabled private networking require a premium tier ACR
       which supports private networking capabilities."
      "The EgressPublicNetworkAccess under online deployment is no longer
       supported when your workspace is secured with managed virtual network.
       Please avoid setting EgressPublicNetworkAccess on the deployment in this
       case."

Measured on `mlw-ffsft-plc`, whose `managedNetwork.isolationMode` is
`AllowInternetOutbound` with `status: Active`, and whose serve registry
`acrffsftkc` is Basic tier.

The trap these tests exist to hold shut: ARM reports `egressPublicNetworkAccess:
Enabled` for a deployment that never set it. The live `blue` deployment reads
`Enabled` and is `Succeeded`, which makes "Enabled" look like a working value to
copy. It is a default-on-read; only an explicitly sent value reaches the
validator. See docs/JOURNAL.md S64.
"""

from ffsft.deploy.endpoint import egress_for
from ffsft.deploy.preflight import StorageReachability


def isolated() -> StorageReachability:
    """The measured `mlw-ffsft-plc` posture: managed VNet, private storage."""
    return StorageReachability(
        account_name="mlwffsftstorage09dd66111",
        workspace_isolation_mode="AllowInternetOutbound",
        private_endpoints=("pe-blob", "pe-file"),
        public_network_access="Disabled",
    )


def not_isolated() -> StorageReachability:
    return StorageReachability(
        account_name="somestorage",
        workspace_isolation_mode="Disabled",
        public_network_access="Enabled",
    )


def test_a_managed_vnet_workspace_gets_no_setting():
    assert egress_for(None, isolated()) is None


def test_an_explicit_ask_is_dropped_rather_than_sent_to_be_rejected():
    """The flag is a request Azure will refuse, so honouring it helps nobody.

    Both directions: `disabled` additionally trips the Premium-ACR clause, and
    `enabled` -- the value ARM shows for working deployments -- trips the
    managed-VNet clause on its own.
    """
    assert egress_for("disabled", isolated()) is None
    assert egress_for("enabled", isolated()) is None


def test_without_a_managed_vnet_an_explicit_choice_is_honoured():
    assert egress_for("disabled", not_isolated()) == "disabled"
    assert egress_for("enabled", not_isolated()) == "enabled"


def test_an_unread_workspace_leaves_the_setting_alone():
    """`read_storage_reachability` returns None when the workspace is unreadable.

    Guessing from nothing is how the previous version of this function shipped a
    value that Azure rejected outright, so an unread workspace sends nothing.
    """
    assert egress_for(None, None) is None


def test_the_arrangement_azure_rejected_is_never_produced():
    """The exact call the failed deploy made: blob weights, managed VNet.

    The old signature derived `disabled` from the presence of a blob URI. The
    weight source was never the axis that decided this; the workspace's
    isolation mode was.
    """
    assert egress_for(None, isolated()) is None


def test_nothing_is_sent_by_default_on_an_ordinary_workspace():
    assert egress_for(None, not_isolated()) is None
