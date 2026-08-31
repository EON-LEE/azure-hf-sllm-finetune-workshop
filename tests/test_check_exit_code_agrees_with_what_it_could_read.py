"""`ffsft-deploy check` must not exit 0 over a workspace that answered nothing.

Round 4 gave `cmd_check` the LOOKED IN scope header, so the *prose* of the
report says which workspace it read. It still ended in an unconditional
`return 0`, so:

    ffsft-deploy check && echo ok

printed `ok` over a subscription where `probe_model_store` had swallowed a 403
(it returns `classify_store("unknown", "Unknown", 0)`, which comes back
`reachable=True`, so no datastore line printed at all) and every dedicated
quota read had failed. That is the same defect `lifecycle.cmd_status` fixed with
`EXIT_COULD_NOT_LOOK = 1`: the prose said UNKNOWN and the exit code -- the only
channel a script reads -- said success. This module pins the reuse of that
constant rather than a second numbering scheme, and 2 is not available: every
usage refusal in `lifecycle.py` already returns it.

The other half of the same split is `deploy_online`'s pre-delete cleanup, which
spelled a 403 exactly the way it spelled a 404 -- `log.debug`, invisible -- and
then created a deployment on the assumption that nothing was there.

Both fixes are one-directional, and the tests below pin both directions,
because the expensive mistake in this repo has been over-correcting: a check
whose reads all succeeded must still exit 0 even when every pattern is BLOCKED,
a 404 on the pre-delete get must stay quiet, and a read that failed must never
stop a deployment that was asked for.
"""

from __future__ import annotations

import argparse
import logging

import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

import ffsft.deploy.endpoint as ep
import ffsft.deploy.identity as ident
import ffsft.deploy.lifecycle as lifecycle
import ffsft.deploy.preflight as pf
import ffsft.deploy.probes as probes
from ffsft.deploy.probes import StoreProbe, classify_store

FFSFT_VARS = (
    "FFSFT_SUBSCRIPTION_ID",
    "AZURE_SUBSCRIPTION_ID",
    "FFSFT_TENANT_ID",
    "AZURE_TENANT_ID",
    "FFSFT_RESOURCE_GROUP",
    "FFSFT_WORKSPACE",
    "FFSFT_LOCATION",
    "FFSFT_COMPUTE",
    "FFSFT_SKU",
    "FFSFT_VM_PRIORITY",
)

READABLE_STORE = StoreProbe(
    account="stffsft",
    public_access="Enabled",
    private_endpoints=0,
    reachable=True,
    detail="",
)

#: Exactly what `probe_model_store` hands back when its three ARM GETs raise.
#: Built through `classify_store` rather than by hand so that this test keeps
#: measuring the real sentinel if that call ever changes shape.
UNREAD_STORE = classify_store("unknown", "Unknown", 0)


def _forbidden(what: str) -> HttpResponseError:
    err = HttpResponseError(f"(AuthorizationFailed) no permission to read {what}")
    err.status_code = 403
    return err


@pytest.fixture
def run_check(monkeypatch, capsys):
    """Drive `cmd_check` with no Azure at all; return (exit code, stdout).

    Every seam is patched on `endpoint`, because that is the module `cmd_check`
    reaches for -- patching `probes.check_pattern` would fake a name this caller
    never reads and the real call would leave the machine (CLAUDE.md,
    `tests/test_deploy_module_split.py`).
    """

    def run(*, store=READABLE_STORE, check_pattern=None, quota=None, probe=False):
        for name in FFSFT_VARS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "sub-participant")
        monkeypatch.setenv("FFSFT_RESOURCE_GROUP", "rg-mine")
        monkeypatch.setenv("FFSFT_WORKSPACE", "mlw-mine")
        monkeypatch.setenv("FFSFT_LOCATION", "koreacentral")

        monkeypatch.setattr(ep, "probe_model_store", lambda target: store)
        monkeypatch.setattr(
            ep,
            "check_pattern",
            check_pattern
            or (lambda key, sub, loc, **kw: (ep.get_serving_registry().get(key), None)),
        )
        monkeypatch.setattr(ep, "read_dedicated_quota", quota or (lambda sub, loc, family: 24))

        def no_network(*a, **kw):
            raise AssertionError("the suite must not reach management.azure.com")

        monkeypatch.setattr(probes, "read_dedicated_quota", no_network)
        capsys.readouterr()
        code = ep.cmd_check(argparse.Namespace(probe=probe))
        return code, capsys.readouterr().out

    return run


