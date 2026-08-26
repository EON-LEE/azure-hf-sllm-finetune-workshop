"""The deploy CLI must expose the one serving path that storage cannot block.

`deploy_online()` grew an `hf_model=` parameter so vLLM can pull weights straight
from the Hugging Face Hub, bypassing the network-isolated workspace storage
account documented in VERIFIED.md section 24.  The argument parser never caught
up: it forced `--model-uri`, so the only unblocked path was unreachable from the
command line.  These tests pin the contract.
"""

from __future__ import annotations

import pytest

from ffsft.deploy import endpoint as ep


def _parse(*argv):
    return ep.build_parser().parse_args(list(argv))


def test_build_parser_is_exposed():
    assert callable(ep.build_parser)


def test_online_accepts_hf_model_without_model_uri():
    args = _parse("deploy-online", "--hf-model", "Qwen/Qwen3.5-0.8B")
    assert args.hf_model == "Qwen/Qwen3.5-0.8B"
    assert args.model_uri is None


def test_online_still_accepts_model_uri_without_hf_model():
    args = _parse("deploy-online", "--model-uri", "azureml:qwen3-ko:1")
    assert args.model_uri == "azureml:qwen3-ko:1"
    assert args.hf_model is None


def test_online_exposes_gpu_memory_utilization():
    args = _parse("deploy-online", "--hf-model", "x", "--gpu-memory-utilization", "0.85")
    assert args.gpu_memory_utilization == pytest.approx(0.85)


def test_online_source_defaults_are_none_so_main_can_tell_them_apart():
    args = _parse("deploy-online")
    assert args.model_uri is None and args.hf_model is None


@pytest.mark.parametrize(
    "argv",
    [
        ("deploy-online",),
        ("deploy-online", "--hf-model", "a", "--model-uri", "azureml:b:1"),
    ],
)
def test_main_rejects_neither_or_both_sources(argv, monkeypatch, capsys):
    """Exactly one weight source. Neither is ambiguous; both is contradictory."""
    called = []
    monkeypatch.setattr(ep, "deploy_online", lambda *a, **k: called.append(k))
    with pytest.raises(SystemExit) as excinfo:
        ep.main(list(argv))
    assert excinfo.value.code != 0
    assert not called, "must not reach Azure with an ambiguous weight source"
    assert "exactly one" in capsys.readouterr().err.lower()


def test_main_forwards_hf_model_to_deploy_online(monkeypatch):
    seen = {}

    def fake(endpoint, model_uri, **kw):
        seen["endpoint"] = endpoint
        seen["model_uri"] = model_uri
        seen.update(kw)
        return "https://example/score"

    monkeypatch.setattr(ep, "deploy_online", fake)
    rc = ep.main(
        [
            "deploy-online",
            "--endpoint", "ffsft-a10",
            "--hf-model", "Qwen/Qwen3.5-0.8B",
            "--sku", "Standard_NV12ads_A10_v5",
            "--gpu-memory-utilization", "0.85",
        ]
    )
    assert rc == 0
    assert seen["endpoint"] == "ffsft-a10"
    assert seen["model_uri"] is None
    assert seen["hf_model"] == "Qwen/Qwen3.5-0.8B"
    assert seen["sku"] == "Standard_NV12ads_A10_v5"
    assert seen["gpu_memory_utilization"] == pytest.approx(0.85)


def test_main_forwards_model_uri_and_leaves_hf_model_unset(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        ep, "deploy_online", lambda endpoint, model_uri, **kw: seen.update(
            endpoint=endpoint, model_uri=model_uri, **kw
        )
    )
    assert ep.main(["deploy-online", "--model-uri", "azureml:qwen3-ko:1"]) == 0
    assert seen["model_uri"] == "azureml:qwen3-ko:1"
    assert seen["hf_model"] is None


# --- blue/green -----------------------------------------------------------
#
# `deploy_online` hardcoded the deployment name "blue" and unconditionally set
# `traffic = {"blue": 100}`.  That makes every redeploy an in-place overwrite of
# the deployment currently serving traffic: a rollout that fails to start takes
# the endpoint down with it, and the only way back is another 20-minute deploy.
# The endpoint resource has always supported several named deployments; only the
# caller could not name them.


def test_deployment_name_defaults_to_blue_so_existing_callers_are_unchanged():
    args = _parse("deploy-online", "--hf-model", "x")
    assert args.deployment == "blue"
    assert args.traffic == 100


def test_a_second_deployment_can_be_named_and_kept_off_traffic():
    args = _parse(
        "deploy-online", "--hf-model", "x", "--deployment", "green", "--traffic", "0"
    )
    assert args.deployment == "green"
    assert args.traffic == 0


def test_main_forwards_the_deployment_name_and_traffic(monkeypatch):
    """A flag the parser accepts but main drops is worse than no flag at all:
    the rollout silently overwrites blue while reporting success."""
    seen = {}
    monkeypatch.setattr(ep, "deploy_online", lambda *a, **k: seen.update(k))
    rc = ep.main(
        ["deploy-online", "--hf-model", "x", "--deployment", "green", "--traffic", "0"]
    )
    assert rc == 0
    assert seen["deployment_name"] == "green"
    assert seen["traffic_percent"] == 0
