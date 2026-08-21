"""Turning an AmlCompute rejection into something a human can act on.

`ffsft-deploy check` used to report `aml_online_vllm ok` because
`StandardNVADSA10v5Family` has a quota of 72. It cannot deploy. Creating an
A10 v5 cluster in this workspace is refused outright, at either tier, and the
72 cores are unreachable. Quota is necessary and not sufficient, and a green
line that means "there is quota" reads as "this will work" -- which is how
§15 spent six GPU hours and $12.9 on four deployments that were never going
to allocate.

The two rejections seen so far, both from a real create call:

    ClusterMinNodesExceedCoreQuota: The specified subscription has a Standard
    NCADSA100v4 family vCPU quota of 0 and cannot accomodate for at least 1
    requested managed compute nodes which maps to 24 vCPUs.

    InvalidPropertyValue: The specified value Standard_NV18ads_A10_v5 for
    property Cluster.Properties.VMSize is not a supported VM size. Supported
    VM sizes: STANDARD_D1,STANDARD_D2,...

The second message is actively misleading: the list it offers is old enough
to omit `Standard_NC24ads_A100_v4`, which this project trains on daily. So
the explanation must not repeat it.

Both failures return in seconds and leave no resource behind, which is what
makes probing free.
"""

from __future__ import annotations

from ffsft.deploy import endpoint

QUOTA_MSG = (
    '{"error":{"code":"ClusterMinNodesExceedCoreQuota","message":"The specified '
    "subscription has a Standard NCADSA100v4 family vCPU quota of 0 and cannot "
    "accomodate for at least 1 requested managed compute nodes which maps to 24 "
    'vCPUs. Talk to your Subscription Admin"}}'
)

SKU_MSG = (
    '{"error":{"code":"InvalidPropertyValue","message":"The specified value '
    "Standard_NV18ads_A10_v5 for property Cluster.Properties.VMSize is not a "
    "supported VM size. Supported VM sizes: STANDARD_D1,STANDARD_D2,STANDARD_NC6,"
    'STANDARD_NV24"}}'
)


def test_a_zero_quota_is_named_as_a_quota_problem():
    code, why = endpoint.classify_cluster_error(QUOTA_MSG)
    assert code == "ClusterMinNodesExceedCoreQuota"
    assert "quota" in why.lower()


def test_the_quota_explanation_carries_the_number_and_the_family():
    """'Ask for more quota' is only actionable if it says how much of what."""
    _, why = endpoint.classify_cluster_error(QUOTA_MSG)
    assert "0" in why
    assert "NCADSA100v4" in why


def test_an_unavailable_sku_is_not_reported_as_a_quota_problem():
    """These need different actions: raise a ticket vs pick another SKU."""
    code, why = endpoint.classify_cluster_error(SKU_MSG)
    assert code == "InvalidPropertyValue"
    assert "quota" not in why.lower()


def test_the_unavailable_sku_explanation_names_the_sku():
    _, why = endpoint.classify_cluster_error(SKU_MSG)
    assert "Standard_NV18ads_A10_v5" in why


def test_the_misleading_supported_list_is_not_repeated():
    """It omits the SKU we train on every day, so echoing it sends people wrong."""
    _, why = endpoint.classify_cluster_error(SKU_MSG)
    assert "STANDARD_D1" not in why
    assert "STANDARD_NC6" not in why


def test_an_unrecognised_failure_is_passed_through_rather_than_guessed_at():
    code, why = endpoint.classify_cluster_error("connection reset by peer")
    assert code == "Unknown"
    assert "connection reset" in why


def test_a_probe_that_created_a_cluster_reports_no_blocker():
    probe = endpoint.SkuProbe(sku="Standard_NC24ads_A100_v4", tier="LowPriority",
                              creatable=True, code="", detail="")
    assert probe.creatable
    assert probe.blocker is None


def test_a_probe_that_was_refused_explains_itself():
    code, why = endpoint.classify_cluster_error(QUOTA_MSG)
    probe = endpoint.SkuProbe(sku="Standard_NC24ads_A100_v4", tier="Dedicated",
                              creatable=False, code=code, detail=why)
    assert probe.blocker is not None
    assert "ClusterMinNodesExceedCoreQuota" in probe.blocker
