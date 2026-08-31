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

import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

from ffsft.deploy import endpoint, probes

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


# ---------------------------------------------------------------------------
# The probe is a destructive caller, and it was never audited as one.
#
# `probe_sku` reaches `begin_create_or_update` -- create_or_UPDATE, an upsert --
# with a name the caller chose (`endpoint.cmd_check` passes `ffsft-probe-{index}`)
# and then deletes that name. Run against a workspace that already owned a
# cluster called `ffsft-probe-0`, an executed audit printed:
#
#     created : [('ffsft-probe-0', 'Standard_NV36ads_A10_v5', 0)]
#     DELETED : ['ffsft-probe-0']
#
# -- somebody's cluster silently re-sized to the probe's settings and then
# removed, from a command whose `--help` calls itself free. The tests below
# pin the three separate holes: no pre-existence check, no `.result()` on the
# delete poller (all four delete sites in `deploy/lifecycle.py` await theirs),
# and a failed delete swallowed into a `log.warning` that `check` never prints.
# ---------------------------------------------------------------------------

PROBE_NAME = "ffsft-probe-0"
SKU = "Standard_NV36ads_A10_v5"


class _Poller:
    """The long-running-operation handle both compute calls return.

    `result()` is the load-bearing part: it is what "the operation finished"
    means, and it is where a delete that fails server-side actually raises. A
    caller that never calls it cannot tell a completed delete from one still in
    flight, so the fake records every call to it.
    """

    def __init__(self, on_result=None, raises=None):
        self._on_result = on_result
        self._raises = raises

    def result(self):
        if self._on_result is not None:
            self._on_result()
        if self._raises is not None:
            raise self._raises
        return None


class _FakeCompute:
    """A workspace's compute collection, with a memory of what was done to it."""

    def __init__(self, existing=(), create_raises=None, delete_raises=None, get_raises=None):
        self.existing = {name: "pre-existing cluster" for name in existing}
        self.create_raises = create_raises
        self.delete_raises = delete_raises
        self.get_raises = get_raises
        self.created = []
        self.delete_calls = []
        self.awaited = []

    def get(self, name):
        if self.get_raises is not None:
            raise self.get_raises
        if name in self.existing:
            return self.existing[name]
        raise ResourceNotFoundError(f"compute {name} not found")

    def begin_create_or_update(self, compute):
        self.created.append((compute.name, compute.size, compute.min_instances))
        self.existing[compute.name] = compute
        return _Poller(raises=self.create_raises)

    def begin_delete(self, name):
        self.delete_calls.append(name)
        outcome = self.delete_raises

        def _awaited():
            self.awaited.append(name)
            if outcome is None:
                self.existing.pop(name, None)

        return _Poller(on_result=_awaited, raises=outcome)


class _FakeClient:
    def __init__(self, **kwargs):
        self.compute = _FakeCompute(**kwargs)


def test_the_probe_does_not_overwrite_a_cluster_that_already_owns_the_probe_name():
    client = _FakeClient(existing=[PROBE_NAME])
    probes.probe_sku(client, SKU, "LowPriority", name=PROBE_NAME)
    assert client.compute.created == [], "the probe upserted over a cluster it did not create"
    assert client.compute.existing[PROBE_NAME] == "pre-existing cluster"


def test_the_probe_does_not_delete_a_cluster_it_did_not_create():
    client = _FakeClient(existing=[PROBE_NAME])
    probes.probe_sku(client, SKU, "LowPriority", name=PROBE_NAME)
    assert client.compute.delete_calls == [], "the probe deleted somebody else's cluster"


def test_a_taken_probe_name_reaches_the_operator_rather_than_only_the_log():
    probe = probes.probe_sku(_FakeClient(existing=[PROBE_NAME]), SKU, "LowPriority",
                             name=PROBE_NAME)
    assert probe.blocker is not None
    assert PROBE_NAME in probe.blocker


def test_a_taken_probe_name_is_not_reported_as_the_sku_being_uncreatable():
    """Nothing was asked of the control plane, so nothing may be claimed about the SKU."""
    probe = probes.probe_sku(_FakeClient(existing=[PROBE_NAME]), SKU, "LowPriority",
                             name=PROBE_NAME)
    assert probe.probed is False
    assert "NOT tested" in probe.blocker


