"""`scripts/_common.sh` and `AzureTarget.from_env` must land on one workspace.

The header of `_common.sh` promises it: "the same FFSFT_* variables the Python
side reads (`AzureTarget.from_env`), so a shell script and `ffsft-deploy` never
disagree about which workspace they are pointed at". Round 3 taught Python that
blank means unset -- including whitespace -- and left the shell on
`${VAR:-default}`, which fires on empty and not on "  ". Measured before the fix,
with one environment:

    FFSFT_RESOURCE_GROUP="  "   bash -> "  "   from_env() -> 'rg-ffsft-kc'

`verify_deployment.sh` builds `FFSFT_WS_URI` from its value and `ffsft-deploy`
builds an MLClient from the other, so one workshop step read two resource
groups: the lab's `az rest` calls 404 on a group whose name is a space, while
the tool next to it quietly answered about `rg-ffsft-kc`.

These tests run the real `_common.sh` in a real bash and the real `from_env` in
this process, on the same environment, and compare. `az` is stubbed on PATH so
nothing leaves the machine -- the preamble's identity check runs `az account
show`, and the point here is the variable resolution above it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ffsft.azure_ml import AzureTarget

_ROOT = Path(__file__).resolve().parents[1]
_COMMON = _ROOT / "scripts" / "_common.sh"

#: Cleared for every case, so a developer workstation that legitimately exports
#: these cannot decide the result on either side of the comparison.
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
    "FFSFT_ACCOUNT",
)

_AZ_STUB = """#!/usr/bin/env bash
# Stands in for the Azure CLI. Answers `account show` from the environment and
# reaches no network, so this test exercises the variable resolution above it.
if [ "$1" = "account" ] && [ "$2" = "show" ]; then
  case "$*" in
    *user.name*) printf '%s\\n' "${FAKE_AZ_USER:-someone@example.com}" ;;
    *) printf '%s\\n' "${FFSFT_SUBSCRIPTION_ID:-${AZURE_SUBSCRIPTION_ID:-}}" ;;
  esac
  exit 0
