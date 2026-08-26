"""Shifting traffic must not be able to leave the endpoint routing to nothing.

Two hazards are pinned here. The first is arithmetic: a traffic map that names
only the winner says nothing about the losers, so a sibling can keep its share
and the endpoint ends up split or invalid. The second is object identity: the
endpoint entity that gets PUT has to be the one that was read back, because a
freshly constructed one serialises `traffic` as `{}` and the write silently
empties the map.
"""

from __future__ import annotations

import pytest

from ffsft.deploy.traffic import shift_traffic, traffic_map

# --------------------------------------------------------------------------
# traffic_map
# --------------------------------------------------------------------------


def test_every_sibling_is_named_and_zeroed_not_omitted():
    """Omitting a deployment does not zero it -- it says nothing about it."""
    assert traffic_map(["blue", "green"], "green") == {"blue": 0, "green": 100}


def test_a_single_deployment_endpoint_gets_all_of_it():
    assert traffic_map(["blue"], "blue") == {"blue": 100}


def test_the_shares_always_sum_to_one_hundred():
    got = traffic_map(["a", "b", "c", "d"], "c")
    assert sum(got.values()) == 100


def test_a_target_that_is_not_on_the_endpoint_is_refused_by_name():
    """The typo case. Naming what IS there is what makes the error actionable."""
    with pytest.raises(ValueError) as err:
        traffic_map(["blue", "green"], "gren")
    assert "gren" in str(err.value)
    assert "blue, green" in str(err.value)


# --------------------------------------------------------------------------
# shift_traffic
# --------------------------------------------------------------------------


class FakeEndpoint:
    def __init__(self, traffic):
        self.traffic = dict(traffic)


class FakeDeployment:
    def __init__(self, name, state="Succeeded"):
        self.name = name
        self.provisioning_state = state


class FakePoller:
    def __init__(self, on_result):
        self._on_result = on_result

    def result(self):
        return self._on_result()


class FakeEndpointOps:
    """Server-side traffic, handed out as a fresh object on every `get`.

    A real `get` is an HTTP round trip, so the caller never holds the server's
    object. Modelling that is what makes the read-back-then-PUT rule testable:
    mutating what `get` returned only reaches the server via the PUT.
    """

    def __init__(self, traffic):
        self.traffic = dict(traffic)
        self.put: list[dict] = []

    def get(self, name):  # noqa: ARG002 - one endpoint in these tests
        return FakeEndpoint(self.traffic)

    def begin_create_or_update(self, entity):
        sent = dict(entity.traffic or {})
        self.put.append(sent)

        def apply():
            self.traffic = sent
            return entity

        return FakePoller(apply)


class FakeDeploymentOps:
    def __init__(self, deployments):
        self.deployments = deployments

    def get(self, name, endpoint_name):  # noqa: ARG002
        return next(d for d in self.deployments if d.name == name)

    def list(self, endpoint_name):  # noqa: ARG002
        return list(self.deployments)


class FakeClient:
    def __init__(self, traffic, deployments):
        self.online_endpoints = FakeEndpointOps(traffic)
        self.online_deployments = FakeDeploymentOps(deployments)


def test_the_put_carries_the_whole_map_because_the_entity_was_read_back():
    """The regression in one line: an entity built fresh serialises traffic as
    {}, and PUTting that empties the map -- the endpoint keeps answering and
    routes to nothing."""
    client = FakeClient({"blue": 100}, [FakeDeployment("blue"), FakeDeployment("green")])

    shift_traffic(client, "ffsft-plc", "green")

    assert client.online_endpoints.put == [{"blue": 0, "green": 100}]


def test_a_completed_shift_leaves_the_target_at_one_hundred():
    client = FakeClient({"blue": 100}, [FakeDeployment("blue"), FakeDeployment("green")])
    assert shift_traffic(client, "ffsft-plc", "green") == {"blue": 0, "green": 100}


def test_an_endpoint_serving_nothing_yet_can_still_be_pointed_somewhere():
    """First deployment on a new endpoint: traffic starts as {} , not as blue=100."""
    client = FakeClient({}, [FakeDeployment("blue")])
    assert shift_traffic(client, "ffsft-plc", "blue") == {"blue": 100}


def test_a_deployment_still_creating_is_refused_before_anything_is_written():
    """It would accept the assignment and then serve 5xx behind the endpoint URL,
    which reads as the model failing rather than the rollout never finishing."""
    client = FakeClient(
        {"blue": 100},
        [FakeDeployment("blue"), FakeDeployment("green", state="Creating")],
    )
    with pytest.raises(RuntimeError) as err:
        shift_traffic(client, "ffsft-plc", "green")
    assert "Creating" in str(err.value)
    assert client.online_endpoints.put == []
    assert client.online_endpoints.traffic == {"blue": 100}


def test_a_failed_deployment_is_refused_too():
    client = FakeClient({"blue": 100}, [FakeDeployment("green", state="Failed")])
    with pytest.raises(RuntimeError):
        shift_traffic(client, "ffsft-plc", "green")


def test_the_provisioning_check_can_be_waived_for_a_deliberate_rollback():
    client = FakeClient({"blue": 100}, [FakeDeployment("blue", state="Updating")])
    assert shift_traffic(client, "ffsft-plc", "blue", require_succeeded=False) == {"blue": 100}


def test_a_shift_that_did_not_take_is_reported_rather_than_returned_as_success():
    """The write returning 200 is not evidence. The read-back is."""
    client = FakeClient({"blue": 100}, [FakeDeployment("blue"), FakeDeployment("green")])

    def swallow(entity):  # the control plane accepts, then does not apply it
        return FakePoller(lambda: entity)

    client.online_endpoints.begin_create_or_update = swallow

    with pytest.raises(RuntimeError) as err:
        shift_traffic(client, "ffsft-plc", "green")
    assert "expected 100" in str(err.value)
