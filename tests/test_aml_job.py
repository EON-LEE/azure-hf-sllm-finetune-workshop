"""Tests for the Azure ML training-job builder.

`aml_job.py` is the module that spends money. Everything it gets wrong is paid
for in GPU-minutes: a malformed command line is only discovered after a node is
allocated and a multi-gigabyte image is pulled, which on a low-priority A100
means roughly ten minutes and a real charge before the first byte of feedback.

So these tests exercise the two things that can be checked for free -- the
command string and the client-side refusals -- and never touch Azure. The SDK is
imported lazily inside `submit`, so the fakes are injected by monkeypatching the
module attributes the function reaches for.
"""

from __future__ import annotations

import pytest

from ffsft.azure_ml import AzureTarget
from ffsft.train import aml_job
from ffsft.train.aml_job import JobSpec, build_command


@pytest.fixture
def target():
    return AzureTarget(
        subscription_id="sub", resource_group="rg", workspace_name="ws"
    )


# --------------------------------------------------------------------------
# build_command
# --------------------------------------------------------------------------


def test_preflight_runs_the_self_test_and_nothing_else():
    """Preflight must not inherit training flags.

    `python -m ffsft.train.preflight` takes no arguments; appending --model or
    --mix to it makes argparse exit 2 on the node, which reads as a crashed job
    rather than a bad command.
    """
    assert build_command(JobSpec(preflight=True)) == "python -m ffsft.train.preflight"


def test_training_command_carries_every_tunable_the_spec_declares():
    cmd = build_command(
        JobSpec(
            model_key="qwen3.5-4b",
            mix="ko_broad",
            rank=32,
            max_seq_length=2048,
            batch_size=2,
            grad_accum=8,
        )
    )
    assert cmd.startswith("python -m ffsft.train.qlora")
    assert "--model qwen3.5-4b" in cmd
    assert "--mix ko_broad" in cmd
    assert "--rank 32" in cmd
    assert "--max-seq-length 2048" in cmd
    assert "--batch-size 2" in cmd
    assert "--grad-accum 8" in cmd


def test_unset_step_and_sample_caps_are_omitted_not_passed_as_sentinels():
    """-1 means "no cap" internally; passing it through would cap at -1 steps."""
    cmd = build_command(JobSpec(max_steps=-1, max_samples=None))
    assert "--max-steps" not in cmd
    assert "--max-samples" not in cmd


def test_output_dir_follows_whether_the_output_is_mounted():
    """`${{outputs.model_dir}}` only resolves when the output actually exists.

    With no mounted output Azure ML leaves the literal token in the command, and
    the adapter is written to a directory named `${{outputs.model_dir}}`.
    """
    assert "--output-dir ${{outputs.model_dir}}" in build_command(
        JobSpec(mount_outputs=True)
    )
    assert "--output-dir ./outputs" in build_command(JobSpec(mount_outputs=False))


def test_default_lora_targets_opt_in_reaches_the_node():
    """The registry declares LoRA targets for 2 of 16 models.

    For every other model `qlora.py` refuses to guess and raises, so without a
    way to pass the opt-in through the job spec the AML path can only ever train
    the two models that declare targets -- which contradicts the whole point of
    a swappable registry.
    """
    assert "--allow-default-lora-targets" in build_command(
        JobSpec(allow_default_lora_targets=True)
    )
    assert "--allow-default-lora-targets" not in build_command(JobSpec())


# --------------------------------------------------------------------------
# submit: refusals that must happen before a node is allocated
# --------------------------------------------------------------------------


class _FakeJobs:
    def __init__(self):
        self.created = []

    def create_or_update(self, node):
        self.created.append(node)
        return type(
            "Submitted", (), {"name": "fake_job", "status": "Starting", "studio_url": "u"}
        )()


class _FakeEnvironments:
    def get(self, name, version):
        return type("Env", (), {"name": name, "version": version})()


class _FakeClient:
    def __init__(self):
        self.jobs = _FakeJobs()
        self.environments = _FakeEnvironments()


@pytest.fixture
def fake_client(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(aml_job, "get_ml_client", lambda target: client)
    return client


def test_submit_refuses_a_model_that_does_not_fit_the_sku(target, fake_client):
    too_big = AzureTarget(
        subscription_id="sub",
        resource_group="rg",
        workspace_name="ws",
        compute_sku="Standard_NC4as_T4_v3",
    )
    with pytest.raises(ValueError, match="refusing to submit"):
        aml_job.submit(too_big, JobSpec(model_key="qwen3.8-27b"))
    assert fake_client.jobs.created == []


def test_submit_refuses_a_model_with_no_lora_targets_before_paying_for_a_node(
    target, fake_client
):
    """Catch the guess-refusal locally instead of on a running GPU.

    `resolve_target_modules` already raises for a model with no declared
    targets, but it raises *inside the container*, which costs a node
    allocation, an image pull and a model download first. The same fact is
    knowable from the registry at submit time, for free.
    """
    with pytest.raises(ValueError, match="lora_target_modules"):
        aml_job.submit(target, JobSpec(model_key="qwen3.5-4b"))
    assert fake_client.jobs.created == []


def test_submit_accepts_the_same_model_once_the_opt_in_is_explicit(
    target, fake_client
):
    aml_job.submit(
        target, JobSpec(model_key="qwen3.5-4b", allow_default_lora_targets=True)
    )
    assert len(fake_client.jobs.created) == 1


def test_submit_skips_both_guards_for_preflight(target, fake_client):
    """Preflight loads a 0.6B model of its own and ignores the registry."""
    aml_job.submit(target, JobSpec(model_key="qwen3.5-4b", preflight=True))
    assert len(fake_client.jobs.created) == 1


def test_submit_passes_the_declared_target_model_through_unblocked(
    target, fake_client
):
    info = aml_job.submit(target, JobSpec(model_key="qwen3.8-27b"))
    assert info["priority"] == "LowPriority"
    assert info["image"] == aml_job.TRAIN_IMAGE
    assert len(fake_client.jobs.created) == 1
