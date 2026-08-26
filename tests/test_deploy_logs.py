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
    REST_CONTAINER_TYPES,
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


def test_container_types_use_the_spelling_the_sdk_accepts():
    """azure-ai-ml validates this client-side and takes only this spelling.

    Measured on 1.34.1 against a live deployment: the PascalCase values the raw
    ARM enum wants raise `ValidationException` before a request is even sent.
    Both spellings are real; this tuple is the one the SDK path uses.
    """
    assert CONTAINER_TYPES == ("storage-initializer", "inference-server")
    assert REST_CONTAINER_TYPES == ("StorageInitializer", "InferenceServer")
    for value in CONTAINER_TYPES:
        assert value.islower() and "-" in value


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


def test_a_sentence_about_logs_is_never_mistaken_for_the_logs():
    """Azure has a third answer, and it is neither an error nor a log.

    Measured when `ffsft-qwen38/blue` went Failed with its node already
    reclaimed: HTTP 200 carrying 76 characters of prose. Classified as OK it
    would read as a container that started, printed one line about logs, and
    stopped -- so the caller would conclude the container produced no output,
    which is the one conclusion this module is built to refuse.
    """
    body = "There are no logs for this deployment at the moment. Please try again later."
    read = classify_log_response(200, body)
    assert read.status is LogStatus.GONE
    assert not read.is_evidence
    assert body.strip() in str(read)


def test_gone_is_distinguished_from_withheld():
    """Both mean "no logs", but only one of them is worth waiting out."""
    withheld = classify_log_response(
        200, "Deployment is in deleting or creating state so logs can't be retrieved."
    )
    gone = classify_log_response(200, "There are no logs for this deployment at the moment.")
    assert withheld.status is LogStatus.WITHHELD
    assert gone.status is LogStatus.GONE
    assert not withheld.is_evidence and not gone.is_evidence
