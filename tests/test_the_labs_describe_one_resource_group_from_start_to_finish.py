"""The workshop is one group, and the labs have to keep saying so.

The teardown story only works if there is exactly one thing to delete. Lab 7
ends with `ffsft infra down --prefix <p>`, which deletes the resource group the
prefix names -- one ARM call, one billing boundary. A lab that tells a
participant to `az group create` a second group puts resources somewhere that
command will never look at, and the participant goes home believing the
workshop is torn down. The bill says otherwise: JOURNAL §11 found $41.66/month
still running in a group nobody was looking at.

The same split used to live in a second env file (`~/.ffsft-serve-env`), which
pointed a shell at a different workspace. `ffsft infra up --write-env <path>`
covers the rare two-region case without any lab having to describe two
profiles, so the file name should not come back.

Docs under `docs/labs/` only. `docs/JOURNAL.md` is append-only and its old
sections are supposed to keep describing the world as it was; `docs/RUNBOOK.md`
documents hand operation, including the split case, on purpose.
"""

from __future__ import annotations

import pathlib

_LABS = sorted((pathlib.Path(__file__).resolve().parents[1] / "docs" / "labs").glob("*.md"))


def test_the_lab_directory_is_actually_being_read():
    """Every assertion below passes on an empty list."""
    names = {path.name for path in _LABS}
    assert "README.md" in names, names
    assert len(names) >= 9, names


def test_no_lab_tells_a_participant_to_create_a_second_resource_group():
    offenders = [path.name for path in _LABS if "az group create" in path.read_text()]
    assert offenders == [], offenders


def test_no_lab_sends_a_participant_to_a_second_env_profile():
    offenders = [path.name for path in _LABS if "ffsft-serve-env" in path.read_text()]
    assert offenders == [], offenders


def test_lab_zero_opens_the_group_and_lab_seven_deletes_it():
    """The two ends of the one storyline. Neither is useful without the other:
    a lab 0 that provisions by hand leaves nothing for `infra down` to name."""
    lab0 = (path for path in _LABS if path.name == "lab0.md")
    lab7 = (path for path in _LABS if path.name == "lab7.md")
    assert "ffsft infra up" in next(lab0).read_text()
    assert "ffsft infra down" in next(lab7).read_text()