# --------------------------------------------------------------------------
# the exit code has to agree with the report -- and only when it disagrees


def test_a_check_whose_every_read_answered_still_exits_zero(run_check):
    """The over-correction guard. A clean first run must stay scriptable."""
    code, out = run_check()

    assert code == 0
    assert "COULD NOT LOOK" not in out


def test_a_pattern_that_is_blocked_is_an_answer_and_still_exits_zero(run_check):
    """BLOCKED is a read that succeeded and said no. It is not a failed look.

    Collecting blockers here would make `check` exit non-zero on the ordinary
    subscription this repo is written for -- dedicated GPU quota is 0 there --
    and an exit code that is always 1 tells a script nothing.
    """
    code, out = run_check(
        check_pattern=lambda key, sub, loc, **kw: (
            ep.get_serving_registry().get(key),
            "dedicated quota for StandardNVADSA10v5Family is 0",
        )
    )

    assert "BLOCKED" in out
    assert code == 0


def test_check_exits_could_not_look_when_the_datastore_posture_was_never_read(run_check):
    code, out = run_check(store=UNREAD_STORE)

    assert code == ep.EXIT_COULD_NOT_LOOK
    assert "COULD NOT LOOK" in out


def test_an_unread_datastore_posture_is_no_longer_reported_as_nothing_at_all(run_check):
    """`reachable=True` on the sentinel meant the old report printed no line.

    The row is the point: an operator who reads "ok?" under a header naming
    their workspace has been told the storage account was checked.
    """
    _, out = run_check(store=UNREAD_STORE)

    assert "UNKNOWN" in out
    assert "UNREACHABLE" not in out
    assert "storage account" in out


def test_check_exits_could_not_look_when_the_dedicated_quota_read_is_refused(run_check):
    def refuse(sub, loc, family):
        raise _forbidden(f"quota family {family}")

    code, out = run_check(quota=refuse)

    assert code == ep.EXIT_COULD_NOT_LOOK
    assert "StandardNVADSA10v5Family" in out


def test_a_refused_quota_read_is_a_row_in_the_table_rather_than_a_traceback(run_check):
    """It used to escape `cmd_check` entirely, partway down the table.

    The rows printed before it then stood as the whole report, with the exit
    code coming from the interpreter rather than from this command.
    """

    def refuse(sub, loc, family):
        raise _forbidden(f"quota family {family}")

    code, out = run_check(quota=refuse)

    assert "aml_online_vllm" in out
    assert "could not read dedicated" in out
    assert code == ep.EXIT_COULD_NOT_LOOK


def test_check_exits_could_not_look_when_check_pattern_itself_cannot_read(run_check):
    """`check_pattern` does its own quota read, so it raises too."""

    def refuse(key, sub, loc, **kw):
        raise _forbidden("Microsoft.Quota")

    code, out = run_check(check_pattern=refuse)

    assert code == ep.EXIT_COULD_NOT_LOOK
    assert "the read failed" in out


def test_check_names_every_read_that_failed_and_not_just_the_first(run_check):
    def refuse_quota(sub, loc, family):
        raise _forbidden(f"quota family {family}")

    _, out = run_check(store=UNREAD_STORE, quota=refuse_quota)

    footer = out.split("COULD NOT LOOK", 1)[1]
    assert "storage account" in footer
    assert "StandardNVADSA10v5Family" in footer


def test_check_reuses_the_exit_code_lifecycle_defined_rather_than_a_second_scheme(run_check):
    assert ep.EXIT_COULD_NOT_LOOK is lifecycle.EXIT_COULD_NOT_LOOK

    code, _ = run_check(store=UNREAD_STORE)

    assert code == lifecycle.EXIT_COULD_NOT_LOOK


def test_a_failed_look_does_not_collide_with_the_code_that_means_usage_error(run_check):
    """2 is what every refusal in `lifecycle.py` returns for "you did not say
    what you meant". A workspace that did not answer is not a typo."""
    code, _ = run_check(store=UNREAD_STORE)

    assert code != 2
    assert code != 0


