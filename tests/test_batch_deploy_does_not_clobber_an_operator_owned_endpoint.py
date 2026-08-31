"""`deploy_batch` must not PUT a freshly built entity at a live batch endpoint.

`deploy_batch` opened by "ensuring" the endpoint with an unconditional
`begin_create_or_update` over an entity it constructed on the spot -- never read
back from Azure. That is the same defect `ensure_endpoint` was fixed for in
docs/JOURNAL.md S65, four hundred lines above it in the same file, and it was
missed in the sibling function. See docs/JOURNAL.md S76.

What the fresh entity actually carries is not a guess. Executed against
azure-ai-ml 1.34.1::

    BatchEndpoint(name='ffsft-batch', description='ffsft offline scoring') \
        ._to_rest_batch_endpoint(location='koreacentral').as_dict()
    -> {'location': 'koreacentral',
        'tags': {},
        'properties': {'description': 'ffsft offline scoring',
                       'authMode': 'aadToken', 'properties': {}}}

`tags` is an explicit empty map, `description` is this tool's own string, and
`defaults` -- the batch endpoint's routing pointer, the field that decides which
deployment a scoring job actually runs on -- is gone from the body entirely.
Send that at an endpoint an operator owns and their cost-centre tags and their
routing pointer are what the request replaces.

The second failure is the ordering. The wiping PUT ran BEFORE the deployment
create, so a `ModelBatchDeployment` create that raises -- quota is the ordinary
reason -- aborted the run with the operator's routing already destroyed and only
the quota error on screen.

THESE FAKES ARE FAKES. No Azure is contacted and no output below was captured
from Azure. What they model is one rule: ARM's endpoint PUT is
create-or-REPLACE, so what the resource holds afterwards is what the request
body carried. Everything either side of that rule is the real SDK --
`_to_rest_batch_endpoint` builds the body and `_from_rest_object` reads it back
-- so the only invented behaviour is replace-versus-merge, and it is invented in
the direction that makes the test demand more, not less.
"""

from __future__ import annotations

import pytest
from azure.ai.ml._restclient.arm_ml_service.models import BatchEndpoint as BatchEndpointData
from azure.ai.ml._restclient.arm_ml_service.models import (
    BatchEndpointDefaults,
    BatchEndpointProperties,
)
from azure.ai.ml.entities import BatchEndpoint
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

import ffsft.deploy.endpoint as ep

LOCATION = "koreacentral"
SCORING_URI = "https://ffsft-batch.koreacentral.inference.ml.azure.com/jobs"
OPERATOR_TAGS = {"cost-centre": "kc-ml-01", "owner": "data-eng"}


def operator_owned(deployment_name: str = "green") -> BatchEndpointData:
    """A batch endpoint as an operator left it: tagged, described, routed."""
    return BatchEndpointData(
        location=LOCATION,
        tags=dict(OPERATOR_TAGS),
        properties=BatchEndpointProperties(
            description="nightly scoring for the pricing team",
            auth_mode="AADToken",
            defaults=BatchEndpointDefaults(deployment_name=deployment_name),
            scoring_uri=SCORING_URI,
        ),
    )


class _Poller:
    def __init__(self, value):
        self._value = value

    def result(self):
        return self._value


class FakeBatchEndpoints:
    """Stands in for `client.batch_endpoints` with ARM PUT-replace semantics."""

    def __init__(self, stored: BatchEndpointData | None, journal: list, read_error=None):
        self.stored = stored
        self.journal = journal
        #: What `get` raises instead of answering. A 403 is the case that
        #: matters: "could not look" must never become "looked, saw nothing".
        self.read_error = read_error
        self.puts: list[BatchEndpointData] = []

    def get(self, name):
        self.journal.append(("endpoint GET", name))
        if self.read_error is not None:
            raise self.read_error
        if self.stored is None:
            raise ResourceNotFoundError(f"no batch endpoint {name}")
        entity = BatchEndpoint._from_rest_object(self.stored)
        entity.name = name
        return entity

    def begin_create_or_update(self, entity):
        body = entity._to_rest_batch_endpoint(location=LOCATION)
        self.puts.append(body)
        self.journal.append(("endpoint PUT", entity.name))
        # PUT replaces. Server-owned readonly fields are the one thing ARM
        # repopulates rather than taking from the body, so they survive.
        body.properties.scoring_uri = SCORING_URI
        self.stored = body
        return _Poller(entity)

    def routing(self) -> str | None:
        if self.stored is None:
            return None
        defaults = self.stored.properties.defaults
        return getattr(defaults, "deployment_name", None) if defaults else None

    def tags(self) -> dict:
        return dict(self.stored.tags or {}) if self.stored else {}

    def description(self) -> str | None:
        return self.stored.properties.description if self.stored else None


