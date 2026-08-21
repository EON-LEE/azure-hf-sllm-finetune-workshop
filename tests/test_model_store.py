"""The second false green: a pattern is not deployable without a model asset.

`check_pattern` used to answer one question -- "is there enough dedicated GPU
quota?" -- and answer `None` (deployable) for every LowPriority pattern, because
LowPriority patterns ignore dedicated quota. That made `ffsft-deploy check`
report both batch patterns as `ok`.

They are not ok. Measured on this subscription while trying to register the
adapter produced by `heroic_fennel_085y2rwm3s`:

    azureml://jobs/heroic_fennel_085y2rwm3s/outputs/artifacts/paths/outputs/
    -> (NoMatchingArtifactsFoundFromJob) No artifacts matching outputs found

and the artifact API agrees -- three separate finished runs each report
`artifacts=0`. Nothing the job wrote ever left the node. The cause is one
property on the workspace's own storage account:

    publicNetworkAccess = Disabled     with 0 private endpoint connections

An account with the public endpoint off and no private endpoint is reachable by
nobody: not this laptop, and not the Azure ML compute node, which is why
`mount_outputs=True` fails at node start and why `./outputs` never uploads.

Both AML endpoint kinds -- online *and* batch -- take a registered model as
input. No datastore means no model asset, which means no batch endpoint, no
matter how much LowPriority quota exists. The check has to say so.
"""

from __future__ import annotations

import pytest

from ffsft.deploy.endpoint import check_pattern, classify_store
from ffsft.deploy.registry import get_serving_registry

ACCOUNT = "mlwffsftstorage8cb451dd1"


def test_enabled_public_access_is_reachable():
    probe = classify_store(ACCOUNT, "Enabled", 0)
    assert probe.reachable is True


def test_private_endpoint_rescues_a_disabled_account():
    """Disabling public access is the *designed* posture -- with a private link."""
    probe = classify_store(ACCOUNT, "Disabled", 1)
    assert probe.reachable is True


def test_disabled_with_no_private_endpoint_is_unreachable():
    probe = classify_store(ACCOUNT, "Disabled", 0)
    assert probe.reachable is False


def test_unreachable_detail_names_the_account_and_the_property():
    probe = classify_store(ACCOUNT, "Disabled", 0)
    assert ACCOUNT in probe.detail
    assert "publicNetworkAccess" in probe.detail


def test_unreachable_detail_offers_the_private_endpoint_remedy():
    probe = classify_store(ACCOUNT, "Disabled", 0)
    assert "private endpoint" in probe.detail.lower()


def test_unknown_public_access_does_not_block():
    """Never turn 'I could not read it' into 'it is broken'."""
    probe = classify_store(ACCOUNT, "Unknown", 0)
    assert probe.reachable is True


def test_probe_keeps_what_it_measured():
    probe = classify_store(ACCOUNT, "Disabled", 0)
    assert probe.account == ACCOUNT
    assert probe.public_access == "Disabled"
    assert probe.private_endpoints == 0


@pytest.mark.parametrize("key", ["aml_batch", "aml_batch_vllm", "aml_online_vllm", "aks_vllm"])
def test_hosted_patterns_require_a_model_asset(key):
    assert get_serving_registry().get(key).requires_model_asset is True


def test_local_pattern_needs_no_model_asset():
    assert get_serving_registry().get("local_vllm").requires_model_asset is False


def test_batch_is_blocked_when_the_store_is_unreachable():
    """The regression this file exists for: previously `None`."""
    store = classify_store(ACCOUNT, "Disabled", 0)
    _, blocker = check_pattern("aml_batch", "sub", "koreacentral", store=store)
    assert blocker is not None
    assert ACCOUNT in blocker


def test_batch_is_clear_when_the_store_is_reachable():
    store = classify_store(ACCOUNT, "Enabled", 0)
    _, blocker = check_pattern("aml_batch", "sub", "koreacentral", store=store)
    assert blocker is None


def test_omitting_the_store_preserves_the_old_answer():
    """Callers that cannot probe must not start seeing invented blockers."""
    _, blocker = check_pattern("aml_batch", "sub", "koreacentral")
    assert blocker is None


def test_local_pattern_ignores_the_store():
    store = classify_store(ACCOUNT, "Disabled", 0)
    _, blocker = check_pattern("local_vllm", "sub", "koreacentral", store=store)
    assert blocker is None