def test_probe_mode_on_a_workspace_that_answers_still_exits_zero(run_check, monkeypatch):
    """`--probe` builds a real client, so it gets its own direction pinned."""
    monkeypatch.setattr(ep, "probe_sku", lambda client, sku, tier, **kw: ep.SkuProbe(
        sku=sku, tier=tier, creatable=True, code="", detail=""
    ))

    from ffsft import azure_ml

    monkeypatch.setattr(azure_ml, "get_ml_client", lambda *a, **k: object())
    code, out = run_check(probe=True)

    assert code == 0
    assert "create accepted" in out


# --------------------------------------------------------------------------
# the pre-delete cleanup: 404 is absence, 403 is not


class _Poller:
    def __init__(self, value):
        self._value = value

    def result(self):
        return self._value


class _FailedDeployment:
    """The corpse the pre-delete block exists to clear: Azure refuses to update
    a deployment whose first provisioning failed."""

    provisioning_state = "Failed"


class FakeDeployments:
    """`get` raises whatever the test hands it; creates are recorded.

    `existing` swaps the raise for a returned entity, which is the only way to
    reach the delete at all -- the branch under it is guarded on the state.
    """

    def __init__(self, error, existing=None, delete_error=None):
        self._error = error
        self._existing = existing
        self._delete_error = delete_error
        self.created = []
        self.deleted = []

    def get(self, name=None, endpoint_name=None):
        if self._existing is not None:
            return self._existing
        raise self._error

    def begin_delete(self, name=None, endpoint_name=None):
        self.deleted.append((endpoint_name, name))
        if self._delete_error is not None:
            raise self._delete_error
        return _Poller(None)

    def begin_create_or_update(self, deployment):
        self.created.append(deployment)
        return _Poller(deployment)


class FakeEndpointEntity:
    def __init__(self):
        self.traffic = {}
        self.identity = None
        self.scoring_uri = "https://ffsft-online.koreacentral.inference.ml.azure.com/score"


class FakeEndpoints:
    def __init__(self):
        self.entity = FakeEndpointEntity()

    def get(self, name=None, **_kw):
        return self.entity

    def begin_create_or_update(self, entity):
        return _Poller(entity)


class FakeEnvironments:
    def get(self, name, version=None):
        raise ResourceNotFoundError(f"no environment {name}:{version}")


class FakeClient:
    def __init__(self, error, **kw):
        self.online_deployments = FakeDeployments(error, **kw)
        self.online_endpoints = FakeEndpoints()
        self.environments = FakeEnvironments()


def _deploy_with_predelete_error(monkeypatch, error, **kw):
    """Run `deploy_online` where only the pre-delete GET misbehaves."""
    for name in FFSFT_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "00000000-0000-0000-0000-0000000000ff")
    monkeypatch.setenv("FFSFT_TENANT_ID", "00000000-0000-0000-0000-0000000000ee")

    from ffsft import azure_ml

    spec = ep.get_serving_registry().get("aml_online_vllm")
    monkeypatch.setattr(ep, "check_pattern", lambda *a, **k: (spec, None))
    monkeypatch.setattr(ep, "ensure_endpoint", lambda *a, **k: None)
    monkeypatch.setattr(pf, "read_storage_reachability", lambda *a, **k: None)
    monkeypatch.setattr(pf, "read_sku_availability", lambda *a, **k: None)
    monkeypatch.setattr(pf, "online_endpoint_blocker", lambda *a, **k: None)
    monkeypatch.setattr(pf, "sku_advisory", lambda *a, **k: None)
    # None short-circuits the whole AcrPull block, which is not what is under
    # test here and would need three more fakes.
    monkeypatch.setattr(ident, "acr_id_for_image", lambda *a, **k: None)

    client = FakeClient(error, **kw)
    monkeypatch.setattr(azure_ml, "get_ml_client", lambda *a, **k: client)

    ep.deploy_online(
        "ffsft-online",
        None,
        hf_model="Qwen/Qwen3.5-0.8B",
        image="myacr.azurecr.io/ffsft-serve:1",
    )
    return client


