"""The serving image must be the caller's, not the authors'.

`SERVE_IMAGE` was a constant naming `acrffsftkc.azurecr.io`, a registry a
participant cannot pull from, and `deploy-online` had no flag for anything
else. lab4 tells them to run `az acr build --registry <your-acr> --image
ffsft-serve:1` and there was then no supported way to deploy the image they
had just built, so the whole managed-online track stopped there for everyone
outside this one subscription.

The dangerous half of the fix is not the flag, it is what the flag drags with
it. Three things have to name the same image and two of them fail silently
when they do not:

* the Azure ML **environment version**, which is immutable -- deploying a new
  image under version `5` returns the entity already registered against the
  authors' `:5` and serves that, with no error anywhere;
* the environment's own `image`;
* the registry the endpoint's managed identity is granted **AcrPull** on --
  grant the wrong one and the pull fails with no container logs at all, which
  is the failure `deploy/identity.py` was written for.

So the version is derived at call time from the image actually being deployed,
and the module-level `SERVE_ENVIRONMENT_VERSION` is now nothing but a
compatibility export. The two ast tests below fail if any function reads it
again, because that regression cannot be seen in the output of a deploy.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from azure.core.exceptions import ResourceNotFoundError

from ffsft.azure_ml import image_tag
from ffsft.deploy import endpoint as ep
from ffsft.deploy import identity as ident
from ffsft.deploy import preflight as pf

MINE = "myacr.azurecr.io/ffsft-serve:1"
FROM_ENV = "otheracr.azurecr.io/ffsft-serve:7"


def _no_env(monkeypatch):
    monkeypatch.delenv(ep.SERVE_IMAGE_ENV_VAR, raising=False)


# --------------------------------------------------------------------------
# precedence


def test_the_flag_wins_over_the_env_var_which_wins_over_the_default(monkeypatch):
    monkeypatch.setenv(ep.SERVE_IMAGE_ENV_VAR, FROM_ENV)
    assert ep.resolve_serve_image(MINE) == MINE
    assert ep.resolve_serve_image(None) == FROM_ENV
    _no_env(monkeypatch)
    assert ep.resolve_serve_image(None) == ep.SERVE_IMAGE


def test_the_default_is_the_shipped_constant_and_is_unchanged(monkeypatch):
    """The fix adds a way out; it does not move where an unconfigured deploy lands."""
    _no_env(monkeypatch)
    assert ep.SERVE_IMAGE == "acrffsftkc.azurecr.io/ffsft-serve:5"
    assert ep.resolve_serve_image() == ep.SERVE_IMAGE


def test_an_exported_but_empty_variable_is_treated_as_unset(monkeypatch):
    """`export FFSFT_SERVE_IMAGE=` is a shell accident, not a request to deploy ''."""
    monkeypatch.setenv(ep.SERVE_IMAGE_ENV_VAR, "")
    assert ep.resolve_serve_image(None) == ep.SERVE_IMAGE
    monkeypatch.setenv(ep.SERVE_IMAGE_ENV_VAR, "  ")
    assert ep.resolve_serve_image(None) == ep.SERVE_IMAGE
    assert ep.resolve_serve_image("") == ep.SERVE_IMAGE


def test_the_names_this_module_has_always_exported_still_resolve():
    """Existing imports (and docs/labs/lab4.md, which quotes both) keep working."""
    for name in ("SERVE_IMAGE", "SERVE_ENVIRONMENT_VERSION", "SERVE_IMAGE_ENV_VAR"):
        assert name in ep.__all__
        assert getattr(ep, name)
    assert ep.SERVE_ENVIRONMENT_VERSION == image_tag(ep.SERVE_IMAGE)


# --------------------------------------------------------------------------
# the environment version follows the image, not the constant


class FakeEnvOps:
    def __init__(self, registered=None):
        self.registered = registered or []

    def get(self, name, version=None):
        for env in self.registered:
            if env.name == name and env.version == version:
                return env
        raise ResourceNotFoundError(f"no environment {name}:{version}")


class FakeEnv:
    def __init__(self, name, version, image):
        self.name, self.version, self.image = name, version, image


class FakeEnvClient:
    def __init__(self, ops):
        self.environments = ops


def test_the_environment_version_follows_the_image_passed_in(monkeypatch):
    """Not `SERVE_ENVIRONMENT_VERSION`, which is the *default* image's tag.

    An Azure ML environment version is immutable. Registering `:1` under
    version `5` hands back the version already holding the authors' `:5`, and
    the deployment then runs an image the caller never named -- the exact
    failure the derived version exists to prevent, one layer down.
    """
    _no_env(monkeypatch)
    env = ep.serve_environment(FakeEnvClient(FakeEnvOps()), MINE)
    assert env.image == MINE
    assert env.version == "1" == image_tag(MINE)
    assert env.version != ep.SERVE_ENVIRONMENT_VERSION


def test_the_environment_falls_back_to_the_env_var_when_no_image_is_named(monkeypatch):
    monkeypatch.setenv(ep.SERVE_IMAGE_ENV_VAR, FROM_ENV)
    env = ep.serve_environment(FakeEnvClient(FakeEnvOps()))
    assert (env.image, env.version) == (FROM_ENV, "7")


def test_a_version_already_holding_a_different_image_is_still_refused(monkeypatch):
    """The immutability check has to move with the image or it checks nothing."""
    _no_env(monkeypatch)
    stale = FakeEnv(ep.SERVE_ENVIRONMENT_NAME, "1", "someoneelse.azurecr.io/ffsft-serve:1")
    with pytest.raises(RuntimeError) as excinfo:
        ep.serve_environment(FakeEnvClient(FakeEnvOps([stale])), MINE)
    message = str(excinfo.value)
    assert MINE in message and "someoneelse.azurecr.io/ffsft-serve:1" in message
    assert "--image" in message, "the message must name the knob that fixes it"


def _function_bodies() -> list[ast.AST]:
    tree = ast.parse(Path(ep.__file__).read_text(encoding="utf-8"))
    return [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _reads(node: ast.AST, name: str) -> bool:
    return any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(node))


def test_no_function_reads_the_module_level_environment_version():
    """The regression this run exists to prevent, and it produces no error message.

    A runtime image paired with an import-time version deploys the wrong
    container and reports success. Static, because the only dynamic evidence
    would be a real rollout.
    """
    offenders = [f.name for f in _function_bodies() if _reads(f, "SERVE_ENVIRONMENT_VERSION")]
    assert not offenders, (
        f"{offenders} read SERVE_ENVIRONMENT_VERSION; derive it from the image "
        f"being deployed with image_tag(), as serve_environment() does"
    )


def test_only_the_resolver_reads_the_default_image_constant():
    """One place decides which image is deployed, or the callers drift apart."""
    offenders = [f.name for f in _function_bodies() if _reads(f, "SERVE_IMAGE")]
    assert offenders == ["resolve_serve_image"], (
        f"{offenders} read SERVE_IMAGE directly; call resolve_serve_image() so "
        f"--image and $FFSFT_SERVE_IMAGE are honoured"
    )


# --------------------------------------------------------------------------
# end to end: a participant's own registry, through deploy_online


class FakeDeployments:
    def __init__(self):
        self.created = []

    def get(self, name=None, endpoint_name=None):
        raise ResourceNotFoundError("no deployment")

    def begin_create_or_update(self, deployment):
        self.created.append(deployment)
        return _Poller(deployment)


class _Poller:
    def __init__(self, value):
        self._value = value

    def result(self):
        return self._value


class FakeIdentity:
    principal_id = "00000000-0000-0000-0000-000000000001"


class FakeEndpoint:
    def __init__(self):
        self.traffic = {}
        self.identity = FakeIdentity()
        self.scoring_uri = "https://ffsft-online.koreacentral.inference.ml.azure.com/score"


class FakeEndpoints:
    def __init__(self, endpoint):
        self.endpoint = endpoint

    def get(self, name=None, **_kw):
        return self.endpoint

    def begin_create_or_update(self, entity):
        return _Poller(entity)


class FakeClient:
    def __init__(self):
        self.online_deployments = FakeDeployments()
        self.online_endpoints = FakeEndpoints(FakeEndpoint())
        self.environments = FakeEnvOps()


def _run_deploy(monkeypatch, **kwargs):
    """Drive `deploy_online` against fakes; return (deployment, acr images seen).

    Every Azure read is stubbed at the module the caller reaches for, not at
    `endpoint`'s re-export -- patching a re-export fakes a name nobody reads
    and the real call leaves the machine (CLAUDE.md, `test_deploy_module_split`).
    """
    monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "00000000-0000-0000-0000-0000000000ff")
    monkeypatch.setenv("FFSFT_TENANT_ID", "00000000-0000-0000-0000-0000000000ee")

    from ffsft import azure_ml
    from ffsft.deploy.registry import get_serving_registry

    spec = get_serving_registry().get("aml_online_vllm")
    monkeypatch.setattr(ep, "check_pattern", lambda *a, **k: (spec, None))
    monkeypatch.setattr(ep, "ensure_endpoint", lambda *a, **k: None)
    monkeypatch.setattr(pf, "read_storage_reachability", lambda *a, **k: None)
    monkeypatch.setattr(pf, "read_sku_availability", lambda *a, **k: None)
    monkeypatch.setattr(pf, "online_endpoint_blocker", lambda *a, **k: None)
    monkeypatch.setattr(pf, "sku_advisory", lambda *a, **k: None)

    seen: list[str] = []
    granted: list[str] = []

    def fake_acr_id(image, subscription_id, resource_group, **_kw):
        seen.append(image)
        registry = image.split("/", 1)[0].split(".", 1)[0]
        return (
            f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
            f"/providers/Microsoft.ContainerRegistry/registries/{registry}"
        )

    def fake_grant(scope, principal, **_kw):
        granted.append(scope)
        return ident.GrantResult(already_had=True)

    monkeypatch.setattr(ident, "acr_id_for_image", fake_acr_id)
    monkeypatch.setattr(ident, "ensure_acr_pull", fake_grant)
    monkeypatch.setattr(ident, "read_identity_grants", lambda *a, **k: None)

    client = FakeClient()
    monkeypatch.setattr(azure_ml, "get_ml_client", lambda *a, **k: client)

    ep.deploy_online("ffsft-online", None, hf_model="Qwen/Qwen3.5-0.8B", **kwargs)
    return client.online_deployments.created[0], seen, granted


def test_a_participants_own_registry_resolves_end_to_end(monkeypatch):
    """The lab4 case: the image they built, the version its tag implies, their ACR."""
    _no_env(monkeypatch)
    deployment, images, granted = _run_deploy(monkeypatch, image=MINE)

    assert deployment.environment.image == MINE
    assert deployment.environment.version == "1"
    assert images == [MINE]
    assert granted and granted[0].endswith("/registries/myacr")


def test_the_acr_pull_identity_is_computed_from_the_image_passed_in(monkeypatch):
    """A grant on the authors' ACR is worth nothing to the participant's pull.

    It also fails invisibly: no logs are produced at all, because the container
    never starts. Two endpoints were lost to that before `identity.py` existed.
    """
    _no_env(monkeypatch)
    _, images, granted = _run_deploy(monkeypatch, image=MINE)
    assert ep.SERVE_IMAGE not in images
    assert not any("acrffsftkc" in scope for scope in granted)


def test_the_env_var_reaches_a_deploy_that_names_no_image(monkeypatch):
    monkeypatch.setenv(ep.SERVE_IMAGE_ENV_VAR, FROM_ENV)
    deployment, images, granted = _run_deploy(monkeypatch)
    assert deployment.environment.image == FROM_ENV
    assert deployment.environment.version == "7"
    assert images == [FROM_ENV]
    assert granted[0].endswith("/registries/otheracr")


def test_a_deploy_that_names_nothing_still_gets_the_shipped_image(monkeypatch):
    _no_env(monkeypatch)
    deployment, images, _ = _run_deploy(monkeypatch)
    assert deployment.environment.image == ep.SERVE_IMAGE
    assert deployment.environment.version == ep.SERVE_ENVIRONMENT_VERSION
    assert images == [ep.SERVE_IMAGE]


def test_an_untagged_image_is_refused_before_the_rollout_starts(monkeypatch):
    """`:latest` and digests cannot supply an environment version.

    Refusing at the top of `deploy_online` is free; the alternative is finding
    out after Azure has spent 15-30 minutes allocating a GPU node.
    """
    _no_env(monkeypatch)
    with pytest.raises(ValueError) as excinfo:
        _run_deploy(monkeypatch, image="myacr.azurecr.io/ffsft-serve")
    assert "carries no tag" in str(excinfo.value)


# --------------------------------------------------------------------------
# the CLI is the only surface a participant has


def test_deploy_online_takes_an_image_flag():
    args = ep.build_parser().parse_args(
        ["deploy-online", "--hf-model", "Qwen/Qwen3.5-0.8B", "--image", MINE]
    )
    assert args.image == MINE


def test_the_flag_defaults_to_none_so_the_env_var_can_still_win():
    """A parser-level default would shadow $FFSFT_SERVE_IMAGE for every deploy."""
    args = ep.build_parser().parse_args(["deploy-online", "--hf-model", "x"])
    assert args.image is None


def test_the_flag_is_handed_to_deploy_online(monkeypatch):
    calls = []
    monkeypatch.setattr(ep, "deploy_online", lambda *a, **k: calls.append(k))
    ep.main(["deploy-online", "--hf-model", "Qwen/Qwen3.5-0.8B", "--image", MINE])
    assert calls[0]["image"] == MINE


def test_the_help_text_points_at_the_registry_the_participant_owns():
    """lab4 has them build an image; --help is where they look for where to put it."""
    text = ep.build_parser().format_help()
    assert "deploy-online" in text
    help_for_image = [
        action.help
        for parser in [ep.build_parser()]
        for action in parser._subparsers._group_actions[0].choices["deploy-online"]._actions
        if action.dest == "image"
    ]
    assert help_for_image and ep.SERVE_IMAGE_ENV_VAR in help_for_image[0]
    assert "az acr build" in help_for_image[0]
