"""`ffsft infra down` may only print a clean verdict it actually measured.

The repo's invariant -- "could not look" is not "looked and saw nothing" --
shows up in teardown with the sign flipped: a delete this process could not
CONFIRM is not a delete, and must not exit 0. These tests drive
`ffsft.infra.teardown` with a scripted `az` and assert on the register each
outcome lands in (`deleted` / `leftover` / `unread`) and on the exit code,
because those three are what a participant's "am I done?" rests on.

No network, no Azure, no subprocess: every `az` call is a lookup in a dict.
"""

from __future__ import annotations

import json

from ffsft.deploy.lifecycle import EXIT_COULD_NOT_LOOK
from ffsft.infra import (
    EXIT_LEFTOVER,
    EXIT_USAGE,
    UNPARSED,
    CommandResult,
    env_block,
    env_values,
    group_name,
    merge_env,
    provision,
    teardown,
    validate_prefix,
    workspace_name,
)

RG = "rg-ffsft-demo"
VAULT = "kvdemoaaaaaaaaaaaaa"

GROUP_SHOW_OK = CommandResult(rc=0, stdout=json.dumps({"name": RG, "location": "koreacentral"}))
GROUP_ABSENT = CommandResult(rc=3, stderr=f"ResourceGroupNotFound: '{RG}' could not be found.")
GROUP_UNREADABLE = CommandResult(rc=1, stderr="AADSTS700082: refresh token has expired")

RESOURCES_OK = CommandResult(
    rc=0,
    stdout=json.dumps(
        [
            {"name": VAULT, "type": "Microsoft.KeyVault/vaults"},
            {"name": "mlw-demo", "type": "Microsoft.MachineLearningServices/workspaces"},
        ]
    ),
)
GRAVEYARD_EMPTY = CommandResult(rc=0, stdout="[]")
GRAVEYARD_HOLDS_VAULT = CommandResult(rc=0, stdout=json.dumps([{"name": VAULT}]))
OK = CommandResult(rc=0, stdout="{}")


class ScriptedAz:
    """An `az` whose answers are chosen by the first three words of the call."""

    def __init__(self, script: dict, default: CommandResult | None = None):
        self.script = script
        self.default = default or OK
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        for key, answer in self.script.items():
            if all(word in argv for word in key.split()):
                if isinstance(answer, list):
                    return answer.pop(0) if len(answer) > 1 else answer[0]
                return answer
        return self.default

    def ran(self, *words: str) -> bool:
        return any(all(w in call for w in words) for call in self.calls)


