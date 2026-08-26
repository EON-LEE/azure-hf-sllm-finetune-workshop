#!/usr/bin/env python3
"""Download a model directory from Azure Blob Storage using AAD only.

Why this file exists
--------------------
The normal way to get a fine-tuned model onto a managed online endpoint is to
register it as a Model asset and let Azure's storage-initializer mount it. That
route is closed on this tenant. The management-group policy assignment
`MCAPSGovDeployPolicies` applies `StorageAccount_DisableLocalAuth_Modify`, which
sets `allowSharedKeyAccess=false` on every storage account at creation. Azure
ML's Model Registry enumerates candidate blobs with an *account key*
(`...ModelRegistry.Services...BlobContainerClient.EnumerateBlobPathsUnderPrefix`
over the legacy `Microsoft.Azure.Storage` SDK), so model registration fails with
`KeyBasedAuthenticationNotPermitted` no matter how the client is configured --
local upload, datastore path and job-output reference all fail identically.

The split is specifically key-vs-AAD, not storage-is-unreachable: datastore
mounts from compute, data-asset registration and this downloader all use AAD and
all work. So the container fetches its own weights with the deployment's managed
identity instead of waiting to be handed a mount.

Failure is fatal on purpose
---------------------------
If this script fails, the container must die. `serve_entrypoint.sh` falls back to
treating MODEL_PATH as a Hugging Face repo id when no local model is found, and
that fallback is correct for base-model deployments -- but if it ever caught a
failed *fine-tuned* fetch, the endpoint would come up healthy while serving the
untuned base model. Every downstream check, including a load test, would pass
against the wrong weights. A non-zero exit here is the guard against that.
"""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit

from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient

#: Downloading 54 GB one blob at a time over a single connection is the
#: difference between a two-minute and a forty-minute container start, and
#: Azure ML marks a deployment unhealthy long before the latter finishes.
WORKERS = int(os.environ.get("MODEL_FETCH_WORKERS", "16"))

#: Anything smaller than this in a checkpoint directory is metadata
#: (config.json, tokenizer files). Used only for the progress log.
BIG = 64 * 1024 * 1024


def parse_uri(uri: str) -> tuple[str, str, str]:
    """Split ``https://acct.blob.core.windows.net/container/prefix`` into parts."""
    parts = urlsplit(uri)
    if parts.scheme != "https" or not parts.netloc:
        raise SystemExit(f"MODEL_BLOB_URI must be an https blob URL, got: {uri!r}")
    segments = [s for s in parts.path.split("/") if s]
    if not segments:
        raise SystemExit(f"MODEL_BLOB_URI has no container: {uri!r}")
    account_url = f"{parts.scheme}://{parts.netloc}"
    container = segments[0]
    prefix = "/".join(segments[1:])
    return account_url, container, prefix


def credential():
    """Prefer the deployment's own identity, fall back for local testing.

    `DefaultAzureCredential` alone is not enough here. On a managed online
    endpoint it does find the MSI, but it first tries several sources that are
    absent in that container and each attempt costs a timeout, which shows up as
    a container that sits silent for a minute before any download starts.
    """
    client_id = os.environ.get("UAI_CLIENT_ID") or os.environ.get(
        "AZURE_CLIENT_ID"
    )
    try:
        if client_id:
            return ManagedIdentityCredential(client_id=client_id)
        return ManagedIdentityCredential()
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"[fetch] managed identity unavailable ({exc}); trying default chain")
        return DefaultAzureCredential()


def main() -> int:
    uri = os.environ.get("MODEL_BLOB_URI", "").strip()
    dest = Path(os.environ.get("MODEL_CACHE_DIR", "/tmp/ffsft-model"))
    if not uri:
        raise SystemExit("MODEL_BLOB_URI is empty; nothing to fetch")

    account_url, container, prefix = parse_uri(uri)
    print(f"[fetch] account  : {account_url}")
    print(f"[fetch] container: {container}")
    print(f"[fetch] prefix   : {prefix}")
    print(f"[fetch] dest     : {dest}")

    svc = BlobServiceClient(account_url=account_url, credential=credential())
    client = svc.get_container_client(container)

    blobs = [b for b in client.list_blobs(name_starts_with=prefix) if b.size]
    if not blobs:
        raise SystemExit(
            f"[fetch] no blobs under {container}/{prefix} -- refusing to start.\n"
            "        An empty prefix means the merge job wrote nothing there."
        )

    total = sum(b.size for b in blobs)
    print(f"[fetch] {len(blobs)} blobs, {total / 2**30:.1f} GiB")

    def one(blob) -> int:
        rel = blob.name[len(prefix):].lstrip("/")
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        # A re-created container reuses the instance's disk. Re-downloading 54 GB
        # because a probe restarted the process is the difference between a
        # healthy rollout and one Azure kills for exceeding its startup budget.
        if out.exists() and out.stat().st_size == blob.size:
            return blob.size
        with open(out, "wb") as fh:
            client.download_blob(blob.name, max_concurrency=4).readinto(fh)
        return blob.size

    started = time.monotonic()
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(one, b): b for b in blobs}
        for fut in as_completed(futures):
            blob = futures[fut]
            # Let the exception propagate: a partial checkpoint must not serve.
            done += fut.result()
            if blob.size >= BIG:
                pct = 100.0 * done / total
                rate = done / 2**20 / max(time.monotonic() - started, 1e-6)
                print(f"[fetch] {pct:5.1f}%  {rate:7.1f} MiB/s  {blob.name}")

    elapsed = time.monotonic() - started
    print(f"[fetch] complete: {total / 2**30:.1f} GiB in {elapsed:.0f}s")

    # The point of the download is a servable checkpoint, so assert that rather
    # than assert that bytes moved. A prefix one level too high copies files
    # successfully and still has no config.json at its root.
    if not (dest / "config.json").exists():
        nested = sorted(dest.glob("*/config.json"))
        if not nested:
            raise SystemExit(
                f"[fetch] downloaded {len(blobs)} blobs but found no config.json "
                f"under {dest} -- MODEL_BLOB_URI probably points at the wrong level"
            )
        print(f"[fetch] config.json found nested at {nested[0]}")
    print(f"[fetch] OK {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
