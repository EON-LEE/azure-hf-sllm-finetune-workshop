"""The third shape of the round-7 invariant: incompleteness delivered INSIDE a
successful list, as a `None` element, instead of as an exception.

`JobOperations.list` in azure-ai-ml passes
`cls=lambda objs: [self._handle_rest_errors(obj) for obj in objs]`, and
`_handle_rest_errors` is `except JobParsingError: return None`. So a job ARM
returned and the SDK could not deserialize arrives as a `None` in an otherwise
successful listing -- nothing raises, and `getattr(None, "status", "")` is `""`,
which the old status filter discarded as "not a running job".

Neither mechanical guard in this suite can see that by construction. The swallow
guard walks `except` handlers under `src/ffsft/` and `docker/`; this `except`
lives in site-packages. The ARM guard walks `management.azure.com` GETs; this is
the AML client. That is why these are hand-written.
"""

from __future__ import annotations

import pytest

from ffsft.deploy.lifecycle import ScanStatus, collect_inventory

ACTIVE = "Running"


class _Ops:
    def __init__(self, items):
        self._items = items

    def list(self, *args, **kwargs):
        return list(self._items)


class FakeJob:
    def __init__(self, name, status):
        self.name = name
        self.status = status


class FakeMLClient:
    """Only `jobs` varies; every other listing is empty AND complete, so any
    failed scan in these tests can only have come from the jobs listing."""

    def __init__(self, jobs):
        self.online_endpoints = _Ops([])
        self.online_deployments = _Ops([])
        self.batch_endpoints = _Ops([])
        self.compute = _Ops([])
        self.jobs = _Ops(jobs)


def _jobs_scan(inv):
    return next(s for s in inv.scans if s.section == "jobs")


def test_the_installed_sdk_really_does_return_none_for_a_job_it_cannot_parse():
    """The premise of every other test in this file, checked against the real
    library rather than assumed.

    A previous round shipped a finding built on a fake that had invented SDK
    behaviour, so this pins the behaviour at the boundary: if a future
    azure-ai-ml stops returning `None` here, this fails and the rest of this
    file becomes re-arguable instead of silently pointless.
    """
    pytest.importorskip("azure.ai.ml")
    from azure.ai.ml._restclient.v2024_01_01_preview.models import CommandJob, JobBase
    from azure.ai.ml.operations._job_operations import JobOperations

    props = CommandJob(command="python train.py", environment_id="/e:1")
    # A REST body the entity layer cannot read: `resources` is not a resource
    # object, so `Command._load_from_rest_job` raises inside `_from_rest_object`
    # and that is converted to `JobParsingError`.
    props.resources = "not-a-resource-object"
    rest = JobBase(properties=props)

    class _OpsStub:
        _resolve_azureml_id = staticmethod(lambda job: job)

    assert JobOperations._handle_rest_errors(_OpsStub(), rest) is None


def test_a_none_in_the_job_listing_is_recorded_as_a_listing_that_did_not_complete():
    inv = collect_inventory(FakeMLClient([None]))
    scan = _jobs_scan(inv)
    assert scan.status is ScanStatus.FAILED
    assert not scan.is_evidence
    assert inv.failed_scans, "an unreadable job must reach the report's failed-scan list"


def test_the_detail_for_an_unreadable_job_counts_it_without_naming_what_it_was():
    """`_handle_rest_errors` returns a bare `None`, so the name and status never
    reach this process. The sentence may not imply it knows more than a count."""
    inv = collect_inventory(FakeMLClient([None, None]))
    detail = _jobs_scan(inv).detail
    assert "2 job(s)" in detail
    assert "unknown" in detail


def test_a_job_that_parsed_still_produces_its_row_when_a_sibling_job_did_not():
    """The mixed case, and the reason this is not fixed by raising: a raise would
    leave the section and discard the row for the job that WAS read."""
    inv = collect_inventory(FakeMLClient([FakeJob("hung-a100-job", ACTIVE), None]))
    assert [i.name for i in inv.items if i.kind == "job"] == ["hung-a100-job"]
    assert _jobs_scan(inv).status is ScanStatus.FAILED


def test_a_job_listing_with_no_holes_in_it_is_still_a_complete_read():
    """Over-correction guard. A workspace whose jobs all parsed must keep
    exiting 0, or this fix has replaced a false all-clear with a false alarm."""
    inv = collect_inventory(FakeMLClient([FakeJob("done", "Completed")]))
    assert _jobs_scan(inv).status is ScanStatus.OK
    assert _jobs_scan(inv).is_evidence
    assert inv.failed_scans == []


def test_an_empty_job_listing_is_still_a_complete_read():
    inv = collect_inventory(FakeMLClient([]))
    assert _jobs_scan(inv).status is ScanStatus.OK
    assert inv.failed_scans == []


def test_an_unreadable_job_is_not_counted_as_an_active_job():
    """The count must not go the other way either: a `None` is not evidence of a
    running job any more than it is evidence of none, so it produces no row."""
    inv = collect_inventory(FakeMLClient([None]))
    assert [i for i in inv.items if i.kind == "job"] == []
