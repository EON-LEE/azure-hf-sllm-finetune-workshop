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


# --- The Hub escape hatch -------------------------------------------------
# Section 24 found the workspace storage account unreachable and concluded every
# hosted pattern was blocked. That was too strong. A vLLM online deployment can
# be handed a Hugging Face repo id instead of a model asset, and then it never
# touches a datastore at all -- which is exactly how ffsft-a10 deployed on an
# A10 while the storage account was still dark.

DEAD_STORE = dict(
    account="mlwffsftstorage8cb451dd1",
    public_access="Disabled",
    private_endpoints=0,
    reachable=False,
    detail="datastore UNREACHABLE: publicNetworkAccess=Disabled with 0 private endpoints",
)


def _dead():
    from ffsft.deploy.endpoint import StoreProbe

    return StoreProbe(**DEAD_STORE)


def test_online_vllm_can_serve_from_hub():
    from ffsft.deploy.endpoint import get_serving_registry

    assert get_serving_registry().get("aml_online_vllm").can_serve_from_hub is True


def test_batch_cannot_serve_from_hub():
    """A batch deployment names a model asset; there is no --model flag to swap."""
    from ffsft.deploy.endpoint import get_serving_registry

    assert get_serving_registry().get("aml_batch").can_serve_from_hub is False


def test_from_hub_clears_the_store_blocker_for_online(monkeypatch):
    from ffsft.deploy import endpoint as ep
    from ffsft.deploy import probes

    # Patched on `probes`, not on `ep`: that is the module `check_pattern`
    # reaches for. Patching the re-export instead is silent -- the call went
    # out to management.azure.com for real when the probes moved out of
    # endpoint.py and this line still said `ep`.
    monkeypatch.setattr(probes, "read_dedicated_quota", lambda *a, **k: 9999)
    _, blocked = ep.check_pattern(
        "aml_online_vllm", "sub", "koreacentral", store=_dead()
    )
    assert blocked is not None and "unreachable" in blocked.lower()
    _, unblocked = ep.check_pattern(
        "aml_online_vllm", "sub", "koreacentral", store=_dead(), from_hub=True
    )
    assert unblocked is None, f"Hub path must not be storage-blocked, got: {unblocked}"


def test_from_hub_does_not_rescue_batch(monkeypatch):
    from ffsft.deploy import endpoint as ep
    from ffsft.deploy import probes

    monkeypatch.setattr(probes, "read_dedicated_quota", lambda *a, **k: 9999)
    _, blocked = ep.check_pattern(
        "aml_batch", "sub", "koreacentral", store=_dead(), from_hub=True
    )
    assert blocked is not None, "batch has no Hub path; it must stay blocked"


# --- The third failure mode: credentials, not reachability ------------------
#
# Everything above measures whether the account can be *reached*. polandcentral
# proved that is only half the check. `mlw-ffsft-plc` sat behind two healthy
# private endpoints -- `classify_store` said reachable, and it was right about
# the network -- yet every finished run still reported artifacts=0, job logs
# never uploaded, and `jobs.download()` returned:
#
#     (KeyBasedAuthenticationNotPermitted) Key based authentication is not
#     permitted on this storage account.
#
# All four of its datastores carried `credentialsType: AccountKey` against a
# storage account with `allowSharedKeyAccess: false`. The account refuses the
# key the datastore insists on presenting. No private endpoint and no role
# assignment can fix that, and the symptom is identical to an unreachable
# account -- which is exactly why the old check passed it.
#
# Not inferable from how the workspace was made: koreacentral `mlw-ffsft` came
# up `None` and polandcentral `mlw-ffsft-plc` came up `AccountKey`, created the
# same way on the same subscription.

KEYED = ("workspaceblobstore", "workspaceartifactstore")


def test_key_based_datastore_on_a_no_key_account_is_unreachable():
    """Two private endpoints do not rescue a credential the account refuses."""
    probe = classify_store(
        ACCOUNT, "Disabled", 2, allow_shared_key=False, key_based_datastores=KEYED
    )
    assert probe.reachable is False
    assert probe.key_auth_refused is True


def test_key_mismatch_blocks_even_with_public_access_on():
    """The key is refused on the public endpoint too -- the axes are orthogonal."""
    probe = classify_store(
        ACCOUNT, "Enabled", 0, allow_shared_key=False, key_based_datastores=KEYED
    )
    assert probe.reachable is False


def test_key_mismatch_detail_names_the_datastores_and_the_fix():
    probe = classify_store(
        ACCOUNT, "Disabled", 2, allow_shared_key=False, key_based_datastores=KEYED
    )
    assert "workspaceblobstore" in probe.detail
    assert "allowSharedKeyAccess" in probe.detail
    assert "credentialsType" in probe.detail
    # The PUT that applies the fix is rejected without this, which cost a round.
    assert "isDefault" in probe.detail


def test_identity_based_datastores_on_a_no_key_account_are_fine():
    """`allowSharedKeyAccess: false` is the hardened posture, not a fault."""
    probe = classify_store(
        ACCOUNT, "Disabled", 2, allow_shared_key=False, key_based_datastores=()
    )
    assert probe.reachable is True
    assert probe.key_auth_refused is False


def test_key_based_datastores_are_fine_when_the_account_allows_keys():
    probe = classify_store(
        ACCOUNT, "Disabled", 2, allow_shared_key=True, key_based_datastores=KEYED
    )
    assert probe.reachable is True


def test_an_unread_key_policy_never_fails_the_check():
    """A probe that cannot see is not a resource that is broken (module rule)."""
    probe = classify_store(
        ACCOUNT, "Disabled", 2, allow_shared_key=None, key_based_datastores=KEYED
    )
    assert probe.reachable is True


def test_the_credential_axis_is_optional_so_existing_callers_are_unchanged():
    assert classify_store(ACCOUNT, "Disabled", 1).reachable is True
    assert classify_store(ACCOUNT, "Disabled", 0).reachable is False


def test_both_blockers_are_reported_together():
    """`mlw-ffsft-jpe` has both at once: 0 private endpoints AND AccountKey
    datastores on a no-key account. Naming only the credential fault would send
    the caller back for a second round on a blocker already visible here."""
    probe = classify_store(
        ACCOUNT, "Disabled", 0, allow_shared_key=False, key_based_datastores=KEYED
    )
    assert probe.reachable is False
    assert "credentialsType" in probe.detail
    assert "0 private endpoints" in probe.detail
