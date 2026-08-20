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

from ffsft.deploy.endpoint import startup_grace_for

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
    assert startup_grace_for(700.0) <= 30 * MINUTE


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
