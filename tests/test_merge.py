"""Tests for the merge's tensor-name handling.

A 27B merge that saves the wrong names is only discovered on a LowPriority
A100, forty minutes and 54 GB later, as a vLLM `ValueError` that reads like a
model problem (docs/VERIFIED.md 49).

`text_only_state_dict` was written to prevent that and does not: `from_pretrained`
records the checkpoint->runtime renaming and `save_pretrained` replays it in
reverse, so the names the caller hands to `state_dict=` are re-mapped before
anything is written. Job `nifty_neck_d3b9x8z5x9` produced the identical
ValueError with the rename in place. The function is still correct at what it
does -- pure dict work, pinned here for nothing -- so its tests stay; what
changed is that they are no longer the tests that protect the merge.

`save_original_format=False` is the switch that decides the names, and
`assert_servable_names` is what refuses to trust that it took effect, since on
some versions the argument travels through `**kwargs` where an unrecognised
name is ignored rather than rejected.

`merge.py` keeps torch, transformers and peft inside the functions that need
them, so importing it costs no extra dependency and these tests run in the
CPU-only environment like everything else.
"""

from __future__ import annotations

from ffsft.deploy.merge import TEXT_PREFIX_FIX, text_only_state_dict


class StubModel:
    """Anything with a `state_dict()`. The real caller is a `PeftModel` merge."""

    def __init__(self, state):
        self._state = state

    def state_dict(self):
        return self._state


def test_the_multimodal_wrappers_prefix_is_stripped_from_every_tensor_name():
    # The exact names transformers writes for a Qwen3.5/3.8 text merge, and the
    # exact names vLLM's Qwen3_5Model expects once it has taken `model.` off the
    # front. The second column is what job brave_bone_2kbcknyrgr needed and did
    # not get.
    out = text_only_state_dict(
        StubModel(
            {
                "model.language_model.embed_tokens.weight": 1,
                "model.language_model.layers.0.self_attn.q_proj.weight": 2,
                "model.language_model.norm.weight": 3,
            }
        )
    )
    assert set(out) == {
        "model.embed_tokens.weight",
        "model.layers.0.self_attn.q_proj.weight",
        "model.norm.weight",
    }


def test_keys_that_never_had_the_prefix_are_left_alone():
    # `lm_head` sits outside the wrapper and must not move; a model from any
    # other architecture has no wrapper at all, which is why this is applied
    # unconditionally rather than behind a Qwen branch.
    state = {"lm_head.weight": 1, "model.layers.0.mlp.gate.weight": 2}
    assert text_only_state_dict(StubModel(state)) == state


def test_the_prefix_is_only_rewritten_where_it_leads():
    # `str.replace` would corrupt this one. The rename is anchored with
    # `startswith` for that reason.
    state = {"model.visual.model.language_model.probe": 1}
    assert text_only_state_dict(StubModel(state)) == state


def test_tensors_are_carried_through_untouched():
    tensor = object()
    out = text_only_state_dict(StubModel({"model.language_model.norm.weight": tensor}))
    assert out["model.norm.weight"] is tensor


def test_the_rename_produces_the_prefix_the_config_already_claims():
    # The config `save_pretrained` writes says `Qwen3_5ForCausalLM` /
    # `qwen3_5_text`, and vLLM builds `Qwen3_5Model` from it: `layers.*`
    # directly, reached by stripping one leading `model.`. Both halves of that
    # round trip are pinned so a future edit to either end has to face it.
    old, new = TEXT_PREFIX_FIX
    renamed = next(iter(text_only_state_dict(StubModel({old + "layers.62.linear_attn.A_log": 1}))))
    assert renamed.startswith(new)
    assert renamed[len(new) :] == "layers.62.linear_attn.A_log"


# --------------------------------------------------------------------------
# what actually protects the merge
# --------------------------------------------------------------------------


def _write_checkpoint(tmp_path, names, *, vision=False, sharded=True):
    """A checkpoint with the right shape and no tensors worth speaking of."""
    import json
    import struct

    config = {"architectures": ["Qwen3_5ForCausalLM"]}
    if vision:
        config["vision_config"] = {}
    (tmp_path / "config.json").write_text(json.dumps(config))
    if sharded:
        (tmp_path / "model.safetensors.index.json").write_text(
            json.dumps({"weight_map": {name: "model-00001-of-00001.safetensors" for name in names}})
        )
        return tmp_path

    entry = {"dtype": "BF16", "shape": [1], "data_offsets": [0, 2]}
    header = json.dumps({name: entry for name in names})
    blob = header.encode()
    (tmp_path / "model.safetensors").write_bytes(struct.pack("<Q", len(blob)) + blob + b"\x00\x00")
    return tmp_path


def test_names_come_from_the_shard_index_when_there_is_one(tmp_path):
    """The index is already the full list; opening 14 shards to rebuild it is waste."""
    from ffsft.deploy.merge import saved_tensor_names

    _write_checkpoint(tmp_path, ["model.layers.0.mlp.down_proj.weight", "lm_head.weight"])

    assert saved_tensor_names(str(tmp_path)) == [
        "lm_head.weight",
        "model.layers.0.mlp.down_proj.weight",
    ]


def test_names_come_from_the_safetensors_header_when_there_is_no_index(tmp_path):
    """A small merge saves one file and no index, and must still be checkable.

    The header is eight little-endian bytes of length followed by that many
    bytes of JSON, so this reads kilobytes rather than the whole checkpoint.
    """
    from ffsft.deploy.merge import saved_tensor_names

    _write_checkpoint(tmp_path, ["model.layers.0.self_attn.o_proj.weight"], sharded=False)

    assert saved_tensor_names(str(tmp_path)) == ["model.layers.0.self_attn.o_proj.weight"]


def test_a_text_only_config_over_multimodal_names_is_refused(tmp_path):
    """The regression pin for purple_wolf / brave_bone / nifty_neck.

    Three jobs died on this contradiction, the last one after a fix that was
    believed to close it. Catching it here turns a 41-minute A100 failure into
    a merge-job failure that names its own cause.
    """
    import pytest

    from ffsft.deploy.merge import assert_servable_names

    _write_checkpoint(tmp_path, ["model.language_model.layers.0.linear_attn.A_log"])

    with pytest.raises(RuntimeError) as excinfo:
        assert_servable_names(str(tmp_path))

    message = str(excinfo.value)
    assert "save_original_format=False" in message
    assert "model.language_model.layers.0.linear_attn.A_log" in message


def test_multimodal_names_are_fine_when_the_config_declares_a_vision_tower(tmp_path):
    """Not a name blacklist -- a consistency check.

    `language_model.` is the correct name in a checkpoint that really does carry
    a vision tower. Refusing it there would break the one case it is right for.
    """
    from ffsft.deploy.merge import assert_servable_names

    _write_checkpoint(tmp_path, ["model.language_model.layers.0.mlp.down_proj.weight"], vision=True)

    assert assert_servable_names(str(tmp_path)) == 1


def test_a_clean_text_only_checkpoint_passes(tmp_path):
    from ffsft.deploy.merge import assert_servable_names

    names = [f"model.layers.{i}.mlp.down_proj.weight" for i in range(4)] + ["lm_head.weight"]
    _write_checkpoint(tmp_path, names)

    assert assert_servable_names(str(tmp_path)) == 5
