"""The serving entrypoint has to survive both ways a model can arrive.

`docker/serve_entrypoint.sh` runs under `set -euo pipefail`, and `resolve_model`
runs `find` against `$MODEL_PATH`. That is correct when Azure ML has mounted a
registered model, but `MODEL_PATH` is *also* allowed to be a bare Hugging Face
repo id -- that is the documented fallback for a workspace whose storage account
is network-isolated, and it is what every smoke deployment so far has used.

`find Qwen/Qwen3-0.6B` exits 1 because there is no such directory. Under
`pipefail` the whole pipeline exits 1, and a failing assignment under `set -e`
terminates the shell:

    $ bash -c 'set -euo pipefail; x="$(find nope -name c 2>/dev/null | head -1)"; echo alive'
    $ echo $?
    1                       # "alive" never printed

The script survives today only because that assignment sits inside a function
body invoked from a command substitution, which is one of the contexts where
bash stops honouring `set -e`. That is an accident of where the code happens to
sit, not a decision: inlining `resolve_model` -- an obvious tidy-up -- would
turn every Hub-backed deployment into a container that exits before vLLM starts,
and Azure withholds container logs until a deployment reaches a terminal state,
so it would present as an unexplained hour in `Creating`.

These tests run the real script with the real environment variables, so the
behaviour is pinned regardless of how the internals are arranged later.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = REPO_ROOT / "docker" / "serve_entrypoint.sh"

#: Exactly the keys `Dockerfile.serve` defines. `set -u` means a missing one is
#: an immediate crash, so the test asserts the image's contract too.
IMAGE_DEFAULTS = {
    "SERVED_MODEL_NAME": "ffsft",
    "MAX_MODEL_LEN": "8192",
    "GPU_MEMORY_UTILIZATION": "0.90",
    "TENSOR_PARALLEL_SIZE": "1",
    "VLLM_PORT": "8000",
    "MAMBA_CACHE_MODE": "",
    "LANGUAGE_MODEL_ONLY": "0",
    "REASONING_PARSER": "",
    "QUANTIZATION": "",
    "ENABLE_LORA": "0",
    "LORA_MODULES": "",
    "MAX_LORA_RANK": "16",
    "EXTRA_ARGS": "",
    "MODEL_BLOB_URI": "",
    "MODEL_CACHE_DIR": "/tmp/ffsft-model",
    "MODEL_FETCH_WORKERS": "16",
}

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="entrypoint is a bash script"
)


def run_entrypoint(tmp_path: Path, **overrides) -> subprocess.CompletedProcess:
    """Execute the real entrypoint with the final `exec` replaced by an echo.

    Replacing only the last line keeps every argument-assembly decision under
    test while stopping short of needing a GPU or the vLLM package.
    """
    source = ENTRYPOINT.read_text(encoding="utf-8").replace("\r\n", "\n")
    lines = source.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("exec python3 -m vllm"):
            lines[i] = 'echo "ARGV: ${ARGS[*]}"'
            break
    else:  # pragma: no cover - the script always ends in that exec
        pytest.fail("entrypoint no longer ends in the expected exec line")

    script = tmp_path / "ep.sh"
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")

    env = dict(IMAGE_DEFAULTS)
    env.update({k: str(v) for k, v in overrides.items()})
    return subprocess.run(
        ["bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def argv_of(result: subprocess.CompletedProcess) -> list[str]:
    for line in result.stdout.splitlines():
        if line.startswith("ARGV: "):
            return line[len("ARGV: ") :].split()
    raise AssertionError(
        f"entrypoint never reached exec.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


# --- the regression this file exists for -----------------------------------


def test_a_hub_repo_id_does_not_kill_the_container(tmp_path):
    """`MODEL_PATH` as a repo id must reach vLLM, not exit the shell."""
    result = run_entrypoint(tmp_path, MODEL_PATH="Qwen/Qwen3-0.6B")
    assert result.returncode == 0, result.stderr
    argv = argv_of(result)
    assert argv[argv.index("--model") + 1] == "Qwen/Qwen3-0.6B"


def test_a_repo_id_that_looks_like_a_path_is_still_passed_through(tmp_path):
    """Org/name always contains a slash; that must not be read as a directory."""
    result = run_entrypoint(tmp_path, MODEL_PATH="meta-llama/Llama-3.1-8B-Instruct")
    assert result.returncode == 0, result.stderr
    assert "meta-llama/Llama-3.1-8B-Instruct" in argv_of(result)


# --- the mounted-model path it was originally written for ------------------


def test_a_mounted_model_is_found_at_the_mount_root(tmp_path):
    mount = tmp_path / "mnt"
    mount.mkdir()
    (mount / "config.json").write_text("{}", encoding="utf-8")
    result = run_entrypoint(tmp_path, MODEL_PATH=str(mount))
    assert result.returncode == 0, result.stderr
    assert str(mount) in argv_of(result)


def test_a_model_nested_the_way_azure_ml_nests_it_is_found(tmp_path):
    """Azure ML mounts a registered model as <mount>/<name>/<version>/."""
    nested = tmp_path / "mnt" / "qwen-ko" / "1"
    nested.mkdir(parents=True)
    (nested / "config.json").write_text("{}", encoding="utf-8")
    result = run_entrypoint(tmp_path, MODEL_PATH=str(tmp_path / "mnt"))
    assert result.returncode == 0, result.stderr
    assert str(nested) in argv_of(result)


def test_the_shallowest_config_wins(tmp_path):
    """A checkpoint containing a sub-model must not shadow the real root."""
    root = tmp_path / "mnt" / "model"
    deep = root / "vision_tower"
    deep.mkdir(parents=True)
    (root / "config.json").write_text("{}", encoding="utf-8")
    (deep / "config.json").write_text("{}", encoding="utf-8")
    result = run_entrypoint(tmp_path, MODEL_PATH=str(tmp_path / "mnt"))
    assert result.returncode == 0, result.stderr
    assert str(root) in argv_of(result)


# --- neutral values must produce no flag, not an empty one -----------------


def test_neutral_architecture_values_emit_no_flags(tmp_path):
    """An empty MAMBA_CACHE_MODE must not become `--mamba-cache-mode ''`."""
    result = run_entrypoint(tmp_path, MODEL_PATH="Qwen/Qwen3-0.6B")
    argv = argv_of(result)
    assert "--mamba-cache-mode" not in argv
    assert "--language-model-only" not in argv
    assert "--reasoning-parser" not in argv
    assert "--quantization" not in argv
    assert "--enable-lora" not in argv


def test_qwen38_style_values_do_emit_their_flags(tmp_path):
    """The flags still have to work for the model they were added for."""
    result = run_entrypoint(
        tmp_path,
        MODEL_PATH="Qwen/Qwen3.8-27B",
        MAMBA_CACHE_MODE="align",
        LANGUAGE_MODEL_ONLY="1",
        REASONING_PARSER="qwen3",
    )
    argv = argv_of(result)
    assert argv[argv.index("--mamba-cache-mode") + 1] == "align"
    assert "--language-model-only" in argv
    assert argv[argv.index("--reasoning-parser") + 1] == "qwen3"


def test_the_probe_port_matches_what_the_deployment_declares(tmp_path):
    """inference_config probes :8000; a mismatch fails every readiness check."""
    result = run_entrypoint(tmp_path, MODEL_PATH="Qwen/Qwen3-0.6B")
    argv = argv_of(result)
    assert argv[argv.index("--port") + 1] == "8000"


# --- the blob fetch stage --------------------------------------------------
#
# Model assets cannot be registered on this tenant (management-group policy
# `MCAPSGovDeployPolicies` disables shared-key auth, and Azure ML's Model
# Registry enumerates blobs with an account key), so a fine-tuned deployment
# fetches its own weights with the endpoint's managed identity. These tests pin
# the two things that matter about that stage: it must not run when it was not
# asked for, and when it fails the container must die rather than quietly serve
# the base model.


def _fake_python3(tmp_path: Path, exit_code: int) -> Path:
    """A `python3` earlier in PATH than the real one.

    Stubbing the interpreter rather than parameterising the script's path keeps
    the real `python3 /usr/local/bin/fetch_model.py` line under test -- a test
    that edits the command it is checking proves nothing about the image.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    shim = bindir / "python3"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "[fetch] stub invoked: $*"\nexit {exit_code}\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return bindir


