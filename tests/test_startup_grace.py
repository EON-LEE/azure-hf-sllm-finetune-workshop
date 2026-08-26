"""Probe timings should scale with the model, not be fixed at the 27B worst case.

`deploy_online` hardcoded a 600-second readiness-probe initial delay with a
failure threshold of 30, because a 27B model downloading ~54 GB from the Hub
genuinely needs that long. The cost of that choice showed up on the first smoke
deployment: a 0.6B model that could never become healthy still sat in
`Creating` for roughly 45 minutes before Azure gave up, and the container logs
are unavailable until it does. A wrong deployment that takes 45 minutes to
report failure is the expensive kind of wrong.

Startup is dominated by fetching and loading weights, which scales with
parameter count, so the grace period should too.
"""

from __future__ import annotations

import pytest

from ffsft.deploy.endpoint import (
    AZURE_DEFAULT_FAILURE_THRESHOLD,
    AZURE_MAX_FAILURE_THRESHOLD,
    PROBE_INITIAL_DELAY,
    probe_settings_for,
    startup_grace_for,
)

MINUTE = 60


def test_a_small_smoke_model_fails_fast():
    """0.6B loads in well under a minute; waiting 10 is pure dead time."""
    assert startup_grace_for(0.6) < 5 * MINUTE


def test_a_27b_model_still_gets_a_long_grace():
    """This is the case the original 600s was chosen for; don't regress it."""
    assert startup_grace_for(26.9) >= 10 * MINUTE


def test_unknown_size_is_treated_conservatively():
    """No size in the registry means we cannot predict load time."""
    assert startup_grace_for(None) >= 10 * MINUTE


def test_grace_never_drops_below_container_start_cost():
    """Even a tiny model has to pull the image and capture CUDA graphs."""
    assert startup_grace_for(0.1) >= 2 * MINUTE


def test_grace_is_bounded_so_a_dead_container_is_still_reported():
    """An unbounded grace period would mean a hung deploy never terminates."""
    assert startup_grace_for(700.0) <= 60 * MINUTE


@pytest.mark.parametrize(
    "smaller,larger",
    [(0.6, 4.0), (4.0, 9.0), (9.0, 26.9), (26.9, 70.0)],
)
def test_grace_is_monotonic_in_model_size(smaller, larger):
    assert startup_grace_for(smaller) <= startup_grace_for(larger)


def test_returns_whole_seconds():
    """ProbeSettings.initial_delay is an integer number of seconds."""
    assert isinstance(startup_grace_for(26.9), int)
    assert isinstance(startup_grace_for(None), int)


def test_the_budget_is_spent_on_retries_not_on_the_initial_delay():
    """The shape of the probe, which is what a 68-minute rollout bought.

    `initial_delay` is dead time: the deployment cannot be reported healthy
    before it elapses, however fast the container actually came up. Retries are
    free in the success case. So the budget belongs in `failure_threshold`, and
    `initial_delay` stays at a small constant no matter how large the model is.
    """
    small = probe_settings_for(startup_grace_for(0.6))
    large = probe_settings_for(startup_grace_for(50))

    assert small["initial_delay"] == large["initial_delay"] == PROBE_INITIAL_DELAY
    assert large["failure_threshold"] > small["failure_threshold"]


def test_the_probe_is_never_less_patient_than_azures_own_default():
    """Azure documents `failure_threshold: 30` as the default for any deployment.

    A vLLM container holding tens of gigabytes is harder to start than the
    generic case that default was picked for, so dropping below it -- which this
    module did, at 10 -- is never right.
    """
    for params in (None, 0.1, 0.6, 8, 26.9, 50, 700.0):
        settings = probe_settings_for(startup_grace_for(params))
        assert settings["failure_threshold"] >= AZURE_DEFAULT_FAILURE_THRESHOLD


def test_the_total_wait_still_covers_the_budget_the_model_size_asked_for():
    """Moving the budget must not quietly shrink it."""
    for params in (0.6, 8, 26.9, 50):
        grace = startup_grace_for(params)
        s = probe_settings_for(grace)
        assert s["initial_delay"] + s["period"] * s["failure_threshold"] >= grace


def test_in_flight_quantization_widens_the_budget():
    """vLLM reading bf16 and writing NF4 during load is work the size estimate misses.

    The per-billion figure models a download. In-flight quantisation is compute
    on the serving card, proportional to the *unquantised* checkpoint, and it
    repeats on every container start rather than being paid once.
    """
    plain = startup_grace_for(26.9)
    quantized = startup_grace_for(26.9, quantization="bitsandbytes")
    assert quantized > plain
    assert quantized <= 3600


def test_failure_threshold_never_reaches_the_value_azure_rejects():
    """Azure answers `failure_threshold >= 120` with a 400, and says so nowhere
    in the schema reference. A 27B model quantised at load asks for 125, which is
    how the ceiling was found. Sweep well past it so no budget can produce one.
    """
    for grace in range(0, 7201, 37):
        threshold = probe_settings_for(grace)["failure_threshold"]
        assert threshold < 120, f"grace={grace} produced {threshold}"
        assert threshold <= AZURE_MAX_FAILURE_THRESHOLD


def test_a_budget_past_the_ceiling_is_absorbed_by_the_period_not_the_delay():
    """The overflow has to go somewhere. Putting it in `initial_delay` would
    make every start slower by the worst case, which is the mistake JOURNAL §38
    is about; putting it in `period` only delays *noticing* a readiness that has
    already happened.
    """
    settings = probe_settings_for(3600)
    assert settings["initial_delay"] == PROBE_INITIAL_DELAY
    assert settings["period"] > 15
    assert settings["failure_threshold"] <= AZURE_MAX_FAILURE_THRESHOLD