class FakeBatchDeployments:
    def __init__(self, journal: list, error: Exception | None = None, existing=None):
        self.journal = journal
        self.error = error
        self.existing = existing
        self.created: list = []

    def get(self, name, endpoint_name):
        """`BatchDeploymentOperations.get(name, endpoint_name)`, positionally.

        Added in round 7, when `deploy_batch` stopped PUTting a deployment it
        had never read. `ResourceNotFoundError` and not `None` for absent,
        because that is the one answer the real operations class gives that
        positively establishes absence, and the gate is built on that
        distinction.
        """
        self.journal.append(("deployment GET", name))
        if self.existing is None:
            raise ResourceNotFoundError(f"batch deployment {name} not found")
        return self.existing

    def begin_create_or_update(self, deployment):
        self.journal.append(("deployment PUT", deployment.name))
        if self.error is not None:
            raise self.error
        self.created.append(deployment)
        return _Poller(deployment)


class FakeClient:
    def __init__(self, stored, *, deployment_error=None, read_error=None):
        self.journal: list = []
        self.batch_endpoints = FakeBatchEndpoints(stored, self.journal, read_error=read_error)
        self.batch_deployments = FakeBatchDeployments(self.journal, error=deployment_error)


def run_deploy(monkeypatch, *, stored, deployment_error=None, read_error=None) -> FakeClient:
    """Drive the real `deploy_batch` against fakes and hand back the client.

    `get_ml_client` is patched on `ffsft.azure_ml`, the module `deploy_batch`
    imports it from at call time -- patching the re-export would fake a name
    nobody reads (see `test_deploy_module_split`).
    """
    monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "00000000-0000-0000-0000-0000000000ff")

    from ffsft import azure_ml

    client = FakeClient(stored, deployment_error=deployment_error, read_error=read_error)
    monkeypatch.setattr(azure_ml, "get_ml_client", lambda *a, **k: client)
    ep.deploy_batch("ffsft-batch", "azureml:qwen-ko:1")
    return client


def test_a_freshly_built_batch_endpoint_entity_drops_defaults_and_empties_tags():
    """Pin the SDK behaviour the guard exists for.

    If a future azure-ai-ml carries `defaults` and the caller's tags through a
    fresh entity, this fails and the next reader re-derives whether the guard is
    still load-bearing instead of inheriting a claim nobody checked.
    """
    fresh = BatchEndpoint(name="ffsft-batch", description="ffsft offline scoring")
    assert fresh.defaults is None
    assert fresh.tags == {}
    body = fresh._to_rest_batch_endpoint(location=LOCATION).as_dict()
    assert body["tags"] == {}
    assert "defaults" not in body["properties"]


def test_a_completed_deploy_leaves_the_endpoint_routing_to_the_deployment_it_made(monkeypatch):
    """Repointing is the job; this pins that the job still gets done."""
    client = run_deploy(monkeypatch, stored=operator_owned("green"))
    assert client.batch_endpoints.routing() == "default"


def test_the_operators_tags_on_a_live_batch_endpoint_survive_a_redeploy(monkeypatch):
    """Cost-centre tags are how the spend is attributed; a PUT must not eat them."""
    client = run_deploy(monkeypatch, stored=operator_owned("green"))
    assert client.batch_endpoints.tags() == OPERATOR_TAGS


