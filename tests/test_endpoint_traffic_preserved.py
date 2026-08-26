"""Re-deploying must not silently take the endpoint's scoring URI down.

`deploy_online` began by "ensuring" the endpoint with an unconditional
`begin_create_or_update`. That is a PUT that replaces, and the entity it built
was constructed fresh rather than read back from Azure, so it serialised with an
explicit empty traffic map::

    ManagedOnlineEndpoint(name=..., auth_mode="key")
        ._to_rest_online_endpoint(location="polandcentral").properties.traffic
    -> {}

`{}` is not an omitted field ARM merges away -- it is an instruction to route
0% of traffic to every deployment. The failure is invisible from every status
Azure reports: deployments stay `Succeeded`, the endpoint stays `Succeeded`, and
only requests to the scoring URI stop working.

Measured on `ffsft-plc`: it served a full 100-request load test on that URI, and
later read back `traffic: {}` with deploys the only writes in between.

It also falsified the promise `deploy_online` makes in its `--traffic 0` branch
-- "leave the traffic map alone, so a bad rollout cannot take the endpoint
down" -- which then logged the `{}` the ensure-step had just created as though
it had found it that way. See docs/JOURNAL.md S65.
"""

from __future__ import annotations

from azure.core.exceptions import ResourceNotFoundError

from ffsft.deploy.endpoint import ensure_endpoint


class FakeEndpoint:
    def __init__(self, traffic=None, auth_mode="key"):
        self.traffic = traffic if traffic is not None else {}
        self.auth_mode = auth_mode


class FakeEndpoints:
    """Stands in for `client.online_endpoints`."""

    def __init__(self, existing: FakeEndpoint | None):
        self.existing = existing
        self.puts: list[object] = []

    def get(self, name):
        if self.existing is None:
            raise ResourceNotFoundError(f"no endpoint {name}")
        return self.existing

    def begin_create_or_update(self, entity):
        self.puts.append(entity)

        class _Poller:
            def result(self_inner):
                return entity

        return _Poller()


class FakeClient:
    def __init__(self, existing: FakeEndpoint | None):
        self.online_endpoints = FakeEndpoints(existing)


def test_the_put_it_used_to_send_really_does_carry_an_empty_traffic_map():
    """Pin the SDK behaviour the guard exists for.

    If a future SDK omits the field instead of sending `{}`, this fails and the
    next reader gets to re-derive whether the guard is still load-bearing rather
    than inheriting a claim nobody checked.
    """
    from azure.ai.ml.entities import ManagedOnlineEndpoint

    fresh = ManagedOnlineEndpoint(name="ffsft-plc", auth_mode="key")
    rest = fresh._to_rest_online_endpoint(location="polandcentral")
    assert rest.properties.traffic == {}


def test_an_endpoint_that_already_exists_is_never_written():
    client = FakeClient(FakeEndpoint(traffic={"blue": 100}))
    ensure_endpoint(client, "ffsft-plc")
    assert client.online_endpoints.puts == []


def test_the_live_traffic_split_survives_a_redeploy():
    """The regression in one line: blue keeps its 100%."""
    existing = FakeEndpoint(traffic={"blue": 100})
    client = FakeClient(existing)
    ensure_endpoint(client, "ffsft-plc")
    assert existing.traffic == {"blue": 100}


def test_a_missing_endpoint_is_still_created():
    client = FakeClient(None)
    ensure_endpoint(client, "ffsft-plc")
    assert len(client.online_endpoints.puts) == 1
    assert client.online_endpoints.puts[0].name == "ffsft-plc"


def test_a_created_endpoint_uses_key_auth():
    client = FakeClient(None)
    ensure_endpoint(client, "ffsft-plc")
    created = client.online_endpoints.puts[0]
    assert str(created.auth_mode).lower().endswith("key")


def test_an_unexpected_auth_mode_is_reported_not_rewritten(caplog):
    """Rewriting auth_mode would rotate how every existing client authenticates."""
    client = FakeClient(FakeEndpoint(traffic={"blue": 100}, auth_mode="aml_token"))
    with caplog.at_level("WARNING"):
        ensure_endpoint(client, "ffsft-plc")
    assert client.online_endpoints.puts == []
    assert any("auth_mode" in r.getMessage() for r in caplog.records)
