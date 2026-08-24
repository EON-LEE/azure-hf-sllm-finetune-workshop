"""The third false green: a bumped image that the job never runs.

`TRAIN_IMAGE` and `ENVIRONMENT_VERSION` are two constants that must move
together, and until now the only thing coupling them was a comment saying so.
On 2026-08-24 that comment lost. `TRAIN_IMAGE` was bumped to `ffsft-train:10`
carrying the `trust_remote_code` fix, `ENVIRONMENT_VERSION` was left at `"9"`,
and `ensure_environment` found version 9 already registered and returned it:

    version 9 -> acrffsftkc.azurecr.io/ffsft-train:9      <- what ran
    version 8 -> acrffsftkc.azurecr.io/ffsft-train:8

Job `plum_station_dxwtzlz94q` was submitted against `ffsft-train:9`, allocated
an A100, pulled nine gigabytes, and died with the *identical* error the fix had
already repaired:

    ValueError: The repository kakaocorp/kanana-2-1.3b-instruct contains custom
    code ... Please pass the argument `trust_remote_code=True`

Nothing was learned and the GPU minutes were spent. An Azure ML environment is
immutable per version, so `create_or_update` on an existing version is a silent
no-op rather than an update -- which makes "I bumped the image" and "the node
runs the new image" two independent facts unless something enforces otherwise.

Two defects, and both are pinned here:

* the version was allowed to drift from the tag -- so derive it, don't repeat it;
* `ensure_environment` returned an existing registration without ever looking at
  the image inside it -- so a stale version is indistinguishable from a correct
  one. It must refuse instead, because a refusal costs nothing and the
  alternative costs a GPU run.
"""

from __future__ import annotations

import pytest

from ffsft.train import aml_job
from ffsft.train.aml_job import (
    ENVIRONMENT_NAME,
    ENVIRONMENT_VERSION,
    TRAIN_IMAGE,
    ensure_environment,
    image_tag,
)

# --------------------------------------------------------------------------
# image_tag
# --------------------------------------------------------------------------


def test_image_tag_reads_the_tag():
    assert image_tag("acrffsftkc.azurecr.io/ffsft-train:10") == "10"


def test_image_tag_ignores_a_port_in_the_registry_host():
    """`host:5000/img:3` has two colons and only the last one is the tag.

    Splitting on the first colon would name the environment '5000/img' and
    register something nobody asked for.
    """
    assert image_tag("localhost:5000/ffsft-train:3") == "3"


def test_image_tag_refuses_an_untagged_image():
    """An untagged reference means `:latest`, which is a moving target.

    A mutable tag reintroduces exactly the bug this module is trying to close:
    the registration would stay valid while the bytes underneath it change.
    """
    with pytest.raises(ValueError, match="tag"):
        image_tag("acrffsftkc.azurecr.io/ffsft-train")


def test_image_tag_refuses_a_digest_pinned_reference_as_a_version():
    """A digest is a fine way to pin an image and a terrible environment version.

    Azure ML version strings cannot carry `sha256:...`, so this has to be caught
    here rather than as a 400 from the service.
    """
    with pytest.raises(ValueError, match="tag"):
        image_tag("acrffsftkc.azurecr.io/ffsft-train@sha256:376f6024")


# --------------------------------------------------------------------------
# the constant that drifted
# --------------------------------------------------------------------------


def test_environment_version_tracks_the_image_tag():
    """The regression pin for `plum_station_dxwtzlz94q`.

    If these two are ever allowed to disagree again, the next training run
    silently executes whichever image the older version points at.
    """
    assert ENVIRONMENT_VERSION == image_tag(TRAIN_IMAGE)


# --------------------------------------------------------------------------
# ensure_environment
# --------------------------------------------------------------------------


class FakeEnv:
    def __init__(self, name, version, image):
        self.name, self.version, self.image = name, version, image


class FakeEnvOps:
    """Mimics the immutability that caused the incident.

    `create_or_update` on a version that already exists returns the *stored*
    entity, it does not overwrite it -- which is precisely why registering over
    a stale version cannot be the fix.
    """

    def __init__(self, existing=None):
        self.store = {(e.name, e.version): e for e in (existing or [])}
        self.created: list[FakeEnv] = []

    def get(self, name, version=None):
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return self.store[(name, version)]
        except KeyError:
            raise ResourceNotFoundError(f"{name}:{version}") from None

    def create_or_update(self, env):
        self.created.append(env)
        key = (env.name, env.version)
        if key in self.store:
            return self.store[key]
        stored = FakeEnv(env.name, env.version, env.image)
        self.store[key] = stored
        return stored


class FakeClient:
    def __init__(self, env_ops):
        self.environments = env_ops


def test_registers_the_current_image_when_nothing_exists():
    ops = FakeEnvOps()
    ref = ensure_environment(FakeClient(ops))

    assert ref == f"{ENVIRONMENT_NAME}:{ENVIRONMENT_VERSION}"
    assert len(ops.created) == 1
    assert ops.created[0].image == TRAIN_IMAGE
    assert ops.created[0].version == image_tag(TRAIN_IMAGE)


def test_reuses_an_existing_registration_that_holds_the_right_image():
    """The happy path must stay free -- one GET, no write."""
    ops = FakeEnvOps([FakeEnv(ENVIRONMENT_NAME, ENVIRONMENT_VERSION, TRAIN_IMAGE)])

    assert ensure_environment(FakeClient(ops)) == f"{ENVIRONMENT_NAME}:{ENVIRONMENT_VERSION}"
    assert ops.created == []


def test_refuses_an_existing_registration_that_holds_a_different_image():
    """The check that would have saved `plum_station_dxwtzlz94q`.

    Same version, different image. Returning it hands the job an image the
    caller never asked for, and the mismatch only surfaces after a node has been
    allocated -- so fail here, where it is free.
    """
    stale = FakeEnv(ENVIRONMENT_NAME, ENVIRONMENT_VERSION, "acrffsftkc.azurecr.io/ffsft-train:9")
    ops = FakeEnvOps([stale])

    with pytest.raises(RuntimeError) as excinfo:
        ensure_environment(FakeClient(ops))

    message = str(excinfo.value)
    assert "ffsft-train:9" in message
    assert TRAIN_IMAGE in message
    assert ops.created == []


def test_the_refusal_names_the_way_out(monkeypatch):
    """A bare mismatch error would send the reader to the SDK docs.

    The only remedy is a new tag, because the registered version cannot be
    rewritten, so the message has to say that.
    """
    monkeypatch.setattr(aml_job, "TRAIN_IMAGE", "acrffsftkc.azurecr.io/ffsft-train:11")
    monkeypatch.setattr(aml_job, "ENVIRONMENT_VERSION", "11")
    stale = FakeEnv(ENVIRONMENT_NAME, "11", "acrffsftkc.azurecr.io/ffsft-train:9")

    with pytest.raises(RuntimeError, match="immutable"):
        ensure_environment(FakeClient(FakeEnvOps([stale])))