def test_a_404_on_the_pre_delete_get_stays_quiet_because_absence_is_the_first_run(
    monkeypatch, caplog
):
    """The other over-correction guard: every clean first deploy hits this path.

    Warning here would put a scary line in the log of every successful first
    run, which is how a warning stops being read.
    """
    with caplog.at_level(logging.WARNING, logger="ffsft.deploy.endpoint"):
        client = _deploy_with_predelete_error(monkeypatch, ResourceNotFoundError("no deployment"))

    assert client.online_deployments.created, "the deploy itself must still happen"
    assert [r.message for r in caplog.records if "exist" in r.message] == []


def test_a_403_on_the_pre_delete_get_is_reported_where_the_operator_sees_it(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="ffsft.deploy.endpoint"):
        _deploy_with_predelete_error(monkeypatch, _forbidden("online deployments"))

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("could not check whether deployment 'blue'" in m for m in warnings), warnings
    assert any("not evidence that it does not" in m for m in warnings), warnings


def test_a_403_on_the_pre_delete_get_does_not_stop_the_deployment_being_created(monkeypatch):
    """Round 4 shipped an over-correction that had to be pinned shut. This is
    that pin for this fix: a read that failed is reported, never enforced."""
    client = _deploy_with_predelete_error(monkeypatch, _forbidden("online deployments"))

    assert [d.name for d in client.online_deployments.created] == ["blue"]


def test_an_error_that_is_not_a_404_at_all_is_not_read_as_absence(monkeypatch, caplog):
    """A timeout is not a 404 either, and it used to be spelled like one."""
    with caplog.at_level(logging.WARNING, logger="ffsft.deploy.endpoint"):
        _deploy_with_predelete_error(monkeypatch, TimeoutError("read timed out"))

    assert any("could not check whether deployment" in r.getMessage() for r in caplog.records)


def test_a_404_carried_on_a_plain_exception_is_still_read_as_absence():
    """`ResourceNotFoundError` built without a response has status_code None,
    which is why the isinstance check exists beside the duck-typed one."""
    plain = Exception("gone")
    plain.status_code = 404

    assert ep._absence_is_proven(plain)
    assert ep._absence_is_proven(ResourceNotFoundError("no deployment"))
    assert not ep._absence_is_proven(_forbidden("online deployments"))
    assert not ep._absence_is_proven(TimeoutError("read timed out"))


# --------------------------------------------------------------------------
# --probe: a question nobody asked is not an answer (§75)


def _unasked_probe(client, sku, tier, **kw):
    """What `probe_sku` returns when the probe name is already taken: the
    control plane was never called, so nothing here is about the SKU."""
    return ep.SkuProbe(
        sku=sku,
        tier=tier,
        creatable=False,
        code="ProbeNameTaken",
        detail=(
            f"a compute named '{kw.get('name', 'ffsft-probe-0')}' already exists and this "
            f"probe did not create it, so nothing was created and nothing was deleted. "
            f"{sku} was NOT tested at {tier} -- this line is about the name, not the SKU."
        ),
        probed=False,
    )


def test_probe_mode_that_never_asked_the_control_plane_does_not_exit_zero(run_check, monkeypatch):
    """Executed before the fix, against a workspace already owning
    `ffsft-probe-0`: every pattern row read `BLOCKED ProbeNameTaken ...
    Standard_NV36ads_A10_v5 was NOT tested`, and rc was 0, because a blocker is
    an answer and answers are not collected in `blind`. `check --probe && echo
    ok` printed ok over a workspace where nothing had been probed."""
    monkeypatch.setattr(ep, "probe_sku", _unasked_probe)

    from ffsft import azure_ml

    monkeypatch.setattr(azure_ml, "get_ml_client", lambda *a, **k: object())
    code, out = run_check(probe=True)

    assert code == ep.EXIT_COULD_NOT_LOOK
    assert "COULD NOT LOOK" in out


def test_a_probe_name_refusal_is_not_printed_as_the_sku_being_blocked(run_check, monkeypatch):
    """The prose half. BLOCKED is the word this table uses for a control plane
    that answered no; a probe that never called it may not borrow the word."""
    monkeypatch.setattr(ep, "probe_sku", _unasked_probe)

    from ffsft import azure_ml

    monkeypatch.setattr(azure_ml, "get_ml_client", lambda *a, **k: object())
    _, out = run_check(probe=True)

    probe_rows = [line for line in out.splitlines() if "ProbeNameTaken" in line]
    assert probe_rows, out
    assert not any("BLOCKED" in line for line in probe_rows), probe_rows
    assert any("UNKNOWN" in line for line in probe_rows), probe_rows


