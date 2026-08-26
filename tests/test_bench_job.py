"""The in-job load test: what has to be true before an A100 node is allocated.

Every assertion here stands for a failure that is invisible until the node is
running and expensive once it is. A LowPriority A100 takes tens of minutes to
allocate and the weights take minutes more to load, so a wrong environment key
or an unsubstituted placeholder costs most of an hour to discover and produces
a job that looks like it ran.

The load test lives in a job rather than behind an endpoint because this
subscription cannot create a GPU online deployment at all -- see
`bench_job`'s module docstring and docs/JOURNAL.md §40.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ffsft.models import get_model
from ffsft.serve.bench_job import (
    BENCH_ENVIRONMENT_VERSION,
    BENCH_IMAGE,
    BenchSpec,
    bench_env,
    build_command,
    check_serving_fits,
)

REPO = Path(__file__).resolve().parents[1]
ENTRYPOINT = REPO / "docker" / "bench_entrypoint.sh"

#: Read by the entrypoint, never written by a job. See the drift test below.
TEST_ONLY_HOOKS = {"BENCH_SERVE_CMD"}


def spec(**kw) -> BenchSpec:
    return BenchSpec(model_asset="qwen3_8-27b-ko-merged:1", **kw)


# --------------------------------------------------------------- wiring ----


def test_the_two_paths_azure_substitutes_are_in_the_command_not_the_environment():
    """`${{...}}` is expanded in the command string and nowhere else.

    Azure ML substitutes input and output bindings when it builds the shell
    line. An `environment_variables` entry containing the same text is passed
    through verbatim, so `MODEL_PATH` set there arrives at the node spelled
    `${{inputs.model}}` and vLLM tries to load a model from a directory of that
    name. The failure surfaces after allocation, as a path error.
    """
    cmd = build_command(spec())
    assert "${{inputs.model}}" in cmd
    assert "${{outputs.bench}}" in cmd

    env = bench_env(spec(), get_model("qwen3.8-27b"))
    assert "MODEL_PATH" not in env
    assert "OUTPUT_DIR" not in env
    for value in env.values():
        assert "${{" not in value


def test_every_variable_the_entrypoint_reads_is_one_the_job_sets():
    """The shell script and the Python that configures it cannot drift apart.

    `bench_entrypoint.sh` reads its configuration from the environment. Adding a
    knob there without adding it here yields a job that silently runs the
    default; renaming one yields a job that silently runs the default. Neither
    is an error at any layer -- the sweep completes and reports numbers for a
    configuration nobody asked for.

    Only `BENCH_*` is checked. The serving variables come from the image's own
    ENV defaults by design, so absence there is a fallback rather than a bug.

    `BENCH_SERVE_CMD` is the one variable the job must *not* set. It replaces
    the server the bench measures, and it exists so the shell can be exercised
    against a mock on a laptop instead of on a LowPriority A100. A job that
    could set it is a job that could publish latency numbers for something other
    than vLLM, so it is asserted absent rather than merely unlisted.
    """
    text = ENTRYPOINT.read_text(encoding="utf-8")
    read = set(re.findall(r"\$\{(BENCH_[A-Z_]+)(?::-[^}]*)?\}", text))
    assert read, "no BENCH_* variables found -- did the entrypoint move?"

    emitted = set(bench_env(spec(), get_model("qwen3.8-27b")))
    assert TEST_ONLY_HOOKS <= read, "the mock-server hook vanished from the entrypoint"
    assert not (TEST_ONLY_HOOKS & emitted), "a job must never redirect the bench off vLLM"

    missing = read - emitted - TEST_ONLY_HOOKS
    assert not missing, f"entrypoint reads {sorted(missing)}, job sets none of them"


def test_the_entrypoint_refuses_to_start_without_the_two_bound_paths():
    """Both paths are required arguments, not defaulted and not environment.

    A default for either would turn a broken binding into a job that serves the
    wrong thing or writes its report where the upload cannot find it. Only
    declared outputs survive the node, so a report written elsewhere is a report
    that does not exist.
    """
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert 'MODEL_ROOT="${1:?' in text
    assert 'OUTPUT_DIR="${2:?' in text


def test_the_model_path_is_an_argument_and_never_an_environment_prefix():
    """Regression for job `sharp_date_dcg59pbtt5` (docs/JOURNAL.md §42).

    The command used to read `MODEL_PATH="${{inputs.model}}" bash ...`. The
    bench image is FROM the serve image, which bakes
    `ENV MODEL_PATH=/var/azureml-app/azureml-models`, and that is the path vLLM
    was launched with -- the run died five minutes in with an HFValidationError
    from transformers, because a path that does not exist gets retried as a
    Hugging Face repo id. An argument cannot be shadowed by an image ENV.
    """
    cmd = build_command(spec())
    assert cmd.startswith("bash /usr/local/bin/bench_entrypoint.sh ")
    assert '"${{inputs.model}}"' in cmd
    assert '"${{outputs.bench}}"' in cmd
    assert "MODEL_PATH=" not in cmd, "an env prefix loses to the image's own ENV"
    assert "OUTPUT_DIR=" not in cmd


def test_the_entrypoint_resolves_the_model_directory_before_starting_vllm():
    """Fail on the path, not five minutes later inside vLLM's config loader.

    `serve_entrypoint.sh`'s resolve_model() searches -maxdepth 4 and silently
    falls back to treating its input as a Hub repo id. Azure ML nests a
    registered model as <root>/<name>/<version>/... and this asset adds its own
    azureml/<run-id>/merged/, which is deeper than 4. The bench resolves it
    itself, and when it cannot it prints the directory instead of guessing.
    """
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert "-maxdepth 8 -name config.json" in text
    assert "FATAL: no config.json under" in text
    resolve = text.index("-maxdepth 8 -name config.json")
    start = text.index("starting server:")
    assert resolve < start, "the model must be resolved before vLLM is launched"


def test_the_measurements_are_echoed_to_stdout_as_well_as_the_output_dir():
    """A second copy for whoever can read the logs, which is not this account.

    Workspace storage has publicNetworkAccess disabled: a direct read and a
    service-issued SAS link both return 403 AuthorizationFailure. The stream is
    *not* the way out either -- Azure ML serves `user_logs/std_log.txt` from
    that same storage, so `jobs.stream` on job helpful_jelly_gndv8d135q
    delivered RunId, Web View and Execution Summary and nothing the job printed.
    This echo is therefore for a reader inside the VNet or in the portal; the
    channel that actually reaches this workstation is MLflow, asserted below.
    """
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert 'cat "${OUTPUT_DIR}/loadtest.json"' in text


def test_the_numbers_leave_the_node_over_mlflow_because_every_blob_path_is_403():
    """The one channel with no blob in it. Without this the job measures and
    then loses the measurement, which is indistinguishable from not running."""
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert "from ffsft.serve.bench_report import main" in text
    assert '--output-dir "${OUTPUT_DIR}"' in text
    assert '--status "${RUN_PHASE}"' in text
    assert '--vllm-log "${VLLM_LOG}"' in text


def test_the_report_goes_out_on_exit_paths_that_precede_the_server():
    """`FATAL: no config.json` is the failure most worth reporting and the one
    that fires earliest. A trap installed next to the server start would miss
    it, and the run would fail for a reason readable only from inside the VNet."""
    text = ENTRYPOINT.read_text(encoding="utf-8")
    trap = text.index("trap cleanup EXIT")
    assert trap < text.index("FATAL: no config.json")
    assert trap < text.index("SERVER_PID=$!")
    # The trap fires before there is a pid, so it must not assume one.
    assert '[[ -n "${SERVER_PID:-}" ]]' in text
    assert "    report\n}" in text


def test_the_status_names_the_phase_so_a_failure_says_where_it_died():
    """`build_report` also keys the vllm.log tail off `swept`: on any other
    status the tail is the failure reason, on that one it is throughput noise."""
    text = ENTRYPOINT.read_text(encoding="utf-8")
    for phase in ("resolving_model", "model_not_found", "waiting_for_health",
                  "health_timeout", "smoke", "sweeping", "swept"):
        assert f'RUN_PHASE="{phase}' in text


def test_the_report_is_written_into_the_declared_output():
    """Anything the node writes outside a declared output dies with the node."""
    assert spec().declared_outputs() == {"bench"}
    text = ENTRYPOINT.read_text(encoding="utf-8")
    for artefact in ("loadtest.json", "smoke.json", "vllm.log"):
        assert f'${{OUTPUT_DIR}}/{artefact}"' in text or f"${{OUTPUT_DIR}}/{artefact}" in text


# ------------------------------------------------------ served-model flags ----


def test_the_server_under_test_gets_the_architecture_flags_the_registry_declares():
    """The bench must measure the server we intend to ship, not a plainer one.

    Qwen3.8-27B is 48 Gated-DeltaNet layers plus a vision tower it does not need
    for Korean text. Without `--mamba-cache-mode align` vLLM raises
    NotImplementedError; without `--language-model-only` the tower costs VRAM
    that the KV cache needed; without `--reasoning-parser qwen3` the `<think>`
    block lands in `content` and inflates every measured output-token count.
    """
    env = bench_env(spec(), get_model("qwen3.8-27b"))
    assert env["MAMBA_CACHE_MODE"] == "align"
    assert env["LANGUAGE_MODEL_ONLY"] == "1"
    assert env["REASONING_PARSER"] == "qwen3"


def test_a_plain_dense_model_gets_none_of_them():
    """Neutral values are emitted, not omitted.

    The image carries ENV defaults, so an omitted key inherits whatever it was
    built with. That is how a dense text-only smoke model was once launched with
    Qwen3.8's hybrid-attention flags.
    """
    env = bench_env(spec(model_key="qwen3.5-0.8b"), get_model("qwen3.5-0.8b"))
    assert env["MAMBA_CACHE_MODE"] == ""
    assert env["REASONING_PARSER"] == ""
    assert env["LANGUAGE_MODEL_ONLY"] == "0"


def test_bf16_is_the_default_because_the_card_is_80gb():
    """No QUANTIZATION key at all, rather than an empty one.

    Quantising is what a 24 GB A10 forced. On an 80 GB A100 the merged bf16
    checkpoint fits with room to spare, and measuring the quantised model would
    report latencies for weights nobody serves.
    """
    assert "QUANTIZATION" not in bench_env(spec(), get_model("qwen3.8-27b"))
    assert bench_env(spec(quantization="bitsandbytes"), get_model("qwen3.8-27b"))[
        "QUANTIZATION"
    ] == "bitsandbytes"


def test_the_batch_ceiling_reaches_vllm():
    """GDN state is allocated per sequence slot up front, so this is VRAM, not a limit."""
    assert "--max-num-seqs 16" in bench_env(spec(), get_model("qwen3.8-27b"))["EXTRA_ARGS"]
    env = bench_env(spec(max_num_seqs=4, extra_args="--enforce-eager"), get_model("qwen3.8-27b"))
    assert env["EXTRA_ARGS"] == "--max-num-seqs 4 --enforce-eager"


def test_the_sweep_goes_one_level_past_the_batch_so_queueing_is_measured():
    """A sweep that stops at the batch ceiling never observes a queue.

    `max_num_seqs` slots is where the batch saturates; the level above it is the
    only one that measures what waiting behind a full batch costs, which is the
    number a capacity plan actually needs.
    """
    s = spec()
    levels = s.concurrency_levels()
    assert s.max_num_seqs in levels
    assert max(levels) > s.max_num_seqs


def test_a_level_deeper_than_the_request_count_is_refused_not_mislabelled():
    """The failure mode this guards is a wrong number, not a crash.

    `loadtest.run_level` drives `requests_per_level` requests through a
    semaphore of `concurrency`. Fewer requests than the level means the level is
    never reached -- 16 requests at concurrency 32 puts 16 in flight and prints
    the row as 32. Nothing errors, the table looks complete, and the top of the
    sweep is a measurement of a different load than its label claims.
    """
    with pytest.raises(ValueError, match="below the highest concurrency level"):
        spec(concurrency="1,8,32", requests_per_level=16)


def test_the_default_sweep_runs_two_full_waves_at_the_top_level():
    s = spec()
    assert s.requests_per_level >= 2 * max(s.concurrency_levels())


def test_an_empty_sweep_is_refused():
    with pytest.raises(ValueError, match="nothing to sweep"):
        spec(concurrency=" ")


# --------------------------------------------------------------- sizing ----


def test_the_merged_27b_fits_the_a100_the_cluster_actually_has():
    fits, why = check_serving_fits(
        get_model("qwen3.8-27b"), "Standard_NC24ads_A100_v4", 0.90, None
    )
    assert fits
    assert "53.8 GB" in why


def test_the_a10_that_could_not_be_deployed_would_not_have_held_it_anyway():
    """Separate from why the deployment failed, and worth pinning.

    The A10 rollouts never got a node, so bf16 sizing was never the binding
    constraint -- but it was a real one underneath, and `--quantization
    bitsandbytes` was there to work around it. Nothing about moving to a job
    should make it possible to ask a 24 GB card for 54 GB of weights.
    """
    fits, why = check_serving_fits(
        get_model("qwen3.8-27b"), "Standard_NV36ads_A10_v5", 0.95, None
    )
    assert not fits
    assert "do not fit" in why


def test_quantisation_opts_out_of_bf16_sizing_rather_than_failing_it():
    fits, _ = check_serving_fits(
        get_model("qwen3.8-27b"), "Standard_NV36ads_A10_v5", 0.95, "bitsandbytes"
    )
    assert fits


def test_an_unknown_sku_is_not_a_refusal():
    """"Cannot check" and "does not fit" are different findings.

    Refusing on an absent table entry would make every new SKU a code change
    before it could be tried, which is how a sizing table becomes the reason a
    working configuration is unavailable.
    """
    fits, why = check_serving_fits(get_model("qwen3.8-27b"), "Standard_Nonsense_v9", 0.9, None)
    assert fits
    assert "cannot verify" in why


def test_a_model_with_no_declared_size_is_not_a_refusal():
    m = get_model("qwen3.8-27b").model_copy(update={"params_b": None})
    fits, why = check_serving_fits(m, "Standard_NC24ads_A100_v4", 0.9, None)
    assert fits
    assert "cannot verify" in why


# ---------------------------------------------------------- environment ----


def test_the_environment_version_tracks_the_image_tag():
    """The code is inside the image, so a code change is an image change.

    Typing the version by hand is how a job runs last week's script under this
    week's name; `plum_station_dxwtzlz94q` is what that cost on the training
    side.
    """
    assert BENCH_IMAGE.endswith(f":{BENCH_ENVIRONMENT_VERSION}")


def test_an_untagged_bench_image_cannot_produce_a_version():
    from ffsft.train.aml_job import image_tag

    with pytest.raises(ValueError, match="carries no tag"):
        image_tag("acrffsftkc.azurecr.io/ffsft-bench")
