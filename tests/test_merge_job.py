"""Tests for the Azure ML merge-job builder.

Same bargain as `test_aml_job.py`: this module spends GPU-minutes, and a bad
command line is only discovered after a node is allocated and a nine-gigabyte
image is pulled. So the command string and the client-side refusals are checked
here, where it is free, and Azure is never touched -- the SDK is imported lazily
inside `submit`, so fakes go in by monkeypatching the module attribute the
function reaches for.
"""

from __future__ import annotations

import pytest

from ffsft.azure_ml import AzureTarget
from ffsft.deploy import merge_job
from ffsft.deploy.merge_job import MergeSpec, build_command, split_asset_ref


@pytest.fixture
def target():
    return AzureTarget(subscription_id="sub", resource_group="rg", workspace_name="ws")


# --------------------------------------------------------------------------
# build_command
# --------------------------------------------------------------------------


def test_command_binds_the_adapter_input_and_the_declared_output():
    """The node must read from the mounted asset and write to the declared output.

    Writing anywhere else produces a merge that completes and then evaporates
    with the node, because a v2 command job collects only declared outputs.
    """
    cmd = build_command(MergeSpec(model_key="qwen3.8-27b", adapter="a:1"))
    assert "--adapter ${{inputs.adapter}}" in cmd
    assert "--output ${{outputs.merged}}" in cmd
    assert cmd.startswith("python -m ffsft.deploy.merge ")


def test_command_states_every_flag_rather_than_relying_on_script_defaults():
    """The submitter's defaults, not the script's, decide what runs.

    `merge.py` defaults are reasonable for a laptop; the job's are chosen for an
    A100. Leaving a flag off would silently hand the decision to whichever set
    happens to change first.
    """
    cmd = build_command(MergeSpec(adapter="a:1", dtype="float16", device_map="cpu"))
    assert "--dtype float16" in cmd
    assert "--device-map cpu" in cmd
    assert "--max-shard-size 4GB" in cmd


def test_merged_is_the_only_declared_output():
    assert MergeSpec(adapter="a:1").declared_outputs() == {"merged"}


# --------------------------------------------------------------------------
# split_asset_ref
# --------------------------------------------------------------------------


def test_asset_reference_must_pin_a_version():
    """A bare name resolves to 'latest', which is not a fixed thing."""
    with pytest.raises(ValueError, match="name:version"):
        split_asset_ref("qwen3_8-27b-ko-lora")


def test_asset_reference_splits_on_the_last_colon():
    assert split_asset_ref("qwen3_8-27b-ko-lora:3") == ("qwen3_8-27b-ko-lora", "3")


# --------------------------------------------------------------------------
# submit -- refusals that are free here and expensive on a node
# --------------------------------------------------------------------------


class _FakeAsset:
    def __init__(self, tags):
        self.tags = tags


class _FakeModels:
    def __init__(self, tags):
        self._tags = tags

    def get(self, name, version=None):
        return _FakeAsset(self._tags)


class _FakeClient:
    def __init__(self, tags):
        self.models = _FakeModels(tags)


def _stub_client(monkeypatch, tags):
    monkeypatch.setattr(merge_job, "get_ml_client", lambda _t: _FakeClient(tags))


def test_submit_refuses_an_empty_adapter(target):
    with pytest.raises(ValueError, match="nothing to merge"):
        merge_job.submit(target, MergeSpec(adapter=""))


def test_submit_refuses_an_unknown_model_key(target):
    """The registry lookup happens before any client is built, so this is free."""
    with pytest.raises(KeyError):
        merge_job.submit(target, MergeSpec(model_key="not-a-model", adapter="a:1"))


def test_submit_refuses_an_adapter_tagged_for_a_different_base(target, monkeypatch):
    """The failure this prevents is silent.

    PEFT applies deltas to whatever module names collide, the save succeeds, and
    the model serves fluent nonsense. Nothing downstream reports an error.
    """
    _stub_client(monkeypatch, {"model_key": "kanana2-1.3b"})
    with pytest.raises(ValueError, match="wrong base"):
        merge_job.submit(target, MergeSpec(model_key="qwen3.8-27b", adapter="a:1"))


def test_submit_accepts_an_untagged_adapter(target, monkeypatch):
    """Assets registered before the tag existed are legitimate.

    The check must fail closed on a *mismatch* and open on an absence, or it is
    easier to bypass than to satisfy.
    """
    _stub_client(monkeypatch, {})
    # Gets past _check_adapter_matches and dies later, reaching for an
    # `environments` collection the fake client deliberately does not have.
    with pytest.raises(AttributeError):
        merge_job.submit(target, MergeSpec(model_key="qwen3.8-27b", adapter="a:1"))