def test_no_blob_uri_means_the_fetch_stage_never_runs(tmp_path):
    """The default deployment style must be untouched by the new branch."""
    bindir = _fake_python3(tmp_path, exit_code=1)  # would fail loudly if called
    result = run_entrypoint(
        tmp_path,
        MODEL_PATH="Qwen/Qwen3-0.6B",
        PATH=f"{bindir}:/usr/bin:/bin",
    )
    assert result.returncode == 0, result.stderr
    assert "stub invoked" not in result.stdout
    assert argv_of(result)[argv_of(result).index("--model") + 1] == "Qwen/Qwen3-0.6B"


def test_a_successful_fetch_serves_the_downloaded_directory(tmp_path):
    cache = tmp_path / "dl"
    cache.mkdir()
    (cache / "config.json").write_text("{}", encoding="utf-8")
    bindir = _fake_python3(tmp_path, exit_code=0)
    result = run_entrypoint(
        tmp_path,
        MODEL_PATH="Qwen/Qwen3.8-27B",
        MODEL_BLOB_URI="https://acct.blob.core.windows.net/c/azureml/run/merged/",
        MODEL_CACHE_DIR=str(cache),
        PATH=f"{bindir}:/usr/bin:/bin",
    )
    assert result.returncode == 0, result.stderr
    assert "stub invoked" in result.stdout
    # The downloaded directory wins over the Hub id that MODEL_PATH still holds.
    assert str(cache) in argv_of(result)


def test_a_failed_fetch_kills_the_container(tmp_path):
    """The regression this guard exists for: no silent fallback to the base model.

    Without it, a failed download would fall through to `resolve_model`, which
    would find no local checkpoint and hand vLLM the bare repo id -- an endpoint
    that passes every health probe and every load test while serving weights
    that were never fine-tuned.
    """
    bindir = _fake_python3(tmp_path, exit_code=1)
    result = run_entrypoint(
        tmp_path,
        MODEL_PATH="Qwen/Qwen3.8-27B",
        MODEL_BLOB_URI="https://acct.blob.core.windows.net/c/azureml/run/merged/",
        MODEL_CACHE_DIR=str(tmp_path / "never"),
        PATH=f"{bindir}:/usr/bin:/bin",
    )
    assert result.returncode != 0, "a failed fetch must not reach vLLM"
    assert "ARGV:" not in result.stdout
