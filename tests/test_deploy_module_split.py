"""`endpoint.py` was split; the names it used to expose must still resolve.

`deploy/endpoint.py` was 1423 lines doing preflight probing, startup-budget
arithmetic, ARM calls and a CLI in one file. Two clusters moved out:

    probes.py     the live "ask Azure" calls -- quota, cluster, storage
    readiness.py  the pure arithmetic -- how long a container gets, and which
                  probe field the budget goes in

The move is meant to be invisible to callers, and these tests are what make
that claim checkable. Two distinct failures are pinned here.

**The import surface.** Every name an existing caller could import from
`endpoint` still imports from `endpoint`.

**The monkeypatch seam, which is the one that bites.** `check_pattern` now
calls `read_dedicated_quota` as a `probes` global. A test that patches the
re-export on `endpoint` therefore patches a name nobody reads, and the fake
never takes effect -- silently, right up until the real call leaves the
machine. That is not hypothetical: it is what this split did to
`test_from_hub_clears_the_store_blocker_for_online`, which went out to
management.azure.com for real and came back with a 400. The suite is supposed
to touch neither Azure nor the network (see the docstring of
`tests/test_aml_job.py`), so the seam gets a test of its own.
"""

import ffsft.deploy.endpoint as ep
import ffsft.deploy.probes as probes
import ffsft.deploy.readiness as readiness

MOVED_TO_PROBES = (
    "SkuProbe",
    "StoreProbe",
    "check_pattern",
    "classify_cluster_error",
    "classify_store",
    "probe_model_store",
    "probe_sku",
    "quota_family_for",
    "read_dedicated_quota",
)

MOVED_TO_READINESS = (
    "AZURE_DEFAULT_FAILURE_THRESHOLD",
    "AZURE_MAX_FAILURE_THRESHOLD",
    "IN_FLIGHT_QUANTIZATION_FACTOR",
    "PROBE_INITIAL_DELAY",
    "PROBE_PERIOD",
    "params_from_hf_id",
    "probe_settings_for",
    "resolve_params_b",
    "startup_grace_for",
)


def test_every_moved_name_still_imports_from_endpoint():
    for name in MOVED_TO_PROBES + MOVED_TO_READINESS:
        assert hasattr(ep, name), f"endpoint.{name} disappeared in the split"


def test_a_moved_name_is_the_same_object_not_a_copy():
    """A re-export, not a reimplementation -- `is`, so a fork cannot hide here."""
    for name in MOVED_TO_PROBES:
        assert getattr(ep, name) is getattr(probes, name)
    for name in MOVED_TO_READINESS:
        assert getattr(ep, name) is getattr(readiness, name)


def test_check_pattern_reads_the_quota_through_the_probes_module(monkeypatch):
    """Patching `probes` must reach `check_pattern`; patching `endpoint` must not.

    Both halves matter. The first is the working instruction. The second is the
    warning: it asserts that the stale spelling is genuinely inert, so nobody
    reads a passing suite as proof that either spelling works.
    """
    calls = []

    def fake(subscription_id, location, family):
        calls.append(family)
        return 9999

    monkeypatch.setattr(probes, "read_dedicated_quota", fake)
    ep.check_pattern("aml_online_vllm", "sub", "koreacentral", from_hub=True)
    assert calls, "check_pattern did not reach probes.read_dedicated_quota"

    monkeypatch.setattr(ep, "read_dedicated_quota", lambda *a, **k: 0)
    calls.clear()
    ep.check_pattern("aml_online_vllm", "sub", "koreacentral", from_hub=True)
    assert calls, "patching the endpoint re-export silently did nothing -- as designed"


def test_the_readiness_arithmetic_needs_no_azure_sdk():
    """`readiness.py` is pure; it must import with no cloud dependency present.

    This is what makes the startup budget testable at all. `serve` is the
    httpx-only extra on purpose, and a stray `azure.ai.ml` import at the top of
    this module would put the whole GPU dependency tree behind reading a
    number.
    """
    src = readiness.__file__
    with open(src) as fh:
        head = fh.read()
    for forbidden in ("import azure", "from azure", "import requests"):
        assert forbidden not in head, f"readiness.py grew a {forbidden!r}"


def test_the_split_left_endpoint_readable():
    """The point of the exercise: the file a participant must read got smaller.

    1423 lines was the state that motivated the split. The number is a ratchet,
    not a target -- it exists so that re-growing the file is a deliberate act
    that edits this line, rather than a drift nobody notices.
    """
    with open(ep.__file__) as fh:
        n = sum(1 for _ in fh)
    assert n < 1000, f"endpoint.py is back up to {n} lines"
