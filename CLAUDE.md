# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

`uv` drives everything. It installs to `~/.local/bin`, which is not on the default PATH:

```bash
export PATH="$HOME/.local/bin:$PATH"
uv sync --extra dev          # dev extra is enough for the whole test suite
uv run pytest                # 700 tests, ~9s, no network and no Azure calls
uv run pytest tests/test_aml_job.py::test_preflight_runs_the_self_test_and_nothing_else
uv run pytest -k lora        # substring selection
uv run ruff check .          # line-length 100, target py310, rules E/F/I/UP/B
```

Extras are split so the CPU-only half stays installable without CUDA: `data`, `train`
(torch/transformers/trl/peft/bitsandbytes), `azure`, `eval` (lm-eval), `serve` (httpx only —
no GPU deps on purpose), `dev`. `requires-python = ">=3.10"` is a floor set by Azure ML's
ACPT images, which ship 3.10; do not use 3.11+ syntax.

### Entry points

`ffsft` is the single entry point. Two kinds of command live under it.

**Registry inspection** — reads `configs/*.yaml`, no heavyweight deps:

```bash
uv run ffsft models list --commercial-only
uv run ffsft models show qwen3.8-27b
uv run ffsft serving list / serving show / serving adapter-modes
uv run ffsft bench list / bench suites
```

**Delegates** — `ffsft train submit | merge submit | merge local | eval | deploy |
lifecycle | loadtest | serve-local` swap `sys.argv` and call the same `main()` the
console script calls, so the two names cannot drift (`tests/test_cli_delegates.py`
reads `[project.scripts]` and fails if a script appears with no `ffsft` command
reaching it). **`cli.py` must import with only the registry deps present** — every
delegating import is function-local, pinned by an ast walk over the module body.
`_COMMAND_ORDER` sorts `--help` by lab, not by Click's collection order.

The console scripts all still work, and the labs use the short names:

```bash
python -m ffsft.train.preflight                  # node self-test, cheapest way to prove a cluster
python -m ffsft.train.qlora --model ... --mix ...
uv run ffsft-eval      # base vs tuned delta
uv run ffsft-merge     # LoRA -> merged bf16 weights
uv run ffsft-deploy    check | deploy-online | deploy-batch
uv run ffsft-lifecycle status | up | down [--all] [--yes]
uv run ffsft-serve-local                         # CPU transformers, OpenAI-compatible + SSE
uv run ffsft-loadtest                            # TTFT / TPOT / p50-p95-p99, knee point
```

`ffsft-serve-local` plus `ffsft-loadtest` (or `scripts/mock_vllm_server.py`) exercise the
entire serving half with no GPU and no Azure.

### Free checks worth running before spending money

```bash
uv run python scripts/verify_hf_ids.py                 # every hf_id in configs/ against the HF API
uv run python scripts/probe_architecture.py qwen3.8-27b --check   # declared vs actual LoRA targets
uv run ffsft-deploy check --probe                      # real create calls, min=0, deleted after
```

Both scripts exit non-zero on mismatch so they can gate CI. `probe_architecture.py` instantiates
on meta device — no weights are downloaded.

### Azure environment

`FFSFT_SUBSCRIPTION_ID` is the only value with no default. Everything else defaults to the
resources this asset provisions: `FFSFT_TENANT_ID`, `FFSFT_RESOURCE_GROUP` (rg-ffsft-kc),
`FFSFT_WORKSPACE` (mlw-ffsft), `FFSFT_LOCATION` (koreacentral), `FFSFT_COMPUTE`, `FFSFT_SKU`,
`FFSFT_VM_PRIORITY`. Set `FFSFT_TENANT_ID` on any workstation signed in to more than one
directory, or calls fail with `InvalidAuthenticationTokenTenant`, which looks like a
permissions problem and is not.

Container images are built server-side with `az acr build` (`docker/Dockerfile.train`,
`docker/Dockerfile.serve`) so a 20 GB image never crosses the client uplink.

## Architecture

### Three registries, one loading convention

`configs/models.yaml`, `configs/serving.yaml`, `configs/benchmarks.yaml` are loaded by
`ffsft/models/registry.py`, `ffsft/deploy/registry.py`, `ffsft/eval/registry.py`, which are
deliberately near-identical: explicit path → `FFSFT_*_REGISTRY` env var →
`Path(__file__).parents[3]/configs/*.yaml`, cached with `@lru_cache`, duplicate keys rejected.
Every spec is pydantic v2 (`ModelSpec`, `ServingSpec`, `BenchmarkSpec`).

**No module hardcodes a model id, a SKU, or a vLLM flag.** Code asks the registry for a spec and
reads capabilities off it, so swapping Qwen → Kanana/EXAONE/Llama is a YAML edit. When adding
behaviour, add a field to the spec, not a branch on the model key.

### Fabric (CPU) vs Azure ML (GPU)

Data preparation runs on Microsoft Fabric Spark; training runs on Azure ML. `notebooks/fabric/`
holds thin Spark wrappers that contain no logic — all of it lives in `src/ffsft/data/fabric_prep.py`
as pure Python so it is unit-testable with no Spark import. `notebooks/fabric` is a **git subtree**
synced to a satellite repo (`git subtree push --prefix=notebooks/fabric fabric main`); edit it here,
push it there, never the reverse without a `subtree pull --squash`.

### Train → register → merge → serve