def test_a_probe_that_the_control_plane_refused_still_exits_zero(run_check, monkeypatch):
    """The over-correction guard, at the exit code this time: a create that came
    back ClusterMinNodesExceedCoreQuota was measured, and dedicated GPU quota is
    0 on the ordinary subscription this repo is written for."""
    monkeypatch.setattr(ep, "probe_sku", lambda client, sku, tier, **kw: ep.SkuProbe(
        sku=sku, tier=tier, creatable=False, code="ClusterMinNodesExceedCoreQuota",
        detail="dedicated quota for this family is 0.",
    ))

    from ffsft import azure_ml

    monkeypatch.setattr(azure_ml, "get_ml_client", lambda *a, **k: object())
    code, out = run_check(probe=True)

    assert "BLOCKED" in out
    assert code == 0


def test_a_probe_cluster_that_could_not_be_deleted_is_reported_and_not_exit_zero(
    run_check, monkeypatch
):
    """`check --help` calls the probe free -- "a refusal creates nothing, an
    acceptance is deleted". When the delete fails, that sentence is false and the
    only channel that says so used to be `log.warning`, which `check` never
    prints."""
    monkeypatch.setattr(ep, "probe_sku", lambda client, sku, tier, **kw: ep.SkuProbe(
        sku=sku, tier=tier, creatable=True, code="", detail="",
        leftover="the probe cluster 'ffsft-probe-0' this run created is still there: the "
                 "delete failed with HttpResponseError: 409, and nothing here can confirm "
                 "it is gone.",
    ))

    from ffsft import azure_ml

    monkeypatch.setattr(azure_ml, "get_ml_client", lambda *a, **k: object())
    code, out = run_check(probe=True)

    assert "create accepted" in out
    assert "ffsft-probe-0" in out
    assert code == ep.EXIT_COULD_NOT_LOOK


def test_a_refused_delete_of_a_failed_deployment_is_not_logged_as_a_failed_check(
    monkeypatch, caplog
):
    """The GET and the DELETE shared one `except`, so a 403 on the delete was
    reported as "could not check whether deployment 'blue' already exists" --
    over a GET that had just succeeded and found it in `Failed`. The operator was
    told the state was unknown; it was known, and it is the state Azure refuses
    to update."""
    with caplog.at_level(logging.WARNING, logger="ffsft.deploy.endpoint"):
        client = _deploy_with_predelete_error(
            monkeypatch,
            None,
            existing=_FailedDeployment(),
            delete_error=_forbidden("online deployments"),
        )

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert client.online_deployments.deleted == [("ffsft-online", "blue")]
    assert not any("could not check whether" in m for m in warnings), warnings
    assert any("could NOT be deleted" in m for m in warnings), warnings
    assert any("Failed" in m for m in warnings), warnings


def test_a_refused_delete_of_a_failed_deployment_does_not_stop_the_deploy(monkeypatch):
    """Same direction as every other read in this file: reported, never
    enforced. The deploy is expected to fail on Azure's side, and saying so is
    this function's job -- refusing to try is not."""
    client = _deploy_with_predelete_error(
        monkeypatch,
        None,
        existing=_FailedDeployment(),
        delete_error=_forbidden("online deployments"),
    )

    assert [d.name for d in client.online_deployments.created] == ["blue"]


def test_a_delete_that_succeeds_says_nothing_about_a_failed_check(monkeypatch, caplog):
    """The true negative: clearing the corpse is the normal path and must stay
    quiet apart from the line that says it is being cleared."""
    with caplog.at_level(logging.WARNING, logger="ffsft.deploy.endpoint"):
        client = _deploy_with_predelete_error(monkeypatch, None, existing=_FailedDeployment())

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert client.online_deployments.deleted == [("ffsft-online", "blue")]
    assert not any("could NOT be deleted" in m for m in warnings), warnings
    assert not any("could not check whether" in m for m in warnings), warnings
