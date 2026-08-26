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
    """`${{outputs.model_dir}}` resolves under either mode, because it is declared.

    The token is substituted for any declared output, mounted or not; what the
    mode changes is only whether the bytes stream out during the run or are
    copied up at the end.
    """
    assert "--output-dir ${{outputs.model_dir}}" in build_command(
        JobSpec(mount_outputs=True)
    )
    assert "--output-dir ${{outputs.model_dir}}" in build_command(
        JobSpec(mount_outputs=False)
    )


def test_outputs_are_not_mounted_by_default():
    """Mounting a `uri_folder` output is the single most likely way to lose a run.

    The mount is a FUSE session the *node* opens against `workspaceblobstore`,
    and it is not covered by the `AzureServices` trusted-service bypass that
    lets the Azure ML control plane through. On a workspace whose storage
    account has public network access disabled and no private endpoint it fails
    with `data-capability.AssetMountOutputSession.Exception`, and it fails in
    the lifecycler *before* the user command starts -- so a 27B run dies after
    paying for node allocation and the image pull, having trained nothing.
    Verified on green_kettle_w1zpbvd64q, which failed this way in about five
    minutes.

    The fix for that was to stop mounting, and the mistake was to stop
    *declaring*: the adapter went to `./outputs` on the assumption that the
    run-history artifact service uploads it. It does not, and two completed 27B
    runs left nothing but logs. `upload` mode declares the output without
    opening a session, which avoids the failure without losing the model.
    """
    assert JobSpec().mount_outputs is False
    assert JobSpec().output_mode == "upload"
    assert "--output-dir ${{outputs.model_dir}}" in build_command(JobSpec())


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
    """A registration always carries an image, so the fake must too.

    `ensure_environment` reuses an existing version only when its image matches
    `TRAIN_IMAGE`; a fake that omitted the field made every `submit` test fail on
    an AttributeError that no real client could produce.
    """

    def get(self, name, version):
        return type(
            "Env",
            (),
            {"name": name, "version": version, "image": aml_job.TRAIN_IMAGE},
        )()


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


# ---------------------------------------------------------------------------
# Chaining evaluation onto the training job.
#
# Evaluating in a *separate* job would mean the adapter has to travel
# training node -> workspaceblobstore -> eval node. That round trip is not
# hypothetically risky here, it is the exact path that fails: the node cannot
# open a FUSE session against the storage account (docs/JOURNAL.md 17), which
# is why `mount_outputs` defaults to False. Chaining keeps the adapter on the
# node's local disk, where it was just written.
# ---------------------------------------------------------------------------


def test_no_eval_step_unless_asked():
    cmd = aml_job.build_command(aml_job.JobSpec(model_key="qwen3.8-27b"))
    assert "ffsft.eval.run" not in cmd
    assert "&&" not in cmd


def test_eval_suite_appends_a_second_stage():
    cmd = aml_job.build_command(
        aml_job.JobSpec(model_key="qwen3.8-27b", eval_suite="ko_fast")
    )
    train, _, evaluate = cmd.partition("&&")
    assert "ffsft.train.qlora" in train
    assert "ffsft.eval.run" in evaluate
    assert "--suite ko_fast" in evaluate
    assert "--model qwen3.8-27b" in evaluate


def test_eval_reads_the_adapter_the_trainer_just_wrote():
    """The two stages must agree on the directory, or the eval scores the base."""
    job = aml_job.JobSpec(model_key="qwen3.8-27b", eval_suite="ko_fast")
    cmd = aml_job.build_command(job)
    train, _, evaluate = cmd.partition("&&")
    assert "--output-dir ${{outputs.model_dir}}" in train
    assert "--adapter ${{outputs.model_dir}}" in evaluate


def test_eval_limit_is_passed_through():
    cmd = aml_job.build_command(
        aml_job.JobSpec(model_key="qwen3.8-27b", eval_suite="ko_fast", eval_limit=25)
    )
    assert "--limit 25" in cmd.partition("&&")[2]


def test_eval_limit_alone_does_nothing():
    """A limit without a suite must not silently start a 27B evaluation."""
    cmd = aml_job.build_command(aml_job.JobSpec(model_key="qwen3.8-27b", eval_limit=25))
    assert "ffsft.eval.run" not in cmd


def test_preflight_never_chains_an_eval():
    cmd = aml_job.build_command(aml_job.JobSpec(preflight=True, eval_suite="ko_fast"))
    assert cmd == "python -m ffsft.train.preflight"


# ---------------------------------------------------------------------------
# The trained adapter has to survive the node.
#
# Two 27B runs completed here -- `heroic_fennel_085y2rwm3s` at train_loss
# 1.2638 and `olden_bean_302vkc7nbz` -- and neither left a model behind. The
# artifact store holds six files for each, all of them logs:
#
#     system_logs/...  (5)
#     user_logs/std_log.txt
#
# No `outputs/`. The adapter was written to the node's local disk and went away
# with the LowPriority node. That is why the workspace has zero registered
# models and why no endpoint ever had anything to serve.
#
# The cause was a comment, not a bug: `mount_outputs=False` was chosen to dodge
# a real FUSE-mount failure against a storage account with public network
# access disabled, and justified with "writing to `./outputs` instead is
# uploaded by the run-history artifact service". That claim was never measured.
# It is false for v2 command jobs, which capture logs and *declared* outputs
# and nothing else.
#
# `upload` mode is the way out: the output is an ordinary local directory for
# the duration of the run and is copied up at the end, so there is no mount
# session for the storage rules to refuse.
# ---------------------------------------------------------------------------


def test_training_never_writes_the_adapter_to_a_path_that_is_not_collected():
    """`./outputs` is local disk on a node that is about to be deleted."""
    cmd = build_command(JobSpec(model_key="qwen3.8-27b", mix="ko_smoke"))
    assert "--output-dir ./outputs" not in cmd


def test_the_adapter_goes_to_a_declared_output():
    cmd = build_command(JobSpec())
    assert "--output-dir ${{outputs.model_dir}}" in cmd


def test_upload_is_the_default_output_mode():
    """Mounting is what failed; uploading is what this workspace can do."""
    assert JobSpec().output_mode == "upload"


def test_outputs_are_declared_even_without_mounting():
    """The regression in one line: outputs used to be None unless mounted."""
    assert JobSpec().mount_outputs is False
    assert JobSpec().declared_outputs() == {"model_dir", "report"}


def test_mount_mode_is_still_reachable():
    """A workspace without the storage restriction should still be able to."""
    assert JobSpec(mount_outputs=True).output_mode == "rw_mount"


def test_eval_reads_the_adapter_back_from_the_same_place():
    """A path mismatch here scores the base model and calls it tuned."""
    cmd = build_command(JobSpec(eval_suite="ko_fast", eval_limit=25))
    assert "--adapter ${{outputs.model_dir}}" in cmd
    assert "--output-dir ${{outputs.model_dir}}/eval" in cmd


def test_preflight_declares_nothing():
    """The self-test produces no model, so it needs no output to lose."""
    assert build_command(JobSpec(preflight=True)) == "python -m ffsft.train.preflight"