`train/aml_job.py` submits a command job; `deploy/model_asset.py` registers the adapter from the
job output; `deploy/merge.py` folds LoRA into bf16 base weights; `deploy/endpoint.py` and
`deploy/lifecycle.py` create and destroy the endpoint. `eval/run.py` always scores base against
tuned on identical items — the delta is the unit of work, a single absolute score is not.

### Two images, never one

`Dockerfile.train` is ACPT + upgraded HF libraries, with `/tmp/base-constraints.txt` pinning the
validated torch build so no dependency can swap it. `Dockerfile.serve` is `vllm/vllm-openai`
pinned to a version that registers the target architecture. They cannot share a base and must not
be merged. Both run a `docker/verify_*.py` build gate as a real file (a `RUN python - <<'PY'`
heredoc is silently a no-op under the classic builder).

## Invariants that cost real money to rediscover

Each of these is a measured constraint, not a preference. `docs/JOURNAL.md` is the evidence log —
sections are cited by number from code comments, and it contains explicit retractions of earlier
wrong root causes (§0, §30). Read it before "fixing" anything below.

- **LowPriority is the default tier and must stay so.** Dedicated GPU quota is per-family and
  absent by default; AmlCompute then reports `not a supported VM size`, which is a lie. LowPriority
  is also the only tier the tenant N-series deny policy permits. Nodes are preemptible → checkpoint.
- **Online endpoints charge `ceil(1.2 × instances) × cores`** (`ONLINE_ENDPOINT_UPGRADE_RESERVATION`)
  for rolling-update headroom, and cannot use LowPriority at all. The round-up makes one instance
  cost double — which is why a 36-core grant does not fit an NV36 — but two instances cost three,
  not four. The A100/H100/ND families are exempt entirely
  (`UPGRADE_RESERVATION_EXEMPT_FAMILIES`; "Skip 20% Reservation" in the supported-SKU doc), so a
  24-core A100 needs 24. `serving.yaml` defaults to `Standard_NV12ads_A10_v5`, pinned by
  `tests/test_serving_registry.py`.
- **Code is baked into the training image**, because `command(code=...)` uploads from the client
  machine to a storage account that refuses it. A code change is therefore an image change: bump
  the tag in `TRAIN_IMAGE`. `ENVIRONMENT_VERSION = image_tag(TRAIN_IMAGE)` is derived, never typed.
  Azure ML environment versions are immutable, so a reused tag silently runs the old script.
- **Only declared outputs survive.** `JobSpec.declared_outputs()` returns `{model_dir, report}`;
  anything else the script writes dies with the node. Two completed 27B runs lost their adapters
  this way.
- **Hybrid-attention models must declare `lora_target_modules` explicitly.** PEFT's default
  `{q,k,v,o}_proj` set exists only on the 1-in-4 full-attention layers of Qwen3.5/3.6/3.8, so most
  layers get no adapter and training succeeds without any error. Both `aml_job.submit` and
  `qlora.resolve_target_modules` refuse rather than guess; `--allow-default-lora-targets` is the
  deliberate opt-in.
- **vLLM architecture flags default to neutral** in `Dockerfile.serve` ENV. Per-model values
  (`multimodal`, `mamba_cache_mode`, `reasoning_parser`) come from the spec via `serving_env()`.
  Baking Qwen3.8's values into the image broke every other model.
- **MLflow is the only readable reporting channel** out of a network-isolated job.
  `deploy/logs.py::classify_log_response` returns a `LogStatus` so "could not look" is never
  reported as "looked, saw nothing".
- **Benchmarks are `eval_only`.** No benchmark id may appear in a training mix — pinned by a test.
  Judge questions are not vendored (LogicKor has no license).
- **Korean text is NFC-normalised before dedup.** NFD renders identically and hashes differently,
  so dedup silently does nothing without it.
- **Every `up` needs a `down`.** A managed online endpoint bills at full rate while idle
  (~$103/day for NV36). `ffsft-lifecycle down` prints orphaned disks/IPs but never deletes them —
  that stays a human decision.

## Conventions

- **`docs/JOURNAL.md` is append-only evidence.** When you measure something on Azure, add a
  numbered section; when a section turns out wrong, retract it in place rather than editing history.
  `docs/RUNBOOK.md` is the manual up/down procedure, `docs/SERVING.md` the serving patterns,
  `docs/design/PLAN.md` the original design research.
- **`docs/labs/lab0..lab8.md` are the workshop**, `docs/GOTCHAS.md` the failures a participant
  actually hits, and `docs/RESULTS.md` the reference run's measured numbers with the raw
  `--output` JSONs in `docs/results/`. A lab's "기대 출력" must be cut from `RESULTS.md`, not
  retyped: every number in the labs traces to a measurement or a JOURNAL section, never to an
  estimate. Bump the test count in `lab0.md` when the suite grows.
- **Tests never touch Azure or the network.** The SDK is imported lazily inside functions, so tests
  inject fakes by monkeypatching the module attribute the function reaches for (see the docstring
  of `tests/test_aml_job.py`). Keep new Azure imports function-local for this reason.
- Test names are full sentences describing the guarantee
  (`test_submit_refuses_a_model_that_does_not_fit_the_sku`). There is no `conftest.py`.
- Comments explain *why*, usually citing a failure that was paid for. Match that register — a
  comment restating the code is noise here.
