"""Tests for the serving lifecycle: cost classification and teardown planning.

The teardown logic is the highest-consequence code in the repo -- a bug that
misclassifies a running A10 endpoint as idle costs real money silently. These
tests use fake ML clients rather than mocks of network calls, matching the
convention in tests/test_main.py.
"""

from __future__ import annotations

import pytest

from ffsft.deploy.lifecycle import (
    HOURS_PER_MONTH,
    BillingItem,
    Inventory,
    collect_inventory,
    format_inventory,
    hourly_rate,
    teardown,
)


class FakeDeployment:
    def __init__(self, name, instance_type, instance_count):
        self.name = name
        self.instance_type = instance_type
        self.instance_count = instance_count


class FakeEndpoint:
    def __init__(self, name):
        self.name = name


class FakeCompute:
    def __init__(self, name, size, min_instances, tier="lowpriority", type="amlcompute"):
        self.name = name
        self.size = size
        self.min_instances = min_instances
        self.tier = tier
        self.type = type


class FakeJob:
    def __init__(self, name, status):
        self.name = name
        self.status = status


class FakeOnlineEndpoints:
    def __init__(self, endpoints):
        self._endpoints = endpoints
        self.deleted = []

    def list(self):
        return list(self._endpoints)

    def begin_delete(self, name):
        self.deleted.append(name)
        return _Poller()


class FakeOnlineDeployments:
    def __init__(self, mapping):
        self._mapping = mapping

    def list(self, endpoint_name):
        return list(self._mapping.get(endpoint_name, []))


class FakeComputeOps:
    def __init__(self, computes):
        self._computes = {c.name: c for c in computes}
        self.updated = []

    def list(self):
        return list(self._computes.values())

    def get(self, name):
        return self._computes[name]

    def begin_create_or_update(self, compute):
        self.updated.append((compute.name, compute.min_instances))
        return _Poller()


class FakeJobs:
    def __init__(self, jobs):
        self._jobs = jobs

    def list(self, max_results=50):
        return list(self._jobs)


class FakeBatchEndpoints:
    def __init__(self, endpoints):
        self._endpoints = endpoints

    def list(self):
        return list(self._endpoints)


class _Poller:
    def result(self):
        return None


class FakeMLClient:
    def __init__(self, *, online=(), deployments=None, computes=(), jobs=(), batch=()):
        self.online_endpoints = FakeOnlineEndpoints(online)
        self.online_deployments = FakeOnlineDeployments(deployments or {})
        self.compute = FakeComputeOps(computes)
        self.jobs = FakeJobs(jobs)
        self.batch_endpoints = FakeBatchEndpoints(batch)


def test_hourly_rate_known_sku():
    assert hourly_rate("Standard_NV36ads_A10_v5") == pytest.approx(4.320)


def test_hourly_rate_unknown_sku_is_zero_not_error():
    # An unknown SKU must not crash the cost report; it degrades to 0 and the
    # resource is still listed so a human can spot it.
    assert hourly_rate("Standard_Something_New") == 0.0


def test_billing_item_idle_resource_costs_nothing():
    item = BillingItem(
        kind="compute-cluster",
        name="gpu",
        detail="min_instances=0",
        sku="Standard_NC24ads_A100_v4",
    )
    assert item.hourly == 0.0
    assert item.monthly == 0.0


def test_billing_item_online_deployment_multiplies_by_instances():
    item = BillingItem(
        kind="online-deployment",
        name="ep/blue",
        detail="",
        sku="Standard_NV36ads_A10_v5",
        instances=2,
        bills_when_idle=True,
    )
    assert item.hourly == pytest.approx(8.640)
    assert item.monthly == pytest.approx(8.640 * HOURS_PER_MONTH)


def test_collect_inventory_flags_online_deployment():
    client = FakeMLClient(
        online=[FakeEndpoint("ffsft-qwen")],
        deployments={
            "ffsft-qwen": [FakeDeployment("blue", "Standard_NV36ads_A10_v5", 1)]
        },
    )
    inv = collect_inventory(client)
    billing = inv.billing
    assert len(billing) == 1
    assert billing[0].kind == "online-deployment"
    assert billing[0].name == "ffsft-qwen/blue"
    assert inv.hourly == pytest.approx(4.320)


