# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

`uv` drives everything. It installs to `~/.local/bin`, which is not on the default PATH:

```bash
export PATH="$HOME/.local/bin:$PATH"
uv sync --extra dev          # dev extra is enough for the whole test suite
uv run pytest                # 1232 passed, 2 skipped, ~9s, no network and no Azure calls
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

**Infrastructure** — `ffsft infra up | down`. `up` deploys `infra/main.bicep` at
subscription scope and writes `~/.ffsft-env`; `down` deletes the whole resource group and
purges its Key Vaults. One prefix names every resource, so the workshop is one group, one
region, one shell — and `down` can mean it (`src/ffsft/infra.py`, `docs/labs/lab0.md`,
`docs/labs/lab7.md` §7).

**Registry inspection** — reads `configs/*.yaml`, no heavyweight deps:

```bash
uv run ffsft models list --commercial-only
uv run ffsft models show qwen3.8-27b
uv run ffsft serving list / serving show / serving adapter-modes
uv run ffsft bench list / bench suites
```

**Delegates** — `ffsft train submit | merge submit | merge local | eval | deploy |
lifecycle | loadtest | plot | serve-local` swap `sys.argv` and call the same `main()` the
console script calls, so the two names cannot drift (`tests/test_cli_delegates.py`
reads `[project.scripts]` and fails if a script appears with no `ffsft` command
reaching it). **`cli.py` must import with only the registry deps present** — every
delegating import is function-local, pinned by an ast walk over the module body.
`_COMMAND_ORDER` sorts `--help` by lab, not by Click's collection order.

The `ffsft-*` console scripts all still work, but the labs use one entry point:

```bash
python -m ffsft.train.preflight                  # node self-test, cheapest way to prove a cluster
python -m ffsft.train.qlora --model ... --mix ...
uv run ffsft eval      # base vs tuned delta
uv run ffsft merge local     # LoRA -> merged bf16 weights
uv run ffsft deploy    check | deploy-online | shift | deploy-batch
uv run ffsft lifecycle status | up | down [--endpoint E [--deployment D]] [--all] [--yes]
uv run ffsft serve-local                         # CPU transformers, OpenAI-compatible + SSE
uv run ffsft loadtest                            # TTFT / TPOT / p50-p95-p99, knee point
uv run ffsft plot      # a loadtest --output report -> SVG, stdlib only, no matplotlib
```

`ffsft serve-local` plus `ffsft loadtest` (or `scripts/mock_vllm_server.py`) exercise the
entire serving half with no GPU and no Azure.

### Free checks worth running before spending money

```bash
uv run python scripts/verify_hf_ids.py                 # every hf_id in configs/ against the HF API
uv run python scripts/probe_architecture.py qwen3.8-27b --check   # declared vs actual LoRA targets
uv run ffsft deploy check --probe                      # real create calls, min=0, deleted after
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

`FFSFT_VERBOSE_AZURE` is the one variable here that names no resource. Every console script
silences the Azure SDK's own INFO logging at startup; setting this puts it back. Any value except
`0`/`false`/`no`/`off` counts as on, and an empty value is treated as unset (`export
FFSFT_VERBOSE_AZURE=` is a shell accident, not a request). See *Quiet by default, one switch to
undo it* below — and note the labs already send participants here when a deployment fails with no
visible reason (`docs/labs/lab5.md`, `docs/labs/lab7.md`).

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

The deploy path is four files, split by what each one is allowed to touch:

| file | contains | touches Azure |
|---|---|---|
| `probes.py` | `check_pattern` and the live "would this work?" calls — quota read, min=0 cluster, storage properties | yes |
| `preflight.py` | the pure classifiers turning a read state into a blocker string — **and the two ARM reads that produce those states**, `read_storage_reachability` and `read_sku_availability` | yes, those two only |
| `readiness.py` | startup budget → the three probe fields Azure accepts | no |
| `endpoint.py` | environment, `serving_env`, `ensure_endpoint`, `deploy_online`, `deploy_batch`, CLI | yes |

`preflight.py` was the Azure-free half at the split, and its own docstring and `probes.py`'s
still describe it that way; the two readers landed there afterwards. Treat the file as mixed —
everything in it except those two functions is pure, and both keep the function-local
`import requests` / `from azure.identity import …` the convention requires. They are also not
deploy-only: `scripts/submit_training.py` and `scripts/submit_merge.py` call
`read_storage_reachability` before submitting, so a change there moves the training path too.

`endpoint.py` re-exports every name that moved, so old imports resolve. **Patching a re-export
is silent**, though: `check_pattern` reads `read_dedicated_quota` as a `probes` global, so a
`monkeypatch.setattr(endpoint, "read_dedicated_quota", …)` fakes a name nobody reads and the real
call leaves the machine. `tests/test_deploy_module_split.py` pins both halves of that seam. The
two preflight readers land in the same trap by a different route: `deploy_online` imports them
*inside* the function, so they are never `endpoint` globals at all — fake them on `preflight`,
which is what every test that reaches them already does.

### Quiet by default, one switch to undo it

`ffsft/logging_setup.py` silences the Azure SDK's INFO chatter, and every console script in
`[project.scripts]` calls `quiet_azure_sdk_logs()` at startup — `ffsft lifecycle status` exists to
print one small cost table and shipped burying it under hundreds of `azure.core` HTTP dumps. The
module imports no Azure package (`logging.getLogger` happily names a logger nothing has created
yet), so it stays importable with the `azure` extra absent and stays cheap on the registry-only
paths; `cli.py` imports it inside the Typer `@app.callback()`, function-local like every other
delegating import there.

It is not the four-line `setLevel(ERROR)` snippet it looks like. Both halves are load-bearing:

- **Levels** on `azure`, `azure.core`, `azure.identity`, `azure.ai.ml`. Naming the parent alone is
  not enough — `azure.ai.ml` sets an explicit level on itself and four children at import, and an
  explicit level short-circuits the effective-level walk.
- **An `AzureNoiseFilter`** on the root handlers *and* on the handlers of every `azure*` logger
  that already exists. Azure imports here are function-local, so the SDK is imported *after* a CLI
  configures logging; that import resets `azure.ai.ml` to INFO, sets `propagate = False` and
  attaches its own `StreamHandler`, which puts its records out of reach of a root filter entirely.
  A handler filter runs after record creation, so it still applies to a logger that reset its own
  level behind our back.

Call it **after** `logging.basicConfig(...)`, never before: the filter half attaches to the root
handlers that exist at call time, and `basicConfig` is what creates them. WARNING and above always
survive — a quota rejection and an auth failure both arrive as WARNING, and swallowing those would
trade a scrolling problem for a silent one. One gap is left open on purpose: a logger the SDK
creates *after* the call cannot be reached, so an entry point that wants the last word calls the
helper again after building its client, which is cheap and idempotent.

`FFSFT_VERBOSE_AZURE=1` puts all of it back, and the helper *actively restores* when it is set
rather than merely declining to quiet. The on position is not decoration: when a deployment fails
the SDK's HTTP dumps are routinely the only evidence that exists, and this repo has already lost a
failure permanently by tearing an endpoint down before its logs were captured.

`tests/test_logging_setup_wiring.py` reads `[project.scripts]` rather than a list maintained in
the test, so a new console script has to be wired or these tests name it. It also pins the call
*after* `basicConfig` — which is where the suite's only two skips come from: `ffsft` and
`ffsft-plot` configure no logging of their own, so there is no order to check.

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
- **The serving image is a parameter; its environment version is derived per call.** The train
  side above can afford a module-level `ENVIRONMENT_VERSION` because `TRAIN_IMAGE` is a constant.
  The serving image is not: `--image` > `$FFSFT_SERVE_IMAGE` > the `SERVE_IMAGE` constant,
  resolved in `endpoint.resolve_serve_image()` and nowhere else. So `serve_environment` computes
  `version = image_tag(image)` at call time, and `SERVE_ENVIRONMENT_VERSION` survives only as a
  compatibility export that no deploy path reads. **A module-level version derived from a
  constant, paired with an image chosen at runtime, is silent-wrong code** — versions are
  immutable, so a participant's `ffsft-serve:1` would ask for the version belonging to the
  authors' tag and get whatever that version already points at, with no error. Two traps around
  it: an empty or whitespace value at either of the first two levels is treated as unset
  (`export FFSFT_SERVE_IMAGE=` is a shell accident, not a request to deploy `''`), and the
  `--image` argparse default is `None` on purpose, because a parser-level default would shadow
  the env var on every invocation. `image_tag` refuses an untagged or digest-pinned reference
  before `check_pattern` — before any Azure call, and long before a 15-30 minute rollout.
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
- **MLflow is the only readable reporting channel** out of a network-isolated job. Azure
  withholds an online deployment's container logs for any state that is not terminal, so during
  `Creating` their absence carries no information at all (`deploy/logs.py`).
- **"Could not look" is never reported as "looked, saw nothing."** This is a property of the
  codebase, not a fact about one file. It was written here as a property of
  `deploy/logs.py::classify_log_response`, and it was therefore obeyed in `deploy/logs.py` and
  nowhere else — while the identical failure sat one directory over on the money path, where four
  listings that all raised printed `BILLING NOW: nothing. No always-on compute in this
  workspace.`, byte-identical to a real teardown. Three more instances of the same class were
  still open a round later. §73.7 draws exactly this lesson — an invariant written per-file is
  obeyed per-file. So check new code against the rule, which runs in both directions:
  - **A call that failed may not be rendered as a fact about the world.** Give the read a status,
    keep that status next to the rows it explains, and print no figure, no count and no verdict
    while the status says the read did not happen.
  - **A caller may not act on the absence of rows it never managed to read.** Deleting, exiting
    `0`, and printing "nothing here" all count as acting.
  - **An unread field may not become a finding either.** Refusing on a value nobody measured is
    the same error with the sign flipped, and costs as much: it blocks a deployment that would
    have worked, or leaves a $4.320/hr A10 running to protect a claim nobody made.

  Where the property is enforced today. This list is the checklist; the rule above is the rule,
  and new code is checked against it and then added here:

  | site | the read | what it refuses to say |
  |---|---|---|
  | `deploy/logs.py::classify_log_response` | container `getLogs` | returns `LogStatus` OK / WITHHELD / GONE / ERROR. Azure answers a refusal with prose at HTTP 200, so the status code alone cannot tell a log from a sentence about logs |
  | `deploy/lifecycle.py::_section`, `SectionScan`, `ScanStatus` | every `collect_inventory` listing | a listing that did not fully happen is recorded, never dropped, so an empty `items` can be read. Raising is not the only way in: a section may also set `scan.status = FAILED` itself, which is how a listing that returned but came back incomplete is recorded WITHOUT discarding the rows it did read |
  | `deploy/lifecycle.py` jobs section | `client.jobs.list()` | that a job listing with a HOLE in it is a complete read. `JobOperations.list` maps each item through `_handle_rest_errors`, which is `except JobParsingError: return None`, so a job the SDK cannot deserialize arrives as a `None` INSIDE a successful list — no exception, no short page. `getattr(None, "status", "")` filtered it out as "not running" and `down` printed `meter stopped.` rc=0 over a Running A100 (§82.1). Neither mechanical guard can see this: the swallowing `except` is in site-packages, and it is not an ARM GET |
  | `deploy/lifecycle.py::read_orphans(inv=…)` | the resource-group disk/NIC/IP scan, **per listing** | that a listing which did not complete makes the two that did unreadable. Each of the three ARM listings is read independently, so a section can hold BOTH measured rows and an unread listing; the rows are returned, `scan.detail` names which listing stopped, and rc stays `EXIT_COULD_NOT_LOOK`. It still never raises — a cost report that raises is one nobody runs. A missing `LEFTOVERS` block is itself a claim and it hid a $41.66/month leak once (§11.4); §81 is the mirror, where a disk that HAD been read was discarded because a sibling listing truncated |
  | `deploy/lifecycle.py::format_inventory`, `unlisted_note` | — | prints `BILLING NOW: UNKNOWN -- could not look` and `LEFTOVERS: UNKNOWN` with no figure and no resource count, and **names** which listing failed and with which exception type |
  | `deploy/lifecycle.py::cmd_down`, `blind_spots` | the endpoint's deployment listing | will not delete an endpoint whose deployments could not be listed. An endpoint that cannot be listed is not an empty one — it may be serving, and it bills either way |
  | `deploy/lifecycle.py` `EXIT_COULD_NOT_LOOK` | — | exit `0` after a failed scan. `down --all --yes && echo clean` printed `clean` over four failed listings, which is the same false sentence in the one channel a script reads |
  | `deploy/lifecycle.py` `EXIT_NOT_IDLE` (§75) | the resource-group scan that succeeded | exit `0` over leftovers it just named. `down` ends by asserting the meter is off; `1` there would mean "could not look", and the operator's next move after each is the opposite of the other. `cmd_status` keeps returning `0` on the same workspace — it answers "did I manage to read", not "is it stopped" |
  | `deploy/probes.py::_name_is_taken`, `_refuse_name` | `compute.get(probe name)` | that an unreadable name is a free one. The write is an upsert and the teardown after it is unconditional, so a guess overwrites and then deletes a cluster belonging to somebody who never ran this command (audited: `created: [('ffsft-probe-0', …)]` then `DELETED: ['ffsft-probe-0']`) |
  | `deploy/probes.py::probe_report`, `SkuProbe.probed` (§75) | the create call that was never made | that a refused probe name is a verdict about the SKU. `probed=False` prints `UNKNOWN`, not `BLOCKED`, and joins `cmd_check`'s COULD NOT LOOK list so `check --probe && echo ok` stops printing ok over a SKU nobody asked about |
  | `deploy/preflight.py::sku_advisory`, `online_endpoint_blocker` | SKU `restrictions` | return `None` when the field was never read: "not measured" is not a finding |
  | `deploy/preflight.py::read_storage_reachability`, `read_sku_availability` | two ARM reads | return `None` on every failure path, so a transient ARM error is never the reason a workable deployment does not happen |
  | `deploy/identity.py::read_identity_grants`, `identity_unread_note` (§79) | the endpoint identity's `roleAssignments` listing, **per scope** | that a listing ARM truncated is a listing that was short. The role lists are tri-state (`[...]` / `[]` / `None`), `identity_blocker` fires only on `is False`, and the gap is printed as `UNKNOWN` with the `az role assignment list` for that scope. This is the sign-flipped half: an AcrPull grant ARM returned on page 2 made `can_pull_image` False and refused a deployment that would have worked |
  | `deploy/endpoint.py::deploy_online`, the `unread` WARNING (§80) | `identity_unread_note`'s sentence | that a state which exists in a record has been reported. `log.warning` → `log.debug` on that one line left the whole suite green and took the only thing an operator would ever see with it: the deploy went out over a listing nobody could finish and said nothing. Not refusing is half the bargain; the other half only exists where it is printed |
  | `deploy/identity.py::acr_id_for_image` (§81.4) | the subscription-wide registry listing | that a listing which did not finish is quieter than one that hit the page cap. Every way for the listing to stop short is the same WARNING, told apart by the exception type in the message; the `log.debug` survives only for a lookup that never started. `log.debug` is off in every shipped entry point, so the level IS whether the operator is told |
  | `deploy/identity.py::identity_blocker`'s remedy footer (§81.5) | the same two role listings the bullets are built from | prescribing an `az role assignment create` for a grant it refused to call missing. Findings and remedies come off one list, so an unread scope cannot become a finding by way of the command that closes it — the sign-flipped rule with an action stapled on |
  | `deploy/endpoint.py::egress_for` | the preflight the caller already ran | a `None` reachability means the workspace could not be read, so the setting is left unset rather than asserted either way (§64) |

  Two boundaries the list carries with it. `blind_spots` scopes the refusal to the claim actually
  being made — `--all` makes every listing load-bearing, `--endpoint E` only that endpoint's two —
  because widening it turns a permission gap anywhere in the workspace into an untearable
  endpoint, and the operator's next move is `--yes` somewhere less careful. And the same family,
  "a success is not an observation", lives in `deploy/model_asset.py` (registering an asset does
  not verify that anything is at the URI, and ARM reports a successful upload's
  `job.outputs[…].path` as `null`) and `ffsft/serve/smoke.py` (a 200 is not evidence a deployment
  serves correctly — read the body).

  **The list above is no longer maintained by eye.** Five manual sweeps in a row each found
  instances the previous one had walked past, and §76.6 is the honest version of why: it
  re-derived every ARM write in `src/` from scratch, found one violation, and wrote down that
  this was "zero *when swept in that shape*". So since round 6 the rule also has a mechanical
  guard, `tests/test_no_except_handler_hands_a_caller_an_empty_value_it_never_read.py`. It walks
  every `except` handler under `src/ffsft/` **and `docker/`** with `ast` — stdlib, this repo adds
  no dependencies — and flags four shapes: a falsy or empty `return`; a falsy local assigned in
  the handler and read after the `try`; a silent swallow that leaves a pre-initialised collection at its default;
  and a bare `pass` over a `try` body of more than one statement, which is the shape that let a
  404 from `ensure_compute`'s *repair* PUT a fresh cluster over the same name.

  It is a **review gate, not a lint rule**, and the difference is the whole design. The same
  `return None` is the fix at one site and the bug at another — `deploy/preflight.py`'s readers
  return it as a documented unread sentinel every caller branches on, and `probes.py`'s datastore
  listing returned `[]` for both "could not look" and "looked, none" until §78.2 gave it the same
  sentinel. No walker can tell a sentinel from a silence, so it does not try. Every flagged
  handler is keyed `file::function::except type::shape` — never by line number, which is the one
  thing the person fixing it moves — and has to appear in one of two dicts in that file:

  - `ALLOWLIST`, with a one-line argument for why this emptiness is *measured*: a 404 that
    positively establishes absence (`ensure_endpoint`'s create-on-404, `_discard_probe`), or a
    falsy value describing this process's own action rather than the world (`publish() -> False`).
    Argue it; do not inherit it. 27 entries.
  - `KNOWN_OPEN`, for a violation that is real, routed, and not fixed yet. It may only shrink.
    6 entries — in `data/korean.py`, `deploy/lifecycle.py`, `serve/bench_report.py` (twice),
    `serve/loadtest.py`, `train/preflight.py`. §78 closed the `azure_ml.py` and
    `deploy/probes.py` entries; the module's own docstring now also lists what the walker
    **cannot** see, because a scan that reports only what it found is the invariant's own
    failure mode applied to the guard.

  Anything else fails the test, which prints the file, the line, the shape and those three
  options. Do not make it green by narrowing the walker: the four shapes are pinned as literal
  source strings in the same file, run through the *same* function that scans `src/`, and a fifth
  test floors how many files it still reaches. Census when it landed: 62 handlers walked, 30
  flagged, 22 allowlisted, 8 open. Live at round 10: 72 walked, 35 flagged, 27 allowlisted, 6 open.
  Round 10 (JOURNAL §87) deleted `eval/run.py::publish`'s own copy of the shared-`try` bug
  entirely rather than re-arguing it, which is why `ALLOWLIST` has one fewer entry than round 9,
  not one more; the walked/flagged totals moved from unrelated handlers added elsewhere since.

  **The walker's root is part of the guard.** `docker/verify_serve.py`'s `except OSError: continue`
  — which drops a vLLM source file it could not open and then prints `scanned N vllm source files`,
  a count with no denominator — was reported by three consecutive audits and fixed by none of
  them, purely because `docker/` sat outside the tree the guard walked and so nothing ever failed
  over it. Round 8 added the root and the file now names every skip. A directory the guard does
  not walk is a directory that is silent by omission, not clean: `scripts/` is the last one, and
  it is declared in the module docstring's blind-spot list rather than left to be rediscovered.

  **The swallow is one shape of the rule; truncation is the other.** A read can succeed and still
  be incomplete. ARM answers a listing one page at a time
  (`{"value": [...], "nextLink": ...}`), so a caller reading `resp.json()["value"]` and dropping
  `nextLink` gets HTTP 200, no exception, a short list — and then states a full-scan negative over
  it. No `except` is involved anywhere, which puts the entire class inside the swallow guard's
  declared blind spot #3. `read_all_arm_pages` (`deploy/preflight.py`) is the fix — it follows
  `nextLink`, caps at `MAX_ARM_PAGES`, refuses a repeated one, refuses one that points off the
  scheme+host the caller named (§81.3 — `nextLink` chooses where the loop sends the caller's ARM
  bearer token next, and comparing against the caller's OWN origin rather than an allowlist keeps
  the sovereign clouds paging), and raises `TruncatedListing` rather than returning a short list
  — and
  `tests/test_no_arm_listing_is_read_one_page_deep_and_called_the_whole_list.py` is the guard: it
  classifies every `management.azure.com` GET under `src/ffsft/` by the top-level keys the caller
  reads off the body, and a GET whose `value` is consumed without going through the paginator is a
  finding. Same two dicts, same argued-reason discipline. It flags a body it could not follow and
  a URL it could not resolve, rather than passing them — a guard that reports only what it managed
  to follow is this repo's own failure mode wearing a lab coat. Live: 8 ARM GETs walked, 7
  paginator call sites, 1 allowlisted (the paginator's own GET) and 0 open.
- **Benchmarks are `eval_only`.** No benchmark id may appear in a training mix — pinned by a test.
  Judge questions are not vendored (LogicKor has no license).
- **Korean text is NFC-normalised before dedup.** NFD renders identically and hashes differently,
  so dedup silently does nothing without it.
- **Every `up` needs a `down`.** A managed online endpoint bills at full rate while idle
  (~$103/day for NV36). `ffsft lifecycle down` prints orphaned disks/IPs but never deletes them —
  that stays a human decision.
- **An unknown hourly rate is not zero, and the report must say which it is.** `hourly_rate()`
  still returns `0.0` for a SKU outside `SKU_HOURLY_PAYG` so no caller crashes, but every caller
  that renders money asks `rate_is_known()` first: the row prints `?` and
  `(price unknown for this SKU)`, the total excludes it and names it, and when nothing can be
  priced the line reads `cost UNKNOWN -- no rate for any of them, which is not the same as free`
  with no dollar figure anywhere. Without that split, `status` over a live
  `Standard_NV6ads_A10_v5` printed `BILLING NOW: 1 resource(s) $0.000/hr ~$0/month` — asserting
  the resource bills and pricing it at zero in the same line (§71). The table is PAYG only:
  managed online endpoints cannot use LowPriority and are not Spot, so filing a cheaper tier
  there would under-report the one resource in this repo that bills 24/7.

## Conventions

- **`docs/JOURNAL.md` is append-only evidence.** When you measure something on Azure, add a
  numbered section; when a section turns out wrong, retract it in place rather than editing history.
  `docs/RUNBOOK.md` is the manual up/down procedure, `docs/SERVING.md` the serving patterns,
  `docs/design/PLAN.md` the original design research.
- **`docs/labs/lab0..lab8.md` are the workshop**, `docs/GOTCHAS.md` the failures a participant
  actually hits, and `docs/PERFORMANCE.md` the reference run's measured numbers with the raw
  `--output` JSONs in `docs/results/`. A lab's "기대 출력" must be cut from `PERFORMANCE.md`, not
  retyped: every number in the labs traces to a measurement or a JOURNAL section, never to an
  estimate. Bump the test count in `lab0.md` when the suite grows.
- **The charts in `docs/results/*.svg` are generated, never hand-drawn** — `ffsft plot` renders
  them from the JSON next to them, so editing raw data updates the pictures. `ffsft serve-local`
  plus `ffsft loadtest` plus `ffsft plot` produce a full report with no GPU and no Azure.
- **A tok/s difference between two deployments is a length difference until proven otherwise.**
  `scripts/compare_deployments.py` is the check: it prints `finish_reason` per prompt and warns
  when replies sat on the `max_tokens` cap, where a token count is a floor and not a length
  (§70 retracts a causal claim that skipped this step).
- **Tests never touch Azure or the network.** The SDK is imported lazily inside functions, so tests
  inject fakes by monkeypatching the module attribute the function reaches for (see the docstring
  of `tests/test_aml_job.py`). Keep new Azure imports function-local for this reason.
- Test names are full sentences describing the guarantee
  (`test_submit_refuses_a_model_that_does_not_fit_the_sku`). There is no `conftest.py`.
- Comments explain *why*, usually citing a failure that was paid for. Match that register — a
  comment restating the code is noise here.