def _script(**overrides):
    base = {
        "group show": GROUP_SHOW_OK,
        "resource list": RESOURCES_OK,
        "group delete": OK,
        "keyvault list-deleted": GRAVEYARD_EMPTY,
        "keyvault purge": OK,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# the invariant itself
# --------------------------------------------------------------------------


def test_a_group_that_could_not_be_read_is_not_reported_as_already_deleted():
    """`az group show` exits non-zero for "it is gone" AND for "I could not ask".

    Collapsing the two is how a teardown prints "already gone" at a participant
    whose token merely expired, leaving a GPU cluster billing behind a
    reassuring sentence.
    """
    az = ScriptedAz(_script(**{"group show": GROUP_UNREADABLE}))
    outcome = teardown("demo", runner=az, dry_run=False)

    assert outcome.exit_code == EXIT_COULD_NOT_LOOK
    assert outcome.unread, "an unreadable group listing must land in `unread`"
    assert not outcome.deleted
    assert not az.ran("group", "delete"), "must not delete what it could not read"


def test_a_group_that_is_genuinely_absent_is_not_reported_as_unreadable():
    """The other half: a real 404 IS a measurement and must not raise the alarm."""
    az = ScriptedAz(_script(**{"group show": GROUP_ABSENT}))
    outcome = teardown("demo", runner=az, dry_run=False)

    assert outcome.exit_code == 0
    assert not outcome.unread
    assert any("already gone" in line for line in outcome.lines)


def test_it_refuses_to_delete_a_group_whose_contents_it_could_not_list():
    """The Key Vault names are only knowable BEFORE the group is deleted.

    Deleting a group whose contents did not list would soft-delete vaults this
    process can no longer name, holding those names for the retention window
    with no local record of what to purge. Refusing is the only safe branch.
    """
    az = ScriptedAz(_script(**{"resource list": CommandResult(rc=1, stderr="throttled")}))
    outcome = teardown("demo", runner=az, dry_run=False)

    assert outcome.exit_code == EXIT_COULD_NOT_LOOK
    assert not az.ran("group", "delete")
    assert any("refusing to delete" in line for line in outcome.lines)


def test_a_group_still_present_after_the_delete_is_a_leftover_not_a_success():
    az = ScriptedAz(
        _script(**{"group show": [GROUP_SHOW_OK, GROUP_SHOW_OK, GROUP_SHOW_OK]})
    )
    outcome = teardown("demo", runner=az, dry_run=False)

    assert outcome.exit_code == EXIT_LEFTOVER
    assert any("still exists" in item for item in outcome.leftover)


def test_an_unread_listing_outranks_a_found_leftover():
    """Same ordering as `lifecycle`: rc=1 beats rc=3.

    A leftover you found is better news than a listing you never read, because
    the second could be hiding any number of the first.
    """
    az = ScriptedAz(
        _script(
            **{
                "keyvault purge": CommandResult(rc=1, stderr="Forbidden"),
                "keyvault list-deleted": CommandResult(rc=1, stderr="throttled"),
            }
        )
    )
    outcome = teardown("demo", runner=az, dry_run=False)

    assert outcome.unread
    assert outcome.exit_code == EXIT_COULD_NOT_LOOK


# --------------------------------------------------------------------------
# the Key Vault graveyard -- what actually blocks tomorrow's `infra up`
# --------------------------------------------------------------------------


def test_the_vaults_are_read_from_the_group_before_the_group_is_deleted():
    az = ScriptedAz(_script())
    teardown("demo", runner=az, dry_run=False)

    order = [" ".join(c) for c in az.calls]
    listed = next(i for i, c in enumerate(order) if "resource list" in c)
    dropped = next(i for i, c in enumerate(order) if "group delete" in c)
    assert listed < dropped, "the vault names must be captured before the delete"
    assert az.ran("keyvault", "purge", VAULT)


def test_a_vault_left_soft_deleted_is_a_leftover_because_its_name_is_still_taken():
    az = ScriptedAz(_script(**{"keyvault purge": CommandResult(rc=1, stderr="Forbidden")}))
    outcome = teardown("demo", runner=az, dry_run=False)

    assert outcome.exit_code == EXIT_LEFTOVER
    assert any(VAULT in item and "NOT purged" in item for item in outcome.leftover)


def test_the_graveyard_is_swept_even_when_the_group_was_already_gone():
    """An earlier `down` that deleted the group and failed to purge leaves
    nothing in the group listing to find -- only the graveyard knows."""
    az = ScriptedAz(
        _script(**{"group show": GROUP_ABSENT, "keyvault list-deleted": GRAVEYARD_HOLDS_VAULT})
    )
    outcome = teardown("demo", runner=az, dry_run=False)

    assert az.ran("keyvault", "purge", VAULT)
    assert any(VAULT in item for item in outcome.deleted)


def test_an_unreadable_graveyard_is_not_an_empty_one():
    az = ScriptedAz(_script(**{"keyvault list-deleted": CommandResult(rc=1, stderr="nope")}))
    outcome = teardown("demo", runner=az, dry_run=False)

    assert outcome.exit_code == EXIT_COULD_NOT_LOOK
    assert not az.ran("keyvault", "purge")


# --------------------------------------------------------------------------
# dry run is the default
# --------------------------------------------------------------------------


def test_the_default_run_deletes_nothing_and_names_what_it_would_delete():
    az = ScriptedAz(_script())
    outcome = teardown("demo", runner=az)

    assert outcome.exit_code == 0
    assert not az.ran("group", "delete")
    assert not az.ran("keyvault", "purge")
    assert any("WOULD DELETE" in line for line in outcome.lines)
    assert any("WOULD PURGE" in line for line in outcome.lines)


# --------------------------------------------------------------------------
# refusals the operator fixes before anything is attempted
# --------------------------------------------------------------------------


def test_a_prefix_azure_would_reject_is_refused_here_rather_than_four_minutes_in():
    for bad in ("", "ab", "toolongprefix", "Demo", "9demo", "de-mo", "demo_1"):
        assert validate_prefix(bad), f"{bad!r} should have been refused"
    for good in ("abc", "demo", "eonlee12"):
        assert validate_prefix(good) is None, f"{good!r} should have been accepted"


def test_a_bad_prefix_stops_both_halves_with_the_usage_code():
    az = ScriptedAz(_script())
    assert teardown("NOPE", runner=az).exit_code == EXIT_USAGE
    assert provision("NOPE", "koreacentral", runner=az).exit_code == EXIT_USAGE
    assert az.calls == [], "nothing may be attempted on a refused prefix"


def test_provision_refuses_without_a_region_rather_than_picking_one():
    az = ScriptedAz(_script())
    outcome = provision("demo", "", runner=az)
    assert outcome.exit_code == EXIT_USAGE
    assert az.calls == []


# --------------------------------------------------------------------------
# names, and the one place they are allowed to come from
# --------------------------------------------------------------------------


def test_the_group_name_agrees_with_the_bicep_template():
    """`group_name` and `rgName` in `infra/main.bicep` are the same string.

    They are written twice -- once in Bicep for ARM, once in Python for the
    teardown -- and a drift between them means `infra down` deletes nothing
    while reporting success against a group that was never created.
    """
    import pathlib

    bicep = (pathlib.Path(__file__).resolve().parents[1] / "infra" / "main.bicep").read_text()
    assert "var rgName = 'rg-ffsft-${prefix}'" in bicep
    assert group_name("demo") == "rg-ffsft-demo"

    workspace = (
        pathlib.Path(__file__).resolve().parents[1] / "infra" / "workspace.bicep"
    ).read_text()
    assert "var workspaceName = 'mlw-${prefix}'" in workspace
    assert workspace_name("demo") == "mlw-demo"


def test_a_deployment_whose_outputs_do_not_parse_does_not_produce_an_env_block():
    """The resources may well exist. What is certain is that this process
    cannot name them, and an env file built from guesses points a participant
    at someone else's workspace."""
    az = ScriptedAz({"deployment sub create": CommandResult(rc=0, stdout="not json")})
    import pathlib

    outcome = provision(
        "demo", "koreacentral", runner=az, template=pathlib.Path(__file__)
    )
    assert outcome.exit_code == EXIT_COULD_NOT_LOOK
    assert outcome.outputs == {}


def test_unparsed_is_a_distinct_answer_from_a_parsed_null():
    """`az` prints `null` for absent values, so `None` cannot also mean
    "did not parse" -- the two would collapse into one verdict."""
    assert CommandResult(rc=0, stdout="null").json() is None
    assert CommandResult(rc=0, stdout="<html>502</html>").json() is UNPARSED
    assert CommandResult(rc=0, stdout="").json() is UNPARSED


# --------------------------------------------------------------------------
# the env file is shared property
# --------------------------------------------------------------------------

LAB0_ENV = """export PATH="$HOME/.local/bin:$PATH"
export AZURE_CONFIG_DIR=$HOME/.azure-ffsft
export FFSFT_SUBSCRIPTION_ID=old-sub
export FFSFT_TENANT_ID=tenant-guid
export FFSFT_LOCATION=koreacentral
export FFSFT_RESOURCE_GROUP=rg-ffsft-kc
export FFSFT_WORKSPACE=mlw-ffsft
echo "profile: TRAIN $FFSFT_LOCATION"
"""


def _values():
    return env_values(
        "demo",
        "polandcentral",
        "new-sub",
        {"resourceGroup": RG, "workspaceName": "mlw-demo", "registryName": "acrdemo"},
    )


def test_writing_the_env_file_keeps_the_lines_lab0_put_there():
    """Lab 0 §2 writes PATH, AZURE_CONFIG_DIR and FFSFT_TENANT_ID into this same
    file. An `infra up` that rewrote it wholesale would drop the isolated CLI
    profile the rest of the workshop runs on -- and the symptom would surface
    three labs later as `az` quietly using the wrong account."""
    text, _ = merge_env(LAB0_ENV, _values(), prefix="demo")

    assert 'export PATH="$HOME/.local/bin:$PATH"' in text
    assert "export AZURE_CONFIG_DIR=$HOME/.azure-ffsft" in text
    assert "export FFSFT_TENANT_ID=tenant-guid" in text
    assert 'echo "profile: TRAIN $FFSFT_LOCATION"' in text


def test_the_managed_variables_are_replaced_in_place_not_appended_twice():
    text, _ = merge_env(LAB0_ENV, _values(), prefix="demo")

    assert text.count("FFSFT_RESOURCE_GROUP=") == 1
    assert text.count("FFSFT_WORKSPACE=") == 1
    assert text.count("FFSFT_LOCATION=") == 1
    assert "export FFSFT_RESOURCE_GROUP=rg-ffsft-demo" in text
    assert "export FFSFT_LOCATION=polandcentral" in text
    assert "rg-ffsft-kc" not in text


def test_a_repoint_is_reported_so_it_cannot_happen_silently():
    _, replaced = merge_env(LAB0_ENV, _values(), prefix="demo")
    assert set(replaced) == {
        "FFSFT_SUBSCRIPTION_ID",
        "FFSFT_RESOURCE_GROUP",
        "FFSFT_WORKSPACE",
        "FFSFT_LOCATION",
    }


def test_rewriting_the_same_values_reports_no_change():
    text, _ = merge_env(LAB0_ENV, _values(), prefix="demo")
    _, replaced = merge_env(text, _values(), prefix="demo")
    assert replaced == []


def test_a_fresh_file_gets_the_whole_block_and_the_teardown_line():
    block = env_block(
        "demo",
        "koreacentral",
        "sub-id",
        {"resourceGroup": RG, "workspaceName": "mlw-demo", "registryName": "acrdemoxyz"},
    )
    assert "export FFSFT_RESOURCE_GROUP=rg-ffsft-demo" in block
    assert "export FFSFT_WORKSPACE=mlw-demo" in block
    assert "export FFSFT_LOCATION=koreacentral" in block
    assert "export FFSFT_SUBSCRIPTION_ID=sub-id" in block
    assert "export FFSFT_ACR=acrdemoxyz" in block
    assert "ffsft infra down --prefix demo" in block