def test_collect_inventory_endpoint_without_deployment_is_not_billing():
    # An endpoint shell with no deployment has no compute behind it. Reporting
    # it as billing would cause needless panic; hiding it entirely would leave
    # an orphan nobody deletes. It must appear, unflagged.
    client = FakeMLClient(online=[FakeEndpoint("empty-ep")], deployments={})
    inv = collect_inventory(client)
    assert inv.billing == []
    assert any(i.name == "empty-ep" for i in inv.items)


def test_collect_inventory_scale_to_zero_cluster_is_not_billing():
    client = FakeMLClient(
        computes=[FakeCompute("gpu-a100", "Standard_NC24ads_A100_v4", min_instances=0)]
    )
    inv = collect_inventory(client)
    assert inv.billing == []
    assert inv.hourly == 0.0


def test_collect_inventory_always_on_cluster_is_billing():
    client = FakeMLClient(
        computes=[FakeCompute("gpu-hot", "Standard_NC24ads_A100_v4", min_instances=2)]
    )
    inv = collect_inventory(client)
    assert len(inv.billing) == 1
    assert inv.hourly == pytest.approx(4.959 * 2)


def test_collect_inventory_ignores_non_amlcompute():
    client = FakeMLClient(
        computes=[
            FakeCompute("ci", "Standard_DS3_v2", min_instances=1, type="computeinstance")
        ]
    )
    inv = collect_inventory(client)
    assert inv.items == []


def test_collect_inventory_batch_endpoint_never_bills_idle():
    client = FakeMLClient(batch=[FakeEndpoint("ffsft-batch")])
    inv = collect_inventory(client)
    assert inv.billing == []
    assert any(i.kind == "batch-endpoint" for i in inv.items)


def test_collect_inventory_reports_running_jobs():
    client = FakeMLClient(jobs=[FakeJob("job1", "Running"), FakeJob("job2", "Completed")])
    inv = collect_inventory(client)
    names = [i.name for i in inv.items if i.kind == "job"]
    assert names == ["job1"]


def test_collect_inventory_survives_a_failing_api():
    class Broken(FakeMLClient):
        def __init__(self):
            super().__init__(computes=[FakeCompute("gpu", "Standard_NC24ads_A100_v4", 3)])

            class Boom:
                def list(self):
                    raise RuntimeError("403 forbidden")

            self.online_endpoints = Boom()

    inv = collect_inventory(Broken())
    # The compute section must still be reported even though endpoints failed.
    assert len(inv.billing) == 1
    assert inv.billing[0].name == "gpu"


def test_teardown_dry_run_deletes_nothing():
    client = FakeMLClient(
        online=[FakeEndpoint("ffsft-qwen")],
        deployments={
            "ffsft-qwen": [FakeDeployment("blue", "Standard_NV36ads_A10_v5", 1)]
        },
    )
    inv = collect_inventory(client)
    planned = teardown(client, inv, dry_run=True)
    assert planned
    assert client.online_endpoints.deleted == []


def test_teardown_deletes_endpoint_once_for_multiple_deployments():
    client = FakeMLClient(
        online=[FakeEndpoint("ffsft-qwen")],
        deployments={
            "ffsft-qwen": [
                FakeDeployment("blue", "Standard_NV36ads_A10_v5", 1),
                FakeDeployment("green", "Standard_NV36ads_A10_v5", 1),
            ]
        },
    )
    inv = collect_inventory(client)
    done = teardown(client, inv, dry_run=False)
    assert client.online_endpoints.deleted == ["ffsft-qwen"]
    assert len(done) == 1


def test_teardown_scales_cluster_instead_of_deleting_it():
    # Deleting a cluster throws away a definition that costs nothing to keep and
    # minutes to recreate. Teardown must scale it to zero, not remove it.
    client = FakeMLClient(
        computes=[FakeCompute("gpu-hot", "Standard_NC24ads_A100_v4", min_instances=2)]
    )
    inv = collect_inventory(client)
    teardown(client, inv, dry_run=False)
    assert client.compute.updated == [("gpu-hot", 0)]