def test_a_probe_name_that_could_not_be_read_is_not_treated_as_a_free_name():
    """Could-not-look is not looked-and-saw-nothing, and here the write is destructive."""
    client = _FakeClient(get_raises=HttpResponseError("403 forbidden"))
    probe = probes.probe_sku(client, SKU, "LowPriority", name=PROBE_NAME)
    assert client.compute.created == []
    assert client.compute.delete_calls == []
    assert probe.probed is False
    assert "HttpResponseError" in probe.blocker


def test_the_probe_deletes_the_cluster_it_did_create():
    client = _FakeClient()
    probe = probes.probe_sku(client, SKU, "LowPriority", name=PROBE_NAME)
    assert probe.creatable is True
    assert client.compute.created == [(PROBE_NAME, SKU, 0)]
    assert client.compute.delete_calls == [PROBE_NAME]


def test_the_probe_delete_is_awaited_rather_than_left_in_flight():
    """Every delete in deploy/lifecycle.py calls .result(); this one returned early."""
    client = _FakeClient()
    probes.probe_sku(client, SKU, "LowPriority", name=PROBE_NAME)
    assert client.compute.awaited == [PROBE_NAME], "the delete poller was never awaited"


def test_a_delete_that_failed_is_surfaced_and_not_swallowed():
    client = _FakeClient(delete_raises=HttpResponseError("409 conflict"))
    probe = probes.probe_sku(client, SKU, "LowPriority", name=PROBE_NAME)
    assert probe.blocker is not None, "a failed delete reached only the log"
    assert PROBE_NAME in probe.blocker
    assert "HttpResponseError" in probe.blocker


def test_a_delete_that_failed_does_not_retract_the_measured_create():
    """The create really did succeed; only the cleanup is unknown."""
    probe = probes.probe_sku(_FakeClient(delete_raises=HttpResponseError("409 conflict")),
                             SKU, "LowPriority", name=PROBE_NAME)
    assert probe.creatable is True
    assert probe.probed is True


def test_a_refused_create_still_has_its_failed_compute_record_deleted():
    """A refusal leaves a compute in `Failed`; it bills nothing but it accumulates."""
    client = _FakeClient(create_raises=Exception(QUOTA_MSG))
    probe = probes.probe_sku(client, SKU, "Dedicated", name=PROBE_NAME)
    assert probe.creatable is False
    assert "ClusterMinNodesExceedCoreQuota" in probe.blocker
    assert client.compute.delete_calls == [PROBE_NAME]
    assert client.compute.awaited == [PROBE_NAME]


def test_a_refusal_that_left_no_compute_record_is_not_reported_as_a_leftover():
    """`ResourceNotFoundError` on the cleanup means the desired end state, not a leak."""
    client = _FakeClient(create_raises=Exception(SKU_MSG),
                         delete_raises=ResourceNotFoundError("no such compute"))
    probe = probes.probe_sku(client, SKU, "Dedicated", name=PROBE_NAME)
    assert probe.leftover == ""
    assert "still there" not in probe.blocker


# ---------------------------------------------------------------------------
# Ctrl-C is not an `Exception` (§75)
# ---------------------------------------------------------------------------


