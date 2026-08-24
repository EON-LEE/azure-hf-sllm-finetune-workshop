"""Turning a finished training job into a registered model asset.

This is the step the project could not take for its entire life. Every hosted
serving pattern -- online *and* batch -- deploys a registered model, so with no
model asset there was nothing to deploy and the deployment half of the asset was
untestable. Three separate causes had to be cleared first: the adapter was never
declared as an output, the storage account was unreachable, and the training
image never actually ran. Job `helpful_sand_971pqxtj0l` was the first run to
clear all three.

Two things were then measured against the live workspace, and both are pinned
here because both cost a round trip to discover.

**Asset names reject the dots that model keys are full of.** Registering
`kanana2-1.3b-ko-lora` fails:

    (RequestInvalid) Resource name is invalid. Resource name can only contain
    alphanumeric characters, dashes, and underscores, with a limit of 255
    characters.

The registry is keyed by `kanana2-1.3b`, `qwen3.8-27b`, `qwen3.5-0.8b` -- the
dot is in almost every key, so this is not an edge case, it is the default case.

**The URI has to name a datastore path.** `azureml://jobs/{job}/outputs/{name}`
reads like the obvious spelling and the service rejects it; a declared output
with no explicit path lands at
`azureml://datastores/workspaceblobstore/paths/azureml/{job}/{output}/`.
"""

from __future__ import annotations

import pytest

from ffsft.deploy.model_asset import asset_name, job_output_uri, register_adapter

# --------------------------------------------------------------------------
# asset_name
# --------------------------------------------------------------------------


def test_asset_name_replaces_the_dot_azure_ml_rejects():
    """The measured failure, pinned. `kanana2-1.3b` is a real registry key."""
    assert asset_name("kanana2-1.3b") == "kanana2-1_3b"


def test_asset_name_keeps_the_characters_that_are_legal():
    """Dashes and underscores are allowed, so nothing should touch them."""
    assert asset_name("qwen3-4b_instruct") == "qwen3-4b_instruct"


def test_asset_name_appends_a_suffix_to_say_what_the_asset_is():
    """A workspace holds many assets per base model; the key alone is ambiguous."""
    assert asset_name("qwen3.8-27b", suffix="ko-lora") == "qwen3_8-27b-ko-lora"


def test_asset_name_sanitises_the_suffix_too():
    """A caller-supplied suffix reaches the same validator as the key does."""
    assert asset_name("qwen3-4b", suffix="ko.lora") == "qwen3-4b-ko_lora"


def test_asset_name_stays_within_the_length_limit():
    """255 characters, enforced by the service. Truncate rather than 400."""
    name = asset_name("q" * 400)
    assert len(name) == 255


def test_asset_name_refuses_an_empty_key():
    with pytest.raises(ValueError, match="empty"):
        asset_name("")


def test_asset_name_refuses_a_key_with_nothing_legal_in_it():
    """`___` is a name the service accepts and no human can trace to a model."""
    with pytest.raises(ValueError, match="no usable"):
        asset_name("...")


# --------------------------------------------------------------------------
# job_output_uri
# --------------------------------------------------------------------------


def test_job_output_uri_points_at_the_datastore_path():
    assert job_output_uri("helpful_sand_971pqxtj0l") == (
        "azureml://datastores/workspaceblobstore/paths/azureml/"
        "helpful_sand_971pqxtj0l/model_dir/"
    )


def test_job_output_uri_can_name_a_different_output():
    assert job_output_uri("abc123", output="report").endswith("/abc123/report/")


def test_job_output_uri_never_produces_the_form_the_service_rejects():
    """`azureml://jobs/...` is the intuitive spelling and it returns an error.

    Measured while trying to register the adapter from `heroic_fennel_085y2rwm3s`:
    `(NoMatchingArtifactsFoundFromJob) No artifacts matching outputs found`.
    """
    assert "azureml://jobs/" not in job_output_uri("abc123")


def test_job_output_uri_refuses_an_empty_job_name():
    with pytest.raises(ValueError, match="job name"):
        job_output_uri("")


# --------------------------------------------------------------------------
# register_adapter
# --------------------------------------------------------------------------


class FakeModel:
    def __init__(self, name, version="1", path=None, tags=None, type=None, description=None):
        self.name, self.version, self.path = name, version, path
        self.tags, self.type, self.description = tags, type, description


class FakeModelOps:
    def __init__(self):
        self.registered = []

    def create_or_update(self, model):
        self.registered.append(model)
        return FakeModel(
            model.name,
            "1",
            path=model.path,
            tags=model.tags,
            type=model.type,
            description=model.description,
        )


class FakeClient:
    def __init__(self):
        self.models = FakeModelOps()


def test_register_adapter_uses_a_sanitised_name_and_a_datastore_path():
    client = FakeClient()

    ref = register_adapter(client, "helpful_sand_971pqxtj0l", "kanana2-1.3b")

    (sent,) = client.models.registered
    assert sent.name == "kanana2-1_3b-ko-lora"
    assert sent.path == job_output_uri("helpful_sand_971pqxtj0l")
    assert ref == "kanana2-1_3b-ko-lora:1"


def test_register_adapter_records_where_the_weights_came_from():
    """An adapter is useless without its base model.

    A LoRA adapter cannot be loaded on its own, and a bare folder of safetensors
    in a workspace gives no clue which checkpoint it was trained against. The
    tags are the only place that survives with the asset.
    """
    client = FakeClient()

    register_adapter(
        client,
        "helpful_sand_971pqxtj0l",
        "kanana2-1.3b",
        base_model="kakaocorp/kanana-2-1.3b-instruct",
        mix="ko_smoke",
    )

    tags = client.models.registered[0].tags
    assert tags["job"] == "helpful_sand_971pqxtj0l"
    assert tags["model_key"] == "kanana2-1.3b"
    assert tags["base_model"] == "kakaocorp/kanana-2-1.3b-instruct"
    assert tags["mix"] == "ko_smoke"


def test_register_adapter_keeps_the_unsanitised_key_in_a_tag():
    """The name loses information; something has to keep the real key.

    `kanana2-1_3b` cannot be looked up in `configs/models.yaml` -- only
    `kanana2-1.3b` can -- so the mapping back has to be stored, not guessed.
    """
    client = FakeClient()
    register_adapter(client, "job1", "qwen3.8-27b")

    assert client.models.registered[0].tags["model_key"] == "qwen3.8-27b"


def test_register_adapter_registers_a_custom_model():
    """`custom_model` is what a PEFT adapter folder is.

    `mlflow_model` would make Azure ML expect an MLmodel file and fail the
    deployment at rollout instead of here.
    """
    client = FakeClient()
    register_adapter(client, "job1", "qwen3-4b")

    assert client.models.registered[0].type == "custom_model"
