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
    #: The request itself failed. Says nothing about anything.
    ERROR = "error"


#: The only values `containerType` accepts. Getting this wrong returns a
#: `RequestInvalid` UserError that looks like an empty log if stderr is dropped.
STORAGE_INITIALIZER = "StorageInitializer"
INFERENCE_SERVER = "InferenceServer"
CONTAINER_TYPES = (STORAGE_INITIALIZER, INFERENCE_SERVER)


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
    if "not a valid enum value" in lowered or "requestinvalid" in lowered:
        return LogRead(
            LogStatus.ERROR,
            detail=f"bad request (check containerType is one of {CONTAINER_TYPES}): {body[:200]}",
        )
    return LogRead(LogStatus.OK, text=body)