fi
exit 1
"""


@pytest.fixture
def resolve_in_bash(tmp_path):
    """Source `_common.sh` in a clean bash and report what it resolved."""
    if shutil.which("bash") is None:  # pragma: no cover - bash is the shell here
        pytest.skip("bash is required to run the shell half of this comparison")
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    az = stub_dir / "az"
    az.write_text(_AZ_STUB)
    az.chmod(0o755)

    def run(**env) -> dict[str, str]:
        clean = {"PATH": f"{stub_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
        clean.update(env)
        script = (
            f'. "{_COMMON}"\n'
            'printf "%s\\n%s\\n%s\\n%s\\n" "$FFSFT_SUBSCRIPTION_ID" '
            '"$FFSFT_RESOURCE_GROUP" "$FFSFT_WORKSPACE" "$FFSFT_LOCATION"\n'
        )
        done = subprocess.run(
            ["bash", "-c", script], env=clean, capture_output=True, text=True, timeout=60
        )
        assert done.returncode == 0, done.stderr
        sub, rg, ws, loc = done.stdout.splitlines()
        return {"subscription_id": sub, "resource_group": rg, "workspace_name": ws,
                "location": loc}

    return run


def _resolve_in_python(monkeypatch, **env) -> dict[str, str]:
    for name in FFSFT_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    target = AzureTarget.from_env()
    return {
        "subscription_id": target.subscription_id,
        "resource_group": target.resource_group,
        "workspace_name": target.workspace_name,
        "location": target.location,
    }


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({"FFSFT_SUBSCRIPTION_ID": "sub-real"}, id="nothing-but-the-subscription"),
        pytest.param(
            {"FFSFT_SUBSCRIPTION_ID": "sub-real", "FFSFT_RESOURCE_GROUP": "rg-mine",
             "FFSFT_WORKSPACE": "mlw-mine", "FFSFT_LOCATION": "japaneast"},
            id="a-fully-configured-profile",
        ),
        pytest.param(
            {"FFSFT_SUBSCRIPTION_ID": "sub-real", "FFSFT_RESOURCE_GROUP": ""},
            id="the-blanked-profile-lab0-used-to-write",
        ),
        pytest.param(
            {"FFSFT_SUBSCRIPTION_ID": "sub-real", "FFSFT_RESOURCE_GROUP": "  "},
            id="a-resource-group-of-spaces",
        ),
        pytest.param(
            {"FFSFT_SUBSCRIPTION_ID": "sub-real", "FFSFT_WORKSPACE": "\t"},
            id="a-workspace-of-one-tab",
        ),
        pytest.param(
            {"FFSFT_SUBSCRIPTION_ID": "sub-real", "FFSFT_RESOURCE_GROUP": " rg-mine "},
            id="a-resource-group-pasted-with-its-surrounding-spaces",
        ),
        pytest.param(
            {"FFSFT_SUBSCRIPTION_ID": " sub-real ", "FFSFT_LOCATION": " koreacentral"},
            id="a-subscription-and-region-pasted-with-spaces",
        ),
    ],
)
def test_bash_and_python_resolve_one_target_from_one_environment(env, resolve_in_bash, monkeypatch):
    assert resolve_in_bash(**env) == _resolve_in_python(monkeypatch, **env)


def test_the_shell_refuses_a_subscription_of_whitespace_the_way_python_does(resolve_in_bash):
    # Python raises here. The shell used to accept "   " -- `${VAR:?}` fires only
    # on empty -- and handed `az` a subscription of spaces, which surfaces as a
    # not-found on an id that prints as blank.
    with pytest.raises(AssertionError):
        resolve_in_bash(FFSFT_SUBSCRIPTION_ID="   ")


def test_python_raises_on_the_same_whitespace_subscription(monkeypatch):
    with pytest.raises(RuntimeError):
        _resolve_in_python(monkeypatch, FFSFT_SUBSCRIPTION_ID="   ")


def test_the_shell_reads_azure_subscription_id_as_a_fallback_like_from_env(
    resolve_in_bash, monkeypatch
):
    # `from_env` has always read this pair. The shell read only the first, so a
    # workstation configured with the Azure-standard name ran `ffsft-deploy`
    # fine and could not run the lab's shell scripts at all.
    env = {"AZURE_SUBSCRIPTION_ID": "sub-from-the-azure-name"}

    assert resolve_in_bash(**env) == _resolve_in_python(monkeypatch, **env)


def test_a_child_process_inherits_what_the_preamble_resolved(tmp_path, resolve_in_bash):
    """A profile sourced without `set -a` used to split the two halves apart.

    `FFSFT_RESOURCE_GROUP=rg-mine` as a plain shell variable is invisible to a
    child, so `az` in the script read rg-mine and the `ffsft-deploy` on the next
    line of the same lab read rg-ffsft-kc -- the disagreement the header of
    `_common.sh` says cannot happen, arriving through a missing `export`.
    """
    if shutil.which("bash") is None:  # pragma: no cover - bash is the shell here
        pytest.skip("bash is required to run the shell half of this comparison")
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    az = stub_dir / "az"
    az.write_text(_AZ_STUB)
    az.chmod(0o755)

    script = (
        'FFSFT_SUBSCRIPTION_ID=sub-real\n'      # set, deliberately not exported
        'FFSFT_RESOURCE_GROUP=rg-mine\n'
        f'. "{_COMMON}"\n'
        'env | grep "^FFSFT_RESOURCE_GROUP=" || echo "NOT EXPORTED"\n'
    )
    done = subprocess.run(
        ["bash", "-c", script],
        env={"PATH": f"{stub_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "FFSFT_RESOURCE_GROUP=rg-mine"


def test_the_shell_defaults_are_the_same_strings_python_defaults_to(resolve_in_bash, monkeypatch):
    """The default values are written twice; this is what stops them drifting.

    A shell preamble cannot import Python, so `rg-ffsft-kc` / `mlw-ffsft` /
    `koreacentral` appear both in `_common.sh` and in `AzureTarget.from_env`.
    Comparing the two resolutions on an empty environment is cheaper than a
    convention that says "remember to change both".
    """
    env = {"FFSFT_SUBSCRIPTION_ID": "sub-real"}
    resolved = resolve_in_bash(**env)

    assert resolved == _resolve_in_python(monkeypatch, **env)
    assert resolved["resource_group"] == "rg-ffsft-kc"
    assert resolved["workspace_name"] == "mlw-ffsft"
    assert resolved["location"] == "koreacentral"