def test_ctrl_c_during_the_create_still_removes_the_cluster_it_had_already_claimed():
    """`.result()` is a ~30s poll against a name the PUT has already written, and
    `except Exception` does not catch what Ctrl-C raises. The unwind went
    straight past the discard, leaving a real AmlCompute in the workspace from a
    command whose own help says an acceptance is deleted."""
    client = _FakeClient(create_raises=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        probes.probe_sku(client, SKU, "LowPriority", name=PROBE_NAME)
    assert client.compute.delete_calls == [PROBE_NAME]
    assert client.compute.awaited == [PROBE_NAME], "the delete poller was never awaited"
    assert PROBE_NAME not in client.compute.existing


def test_ctrl_c_is_re_raised_rather_than_turned_into_a_probe_result():
    """Cleaning up must not mean the interrupt did nothing: swallowing it would
    let the loop in `cmd_check` walk on to the next pattern and create the next
    cluster, which is the opposite of what the operator just asked for."""
    with pytest.raises(KeyboardInterrupt):
        probes.probe_sku(_FakeClient(create_raises=KeyboardInterrupt()), SKU, "Dedicated",
                         name=PROBE_NAME)


def test_a_ctrl_c_whose_cleanup_also_failed_names_the_cluster_it_is_leaving(capsys):
    """The one path where the operator has to be told a name: nothing returns a
    `SkuProbe` here, so the leftover has nowhere else to go."""
    client = _FakeClient(create_raises=KeyboardInterrupt(),
                         delete_raises=HttpResponseError("409 conflict"))
    with pytest.raises(KeyboardInterrupt):
        probes.probe_sku(client, SKU, "Dedicated", name=PROBE_NAME)
    out = capsys.readouterr().out
    assert PROBE_NAME in out
    assert "HttpResponseError" in out


# ---------------------------------------------------------------------------
# three outcomes, three words (§75)
# ---------------------------------------------------------------------------


def _rendered(probe):
    lines, unread = probes.probe_report(probe, "aks_vllm", 15)
    return "\n".join(lines), unread


def test_a_probe_that_never_asked_is_rendered_unknown_rather_than_blocked():
    """Executed against a workspace already owning `ffsft-probe-0`, the row read
    `aks_vllm BLOCKED ProbeNameTaken. ...` -- a cell claiming a verdict over a
    sentence that says the SKU was never tested."""
    out, _ = _rendered(probes.probe_sku(_FakeClient(existing=[PROBE_NAME]), SKU, "LowPriority",
                                        name=PROBE_NAME))
    assert "UNKNOWN" in out
    assert "BLOCKED" not in out
    assert "was not tested" in out
    assert "NOT tested" in out


def test_a_probe_that_never_asked_is_added_to_the_could_not_look_list():
    """The half that decides the exit code: a blocker is an answer and is not
    collected, so rendering the refusal as one made `check --probe && echo ok`
    print ok over a SKU nobody asked about."""
    _, unread = _rendered(probes.probe_sku(_FakeClient(existing=[PROBE_NAME]), SKU, "LowPriority",
                                           name=PROBE_NAME))
    assert unread is not None
    assert SKU in unread


def test_a_probe_the_control_plane_refused_is_still_blocked_and_still_an_answer():
    """The over-correction guard. A create that came back with
    ClusterMinNodesExceedCoreQuota was measured; it must not join the unread
    list, or `check` exits non-zero on the ordinary subscription this repo is
    written for, where dedicated GPU quota is 0."""
    out, unread = _rendered(probes.probe_sku(_FakeClient(create_raises=Exception(QUOTA_MSG)),
                                             SKU, "Dedicated", name=PROBE_NAME))
    assert "BLOCKED" in out
    assert unread is None


def test_a_probe_that_was_accepted_and_cleaned_up_is_one_ok_row_and_nothing_unread():
    out, unread = _rendered(probes.probe_sku(_FakeClient(), SKU, "LowPriority", name=PROBE_NAME))
    assert "create accepted" in out
    assert unread is None
    assert len(out.splitlines()) == 1


def test_a_probe_whose_cleanup_failed_is_ok_with_a_leftover_and_not_blocked():
    """The control plane accepted this SKU a second earlier. Printing BLOCKED
    over it sends the operator to choose a different SKU when the thing to do is
    delete a cluster."""
    out, unread = _rendered(probes.probe_sku(_FakeClient(delete_raises=HttpResponseError("409")),
                                             SKU, "LowPriority", name=PROBE_NAME))
    assert "create accepted" in out
    assert "BLOCKED" not in out
    assert "LEFTOVER" in out
    assert PROBE_NAME in out
    # A delete that failed is not a resource anyone confirmed is gone.
    assert unread is not None


def test_a_probe_paragraph_is_wrapped_under_its_row_instead_of_pasted_into_the_cell():
    """Every other row in `cmd_check` runs its message through `_summary`; this
    one did not, and a 400-character detail reflowed the column."""
    probe = probes.probe_sku(_FakeClient(existing=[PROBE_NAME]), SKU, "LowPriority",
                             name=PROBE_NAME)
    out, _ = _rendered(probe)
    lines = out.splitlines()
    # If this stops being a paragraph, the test stops measuring anything.
    assert len(probe.detail) > 300
    assert len(lines) > 1
    # 110 is `_summary`'s bound; the row prefix is what the rest of the table
    # already spends on top of it.
    assert all(len(line) <= 140 for line in lines), lines
    assert probe.detail not in lines[0]
    assert PROBE_NAME in out
