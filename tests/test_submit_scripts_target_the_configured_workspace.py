"""A flag left off must mean "my profile", never "the authors' resources".

Four scripts build an `AzureTarget` from argparse, and all four bypassed
`AzureTarget.from_env` in one of two ways.

`provision_azure.py` and `submit_training.py` gave `--resource-group`,
`--workspace` and `--location` argparse defaults of `rg-ffsft-kc` / `mlw-ffsft` /
`koreacentral` and never read the environment at all. A participant who had set
their profile correctly and omitted a flag provisioned into, or submitted a GPU
job to, the AUTHORS' resources -- and the failure arrives later as a permission
error naming a resource they have never heard of, which reads as a broken login.

`submit_bench.py` and `submit_merge.py` did read the environment and then applied
their flags with `dataclasses.replace`, which writes whatever it is handed. The
shape that reaches it is not `--resource-group ""` typed by hand; it is
`--resource-group "$RG"` with `RG` unset, where the shell hands argparse an empty
string. That reassembled a blank target *after* `from_env`'s guard had passed,
and a workspace read at rg='' is the §11.4 failure that guard exists to stop.

Both are now `AzureTarget.from_env(**flags)`: one rule, one place, flags win when
they say something.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]

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

#: The profile a participant is told to set, deliberately sharing no value with
#: the defaults -- so a target built from the defaults cannot pass by accident.
PROFILE = {
    "FFSFT_SUBSCRIPTION_ID": "sub-participant",
    "FFSFT_RESOURCE_GROUP": "rg-mine",
    "FFSFT_WORKSPACE": "mlw-mine",
    "FFSFT_LOCATION": "japaneast",
    "FFSFT_COMPUTE": "gpu-mine",
}


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_script", _ROOT / "scripts" / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def profile(monkeypatch):
    for name in FFSFT_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in PROFILE.items():
        monkeypatch.setenv(name, value)


def _run(monkeypatch, module: str, argv: list[str], entry: tuple[str, ...]):
    """Drive one script's `main` and return the target it built.

    `entry` names every function the script calls with that target -- all of
    them, not just the first: faking `ensure_workspace` and leaving
    `ensure_compute` real let the first draft of this test reach Azure for real
    and come back with `InvalidSubscriptionId`. Capturing the argument of the
    first is the whole measurement; stubbing the rest is what keeps the suite
    off the network.
    """
    mod = _load(module)
    captured = {}

    def capture(target, *a, **kw):
        captured.setdefault("target", target)
        return {"status": "Completed"}

    for name in entry:
        monkeypatch.setattr(mod, name, capture)
    # Anything that still tries to build a client is a path this test did not
    # fake, and it must fail here rather than on the network.
    import ffsft.azure_ml as azure_ml

    def no_azure(*a, **kw):
        raise AssertionError("the suite must not build a real MLClient")

    monkeypatch.setattr(azure_ml, "get_ml_client", no_azure)
    # `submit_training` and `submit_merge` both run a storage preflight first,
    # importing it inside `main`; None means "could not read", which is the
    # documented not-a-blocker answer.
    import ffsft.deploy.preflight as preflight

    monkeypatch.setattr(preflight, "read_storage_reachability", lambda *a, **kw: None)
    monkeypatch.setattr(sys, "argv", [module, *argv])
    mod.main()
    return captured["target"]


PATHS = [
    pytest.param(
        "provision_azure.py", [], ("ensure_workspace", "ensure_compute"), id="provision_azure"
    ),
    pytest.param("submit_training.py", ["--preflight"], ("submit",), id="submit_training"),
    pytest.param(
        "submit_bench.py", ["--model-asset", "qwen-merged:1"], ("submit",), id="submit_bench"
    ),
    pytest.param(
        "submit_merge.py", ["--adapter", "qwen-lora:1"], ("submit",), id="submit_merge"
    ),
]


@pytest.mark.parametrize("module,argv,entry", PATHS)
def test_omitting_every_flag_targets_the_profile_rather_than_the_authors_resources(
    module, argv, entry, profile, monkeypatch
):
    target = _run(monkeypatch, module, argv, entry)

    assert target.subscription_id == "sub-participant"
    assert target.resource_group == "rg-mine"
    assert target.workspace_name == "mlw-mine"
    assert target.compute_name == "gpu-mine"


@pytest.mark.parametrize("module,argv,entry", PATHS)
def test_a_flag_the_shell_expanded_to_nothing_does_not_blank_the_target(
    module, argv, entry, profile, monkeypatch
):
    # `--resource-group "$RG"` with RG unset. argparse has no opinion about an
    # empty string, so this is the realistic way a blank reaches the target.
    target = _run(monkeypatch, module, [*argv, "--resource-group", "", "--workspace", "  "], entry)

    assert target.resource_group == "rg-mine"
    assert target.workspace_name == "mlw-mine"


@pytest.mark.parametrize("module,argv,entry", PATHS)
def test_a_flag_that_says_something_still_wins_over_the_profile(
    module, argv, entry, profile, monkeypatch
):
    # The flags have to keep working -- this is a fix to what an ABSENT flag
    # means, not a removal of the override.
    target = _run(
        monkeypatch, module, [*argv, "--resource-group", "rg-flag", "--workspace", "mlw-flag"],
        entry,
    )

    assert target.resource_group == "rg-flag"
    assert target.workspace_name == "mlw-flag"


@pytest.mark.parametrize("module,argv,entry", PATHS)
def test_a_pasted_flag_is_stripped_the_way_a_pasted_variable_is(
    module, argv, entry, profile, monkeypatch
):
    # Azure 404s on ' rg-flag' and echoes the name back looking correct, which
    # is why the same strip has to apply on both sides of the boundary.
    target = _run(monkeypatch, module, [*argv, "--resource-group", " rg-flag "], entry)

    assert target.resource_group == "rg-flag"


@pytest.mark.parametrize("module,argv,entry", PATHS)
def test_the_subscription_flag_works_with_no_subscription_in_the_environment(
    module, argv, entry, monkeypatch
):
    # `from_env()` raises when the environment names no subscription, so the
    # scripts have to hand their flag to it rather than calling it first and
    # patching the result -- `--subscription` on a bare shell is how lab 1 is
    # written and it must keep working.
    for name in FFSFT_VARS:
        monkeypatch.delenv(name, raising=False)

    target = _run(monkeypatch, module, [*argv, "--subscription", "sub-from-the-flag"], entry)

    assert target.subscription_id == "sub-from-the-flag"
    assert target.resource_group == "rg-ffsft-kc"


def test_provision_reads_the_priority_off_the_target_not_off_the_bare_flag(profile, monkeypatch):
    # `--priority` lost its literal default in the same change, so anything
    # still reading `args.priority` sees None whenever the value came from
    # FFSFT_VM_PRIORITY -- and the quota advice below it would then name the
    # wrong tier while the cluster was created with the right one.
    monkeypatch.setenv("FFSFT_VM_PRIORITY", "Dedicated")

    target = _run(
        monkeypatch,
        "provision_azure.py",
        ["--model", "qwen3.5-9b"],
        ("ensure_workspace", "ensure_compute"),
    )

    assert target.vm_priority == "Dedicated"
