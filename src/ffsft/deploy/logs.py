"""Never again read an error response as evidence of anything.

Two separate mistakes in this repo came from treating a failed or refused log
read as a fact about the container:

1. The original misdiagnosis. "App Insights traces empty and no container logs"
   was offered as proof that the container never started. Azure withholds logs
   for any deployment that is not in a terminal state, so during `Creating` that
   observation carries no information at all.

2. A log poller that ran for 33 minutes reporting `loglen=0` while every single
   request was being rejected with `RequestInvalid`. `containerType` is an enum
   that only accepts `StorageInitializer` and `InferenceServer`; the lowercase
   spellings are refused. `2>/dev/null` turned the rejection into silence and
   the silence read exactly like a healthy empty log.

So `read_logs` returns a verdict, never a bare string. Callers cannot
accidentally treat "could not look" as "looked, saw nothing".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LogStatus(str, Enum):
    """Why the log body is what it is."""

    #: Logs were returned. `text` is meaningful, including when empty.
    OK = "ok"
    #: Azure refuses logs outside terminal states. Says nothing about the container.
    WITHHELD = "withheld"
    #: The logs are gone rather than refused. A deployment that never reached a
    #: terminal state has its node reclaimed with the container's output still on
    #: it, and Azure then answers `getLogs` with prose telling you to try later --
    #: which is never going to become true. Distinct from WITHHELD because
    #: WITHHELD is worth retrying and this is not.
    GONE = "gone"
    #: The request itself failed. Says nothing about anything.
    ERROR = "error"


#: The container names, in the spelling `MLClient.online_deployments.get_logs`
#: accepts. There are two spellings and they belong to two different call paths,
#: which is why this looks like a contradiction and is not:
#:
#:   * The raw ARM `getLogs` enum takes PascalCase. Sending it lowercase returns
#:     a `RequestInvalid` UserError that reads as an empty log once stderr is
#:     dropped -- the 33-minute misdiagnosis in this module's docstring.
#:   * The SDK validates `container_type` on the client, before any request
#:     goes out, and accepts only the hyphenated lowercase form. On
#:     azure-ai-ml 1.34.1 the PascalCase constants raise
#:     `ValidationException: Invalid container type 'StorageInitializer'.
#:     Supported container types are inference-server and storage-initializer`
#:     -- measured 2026-08-24 against a live deployment.
#:
#: This repo reaches Azure through the SDK, so the SDK spelling is the one that
#: belongs in the constants. `REST_CONTAINER_TYPES` keeps the other so the
#: earlier finding is not re-lost.
STORAGE_INITIALIZER = "storage-initializer"
INFERENCE_SERVER = "inference-server"
CONTAINER_TYPES = (STORAGE_INITIALIZER, INFERENCE_SERVER)

#: The same two containers as the raw ARM enum spells them.
REST_CONTAINER_TYPES = ("StorageInitializer", "InferenceServer")


@dataclass
class LogRead:
    status: LogStatus
    text: str = ""
    detail: str = ""

    @property
    def is_evidence(self) -> bool:
        """True only when the absence of log lines actually means something."""
        return self.status is LogStatus.OK

    def __str__(self) -> str:
        if self.status is LogStatus.OK:
            return self.text or "(container produced no output)"
        return f"[{self.status.value}] {self.detail}"


def classify_log_response(status_code: int, body: str) -> LogRead:
    """Turn an ARM getLogs response into a verdict.

    Azure returns HTTP 200 with a prose sentence in the body when it declines to
    serve logs, so the status code alone cannot distinguish the cases.
    """
    if status_code != 200:
        return LogRead(LogStatus.ERROR, detail=f"http {status_code}: {body[:200]}")

    lowered = body.lower()
    if "can't be retrieved" in lowered or "cannot be retrieved" in lowered:
        return LogRead(LogStatus.WITHHELD, detail=body.strip())
    # Measured 2026-08-24 on `ffsft-qwen38/blue` the moment it went Failed, with
    # the node already reclaimed:
    #
    #     There are no logs for this deployment at the moment.
    #     Please try again later.
    #
    # 76 characters of prose returned as HTTP 200, and without this branch it
    # classified as OK -- a sentence about logs presented as the log. That is the
    # exact confusion this module exists to prevent, and it survived here for as
    # long as it did because the WITHHELD wording was the only one ever seen.
    if "no logs for this deployment" in lowered:
        return LogRead(LogStatus.GONE, detail=body.strip())
    if "not a valid enum value" in lowered or "requestinvalid" in lowered:
        return LogRead(
            LogStatus.ERROR,
            detail=(
                f"bad request: containerType must be one of {CONTAINER_TYPES} "
                f"through the SDK, or {REST_CONTAINER_TYPES} against raw ARM. "
                f"{body[:200]}"
            ),
        )
    return LogRead(LogStatus.OK, text=body)