def test_the_operators_description_is_not_replaced_with_this_tools_own_string(monkeypatch):
    client = run_deploy(monkeypatch, stored=operator_owned("green"))
    assert client.batch_endpoints.description() == "nightly scoring for the pricing team"


def test_an_existing_batch_endpoint_is_read_before_anything_is_written(monkeypatch):
    """No write may precede the read that decides whether a write is safe."""
    client = run_deploy(monkeypatch, stored=operator_owned("green"))
    kinds = [kind for kind, _ in client.journal]
    assert kinds[0] == "endpoint GET", client.journal


def test_a_failed_deployment_create_leaves_the_routing_pointer_untouched(monkeypatch):
    """The quota case: the run aborts, and the operator's endpoint still routes.

    Aborting is fine. Aborting after having silently destroyed the routing the
    operator depends on, while the screen shows a quota error, is not.
    """
    quota = HttpResponseError("BadRequest: not enough quota for the requested instances")
    with pytest.raises(HttpResponseError):
        run_deploy(monkeypatch, stored=operator_owned("green"), deployment_error=quota)


def test_the_endpoint_is_not_written_at_all_when_the_deployment_create_fails(monkeypatch):
    quota = HttpResponseError("BadRequest: not enough quota for the requested instances")
    client = FakeClient(operator_owned("green"), deployment_error=quota)
    monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "00000000-0000-0000-0000-0000000000ff")
    from ffsft import azure_ml

    monkeypatch.setattr(azure_ml, "get_ml_client", lambda *a, **k: client)
    with pytest.raises(HttpResponseError):
        ep.deploy_batch("ffsft-batch", "azureml:qwen-ko:1")
    assert client.batch_endpoints.puts == []
    assert client.batch_endpoints.routing() == "green"
    assert client.batch_endpoints.tags() == OPERATOR_TAGS


def test_a_batch_endpoint_read_that_failed_is_never_treated_as_a_missing_endpoint(monkeypatch):
    """A 403 on the read must abort, not fall through to creating the endpoint.

    This is the invariant this round is about: "could not look" reported as
    "looked, saw nothing". `ResourceNotFoundError` is the only answer that means
    absent; every other failure is an unanswered question, and answering it with
    a create is how a PUT lands on a resource nobody checked.
    """
    denied = HttpResponseError("AuthorizationFailed: no read on the batch endpoint")
    client = FakeClient(operator_owned("green"), read_error=denied)
    monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "00000000-0000-0000-0000-0000000000ff")
    from ffsft import azure_ml

    monkeypatch.setattr(azure_ml, "get_ml_client", lambda *a, **k: client)
    with pytest.raises(HttpResponseError):
        ep.deploy_batch("ffsft-batch", "azureml:qwen-ko:1")
    assert client.batch_endpoints.puts == []
    assert client.batch_deployments.created == []


def test_a_missing_batch_endpoint_is_still_created(monkeypatch):
    client = run_deploy(monkeypatch, stored=None)
    assert client.batch_endpoints.puts, "nothing was created for an absent endpoint"
    assert client.batch_endpoints.routing() == "default"


def test_a_batch_endpoint_created_from_nothing_carries_this_tools_description(monkeypatch):
    client = run_deploy(monkeypatch, stored=None)
    assert client.batch_endpoints.description() == "ffsft offline scoring"


def test_the_default_pointer_is_left_alone_when_it_already_names_this_deployment(monkeypatch):
    """A PUT that changes nothing is still a PUT; do not send it."""
    client = run_deploy(monkeypatch, stored=operator_owned("default"))
    assert client.batch_endpoints.puts == []


