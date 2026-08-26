#!/usr/bin/env python3
"""Prove a finished job actually wrote weights where the next step will look.

    python scripts/verify_output_path.py <run-name> [--output model_dir]

Registering a model asset is not evidence that anything is in it: an asset can
point at an empty folder and register cleanly, and the failure only surfaces
twenty minutes into a deployment when vLLM finds no config.json. The only way
to know is to mount the path and count what is there.

The assertion lives in the job's **exit code**, not in its stdout and not in
MLflow. Job stdout is unreadable from outside the VNet on a network-isolated
workspace, and metric values do not always land. Job status is the one channel
measured to work from a laptop:

    Completed -- the folder had files, bytes, and adapter or model weights
    Failed    -- it did not; whatever wrote there wrote nothing usable

Runs on the training cluster, which is LowPriority, so this costs a few cents.
"""

from __future__ import annotations

import argparse
import logging
import os

#: What the probe runs on the node. Written as one line because it is passed as
#: a `python -c` argument -- keeping it here rather than in a file means no
#: second asset has to exist for a check that runs once.
PROBE = (
    "python -c '"
    "import os,sys;"
    "r=sys.argv[1];"
    "fs=[(os.path.getsize(os.path.join(d,f)),os.path.join(d,f)) "
    "for d,_,g in os.walk(r) for f in g];"
    "n=len(fs);tot=sum(s for s,_ in fs);"
    'w=any(p.endswith((".safetensors",".bin",".gguf")) for _,p in fs);'
    'print("MOUNT_ROOT",r);'
    '[print("FILE",s,p) for s,p in sorted(fs,reverse=True)[:40]];'
    'print("COUNT",n,"BYTES",tot,"WEIGHTS",w);'
    "sys.exit(0 if (n>0 and tot>0 and w) else 1)"
    "' ${{inputs.target}}"
)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run", help="Run name of the finished job, e.g. olden_bean_302vkc7nbz.")
    ap.add_argument(
        "--output",
        default="model_dir",
        help="Named output of that run to inspect (default: model_dir).",
    )
    ap.add_argument(
        "--compute",
        default=None,
        help="Cluster to run the probe on. Defaults to FFSFT_COMPUTE.",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for noisy in ("azure", "azure.core", "azure.identity", "azure.ai.ml"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    from azure.ai.ml import Input, command
    from azure.ai.ml.constants import AssetTypes, InputOutputModes

    from ffsft.azure_ml import AzureTarget, get_ml_client
    from ffsft.train.aml_job import ENVIRONMENT_NAME, ENVIRONMENT_VERSION

    target = AzureTarget.from_env()
    client = get_ml_client(target)
    path = f"azureml://datastores/workspaceblobstore/paths/azureml/{args.run}/{args.output}/"

    job = command(
        command=PROBE,
        inputs={
            "target": Input(
                type=AssetTypes.URI_FOLDER, path=path, mode=InputOutputModes.RO_MOUNT
            )
        },
        environment=f"{ENVIRONMENT_NAME}:{ENVIRONMENT_VERSION}",
        compute=args.compute or target.compute_name,
        display_name=f"verify-path-{args.output}-{args.run[:12]}",
        experiment_name="ffsft-verify",
    )
    submitted = client.jobs.create_or_update(job)

    print(f"SUBMITTED: {submitted.name}")
    print(f"mounting : {path}")
    print()
    print("Completed = the folder had files, bytes, and weights")
    print("Failed    = it did not; whatever wrote there wrote nothing usable")
    print()
    print(f"  scripts/watch_jobs.sh VERIFY:{submitted.name}")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("AZURE_CORE_ONLY_SHOW_ERRORS", "true")
    raise SystemExit(main())