def test_format_inventory_states_clean_when_nothing_bills():
    inv = Inventory(items=[])
    out = format_inventory(inv)
    assert "nothing" in out.lower()


def test_format_inventory_shows_monthly_projection():
    client = FakeMLClient(
        online=[FakeEndpoint("ep")],
        deployments={"ep": [FakeDeployment("blue", "Standard_NV36ads_A10_v5", 1)]},
    )
    out = format_inventory(collect_inventory(client))
    assert "BILLING NOW" in out
    assert "/month" in out
    assert "3,154" in out or "3,153" in out


# --- `up` flag passthrough --------------------------------------------------
#
# The serve image has always read GPU_MEMORY_UTILIZATION and EXTRA_ARGS, but the
# `up` parser did not offer either, so neither was reachable without rebuilding
# the image. On a model that only just fits its card these are the two knobs
# that decide whether the rollout comes up at all -- what is left after the
# weights has to cover the KV cache, the hybrid state cache and CUDA graph
# capture, and vLLM exits rather than shrinking to fit. Pin the wiring.


def _run_up(monkeypatch, argv):
    """Drive `ffsft-lifecycle up` and return the kwargs deploy_online saw."""
    from ffsft.deploy import endpoint, lifecycle

    seen = {}

    def fake_deploy_online(endpoint_name, model_uri, **kwargs):
        seen["endpoint_name"] = endpoint_name
        seen["model_uri"] = model_uri
        seen.update(kwargs)
        return "https://example.invalid/score"

    monkeypatch.setattr(endpoint, "deploy_online", fake_deploy_online)
    monkeypatch.setattr("sys.argv", ["ffsft-lifecycle", "up", *argv])
    assert lifecycle.main() == 0
    return seen


def test_up_defaults_gpu_memory_utilization_to_the_deploy_default(monkeypatch):
    seen = _run_up(monkeypatch, ["--endpoint", "e", "--hf-model", "org/repo"])
    assert seen["gpu_memory_utilization"] == 0.90
    assert seen["extra_args"] == ""


def test_up_forwards_gpu_memory_utilization(monkeypatch):
    seen = _run_up(
        monkeypatch,
        ["--endpoint", "e", "--hf-model", "org/repo", "--gpu-memory-utilization", "0.82"],
    )
    assert seen["gpu_memory_utilization"] == 0.82


def test_up_forwards_extra_args_verbatim(monkeypatch):
    """The value must be attached with `=`.

    Every useful value here starts with a dash, and argparse reads a
    dash-leading token as the next option rather than as this one's value:
    `--extra-args --enforce-eager` exits 2 with "expected one argument". The
    attached form is the only spelling that works, so it is the one pinned.
    """
    seen = _run_up(
        monkeypatch,
        ["--endpoint", "e", "--hf-model", "org/repo", "--extra-args=--enforce-eager"],
    )
    assert seen["extra_args"] == "--enforce-eager"


def test_up_forwards_several_extra_args_as_one_string(monkeypatch):
    seen = _run_up(
        monkeypatch,
        [
            "--endpoint", "e",
            "--hf-model", "org/repo",
            "--extra-args=--enforce-eager --max-num-seqs 8",
        ],
    )
    assert seen["extra_args"] == "--enforce-eager --max-num-seqs 8"


def test_up_forwards_quantization_and_max_model_len(monkeypatch):
    seen = _run_up(
        monkeypatch,
        [
            "--endpoint", "e",
            "--hf-model", "org/repo",
            "--quantization", "bitsandbytes",
            "--max-model-len", "2048",
        ],
    )
    assert seen["quantization"] == "bitsandbytes"
    assert seen["max_model_len"] == 2048


def test_up_prefers_a_registered_model_over_the_specs_hub_id(monkeypatch):
    """A merged asset must not be silently replaced by the untuned base."""
    seen = _run_up(
        monkeypatch,
        [
            "--endpoint", "e",
            "--model", "qwen3.8-27b",
            "--model-uri", "azureml:qwen3_8-27b-ko-merged:1",
        ],
    )
    assert seen["model_uri"] == "azureml:qwen3_8-27b-ko-merged:1"
    assert seen["hf_model"] is None
    assert seen["model_spec"].key == "qwen3.8-27b"