def test_the_previous_default_deployment_is_logged_before_it_is_repointed(monkeypatch, caplog):
    """Repointing is this command's job; doing it silently is not.

    The old pointer is the only thing that tells the operator what to put back.
    """
    with caplog.at_level("INFO"):
        run_deploy(monkeypatch, stored=operator_owned("green"))
    assert any("green" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


def test_the_scoring_uri_it_returns_is_the_one_azure_reports(monkeypatch):
    monkeypatch.setenv("FFSFT_SUBSCRIPTION_ID", "00000000-0000-0000-0000-0000000000ff")
    from ffsft import azure_ml

    client = FakeClient(operator_owned("green"))
    monkeypatch.setattr(azure_ml, "get_ml_client", lambda *a, **k: client)
    assert ep.deploy_batch("ffsft-batch", "azureml:qwen-ko:1") == SCORING_URI


def test_the_batch_surface_still_imports_from_endpoint_after_the_split():
    """`deploy_batch` moved to `deploy/batch.py`; the old import must still work.

    Same contract `test_deploy_module_split` holds probes.py and readiness.py to.
    The move happened because the line ratchet in that file asks for code to be
    moved out rather than for the number to be raised again.
    """
    from ffsft.deploy import batch

    assert ep.deploy_batch is batch.deploy_batch
    assert ep.ensure_batch_endpoint is batch.ensure_batch_endpoint
    assert "deploy_batch" in ep.__all__
    assert "ensure_batch_endpoint" in ep.__all__


def test_batch_py_reaches_for_no_azure_package_at_import_time():
    """Function-local Azure imports, like the rest of this package.

    A module-level `from azure...` both breaks the CPU-only install and moves
    the monkeypatch seam out from under every test that fakes the SDK.
    """
    from ffsft.deploy import batch

    with open(batch.__file__) as fh:
        source = fh.read()
    module_level = [
        line
        for line in source.splitlines()
        if line.startswith(("import azure", "from azure", "import requests"))
    ]
    assert module_level == [], module_level


def test_an_endpoint_that_already_exists_is_never_written_by_the_guard_itself():
    """`ensure_batch_endpoint` on its own, with no deployment step in the way."""
    from ffsft.deploy import batch

    journal: list = []
    endpoints = FakeBatchEndpoints(operator_owned("green"), journal)
    batch.ensure_batch_endpoint(_ClientOf(endpoints), "ffsft-batch")
    assert endpoints.puts == []
    assert endpoints.routing() == "green"
    assert endpoints.tags() == OPERATOR_TAGS


def test_the_guard_creates_an_absent_endpoint_with_the_tools_description():
    from ffsft.deploy import batch

    journal: list = []
    endpoints = FakeBatchEndpoints(None, journal)
    batch.ensure_batch_endpoint(_ClientOf(endpoints), "ffsft-batch")
    assert len(endpoints.puts) == 1
    assert endpoints.description() == "ffsft offline scoring"


def test_the_guard_lets_a_read_it_could_not_perform_propagate():
    """Only `ResourceNotFoundError` means absent. A 403 is not an answer."""
    from ffsft.deploy import batch

    journal: list = []
    denied = HttpResponseError("AuthorizationFailed: no read on the batch endpoint")
    endpoints = FakeBatchEndpoints(operator_owned("green"), journal, read_error=denied)
    with pytest.raises(HttpResponseError):
        batch.ensure_batch_endpoint(_ClientOf(endpoints), "ffsft-batch")
    assert endpoints.puts == []


class _ClientOf:
    """The smallest thing `ensure_batch_endpoint` needs: one attribute."""

    def __init__(self, endpoints):
        self.batch_endpoints = endpoints


def test_the_default_deployment_name_is_read_from_both_shapes_the_sdk_uses():
    """azure-ai-ml hands `defaults` back as a REST object but takes it as a dict.

    So one accessor cannot serve both directions: `.get(...)` raises on what a
    real `get()` returned, `.deployment_name` raises on what this module set.
    """
    from ffsft.deploy.batch import _default_deployment_name

    from_azure = BatchEndpoint._from_rest_object(operator_owned("green")).defaults
    assert not isinstance(from_azure, dict), "the asymmetry this helper exists for is gone"
    assert _default_deployment_name(from_azure) == "green"
    assert _default_deployment_name({"deployment_name": "blue"}) == "blue"
    assert _default_deployment_name(None) is None
