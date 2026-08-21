"""A refused log read must never be mistaken for an empty one.

Both mistakes these tests guard against actually happened here:

  * "no container logs" was cited as proof the container never started, while
    the deployment was in `Creating` -- a state in which Azure refuses logs to
    everyone, healthy or not.
  * a poller printed `loglen=0` for 33 minutes while every request was being
    rejected for using `containerType: inference-server` instead of
    `InferenceServer`.

Both look identical to a caller that only sees an empty string.
"""

from __future__ import annotations

from ffsft.deploy.logs import (
    CONTAINER_TYPES,
    INFERENCE_SERVER,
    STORAGE_INITIALIZER,
    LogStatus,
    classify_log_response,
)

WITHHELD_BODY = "Deployment is in deleting or creating state so logs can't be retrieved."
BAD_ENUM_BODY = (
    '{"error":{"code":"UserError","message":"Request is invalid and/or missing fields.",'
    '"details":[{"code":"RequestInvalid","message":"The value provided is not a valid '
    'enum value. Field: containerType. Allowed values: StorageInitializer,InferenceServer."}]}}'
)


# --------------------------------------------------------------------------
# The two silences
# --------------------------------------------------------------------------


def test_withheld_logs_are_not_evidence():
    """The exact body Azure returned during this deployment."""
    read = classify_log_response(200, WITHHELD_BODY)
    assert read.status is LogStatus.WITHHELD
    assert read.is_evidence is False


def test_rejected_request_is_not_evidence():
    """The 33-minute poller bug. HTTP 200 shape, zero information."""
    read = classify_log_response(200, BAD_ENUM_BODY)
    assert read.status is LogStatus.ERROR
    assert read.is_evidence is False


def test_a_genuinely_empty_log_is_evidence():
    """Only this case licenses "the container printed nothing"."""
    read = classify_log_response(200, "")
    assert read.status is LogStatus.OK
    assert read.is_evidence is True


def test_real_log_content_is_evidence():
    read = classify_log_response(200, "INFO vLLM API server started\n")
    assert read.is_evidence is True
    assert "vLLM" in read.text


def test_http_failure_is_not_evidence():
    assert classify_log_response(503, "upstream unavailable").is_evidence is False


# --------------------------------------------------------------------------
# The error has to point at the actual mistake
# --------------------------------------------------------------------------


def test_enum_rejection_names_the_valid_values():
    """The fix is a spelling, so the message must carry the spellings."""
    detail = classify_log_response(200, BAD_ENUM_BODY).detail
    assert STORAGE_INITIALIZER in detail
    assert INFERENCE_SERVER in detail


def test_container_types_are_pascal_case():
    """Lowercase spellings are silently refused by ARM."""
    assert CONTAINER_TYPES == ("StorageInitializer", "InferenceServer")
    for value in CONTAINER_TYPES:
        assert value[0].isupper()


def test_withheld_read_repeats_azures_own_wording():
    assert "creating state" in classify_log_response(200, WITHHELD_BODY).detail


# --------------------------------------------------------------------------
# Rendering must not disguise an absence as an observation
# --------------------------------------------------------------------------


def test_str_of_a_withheld_read_does_not_look_like_empty_output():
    rendered = str(classify_log_response(200, WITHHELD_BODY))
    assert rendered.startswith("[withheld]")


def test_str_of_an_empty_ok_read_says_so_explicitly():
    assert "no output" in str(classify_log_response(200, ""))


def test_wording_variant_cannot_be_retrieved_is_also_withheld():
    body = "Logs cannot be retrieved at this time."
    assert classify_log_response(200, body).status is LogStatus.WITHHELD
