"""One prefix, one resource group, one region -- and one command to remove it.

The workshop's teardown story is `az group delete`. `ffsft lifecycle down`
stops the meter mid-workshop (it deletes endpoints and scales compute to zero)
and deliberately deletes nothing that cannot be recreated; this module is the
other half, the one that ends the day. It exists because "did I turn everything
off?" was unanswerable while a participant's resources were spread over two
groups in two regions -- the shape this repo grew into by accident, and which
`src/ffsft/deploy/identity.py:411` records the bill for.

The invariant the rest of the repo is built on applies here with the sign
flipped. Elsewhere it is "an empty listing is not proof of an empty world".
In teardown it is: **a delete that could not be verified is not a delete.**
Both halves of `teardown` below refuse to print a clean verdict they did not
measure, and both exit non-zero when the check itself is what failed.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import re
import subprocess
from collections.abc import Callable, Sequence

from .deploy.lifecycle import EXIT_COULD_NOT_LOOK

#: Refusals that are the operator's to fix before anything is attempted.
EXIT_USAGE = 2

#: `infra up` succeeded but the env file already points at a DIFFERENT
#: workspace. The group is real either way; refusing to silently repoint the
#: file is the difference between a participant's later labs going to their own
#: workspace and going to the one they used yesterday.
EXIT_ENV_CONFLICT = 4

#: Something survived the teardown and is still capable of billing or of
#: blocking the next `infra up`. Ranks below EXIT_COULD_NOT_LOOK, for the same
#: reason it does in `lifecycle`: a leftover you found is better news than a
#: listing you never read.
EXIT_LEFTOVER = 3

#: Matches the `@minLength(3) @maxLength(8)` on `infra/main.bicep`, and adds
#: the leading-letter rule the storage and Key Vault name rules impose once
#: the template concatenates the prefix into `st<prefix><suffix>`. Rejecting
#: here rather than at ARM turns a four-minute deployment failure into a line
#: of output.
PREFIX_RE = re.compile(r"^[a-z][a-z0-9]{2,7}$")


#: Returned by `CommandResult.json` when stdout could not be parsed at all.
#: `None` cannot carry that meaning here: `az` legitimately prints `null` for
#: an absent value, so a `None` return would collapse "the command answered
#: null" into "the command produced something I could not read". Same shape,
#: and for the same reason, as `azure_ml.UNREAD`.
UNPARSED = object()


@dataclasses.dataclass(frozen=True)
class CommandResult:
    """What a single `az` invocation did. `rc != 0` is a fact, not an error."""

    rc: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def json(self):
        """Parse stdout, or return `UNPARSED`.

        Callers MUST branch on `is UNPARSED` rather than on falsiness. Handing
        back a bare `None` here is the repo's oldest defect wearing a new hat:
        the caller cannot tell it from a read that succeeded and found nothing,
        and the whole teardown verdict is built on that distinction.
        """
        try:
            return json.loads(self.stdout)
        except (ValueError, TypeError):
            return UNPARSED


Runner = Callable[[Sequence[str]], CommandResult]


def run_az(argv: Sequence[str]) -> CommandResult:
    """Default runner. Injected everywhere below so the tests never shell out."""
    try:
        proc = subprocess.run(  # noqa: S603
            list(argv), capture_output=True, text=True, timeout=1800
        )
    except FileNotFoundError:
        return CommandResult(rc=127, stderr="az not found on PATH")
    except subprocess.TimeoutExpired:
        return CommandResult(rc=124, stderr=f"timed out: {' '.join(argv)}")
    return CommandResult(rc=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def group_name(prefix: str) -> str:
    """The one group. Must agree with `rgName` in `infra/main.bicep`."""
    return f"rg-ffsft-{prefix}"


def workspace_name(prefix: str) -> str:
    """Must agree with `workspaceName` in `infra/workspace.bicep`."""
    return f"mlw-{prefix}"


def template_path() -> pathlib.Path:
    """Locate `infra/main.bicep` relative to this file's repo checkout.

    Deliberately not packaged: a participant reading `infra/main.bicep` at the
    top of the clone is half the point of using IaC for the workshop at all,
    and a copy inside `site-packages` would be the one they never open. The
    caller reports the resolved path when it is missing rather than falling
    back to a template that might not be the one on screen.
    """
    return pathlib.Path(__file__).resolve().parents[2] / "infra" / "main.bicep"


def validate_prefix(prefix: str) -> str | None:
    """Return the complaint, or None. Pure, so the tests can enumerate it."""
    if not prefix:
        return "prefix is required: it names every resource you are about to create"
    if not PREFIX_RE.match(prefix):
        return (
            f"prefix {prefix!r} is not usable: 3-8 characters, lowercase letters and "
            "digits, starting with a letter. Azure storage and Key Vault names are "
            "built from it and reject anything else."
        )
    return None


# --------------------------------------------------------------------------
# up
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ProvisionOutcome:
    resource_group: str = ""
    outputs: dict = dataclasses.field(default_factory=dict)
    lines: tuple[str, ...] = ()
    exit_code: int = 0


def provision(
    prefix: str,
    location: str,
    *,
    runner: Runner = run_az,
    template: pathlib.Path | None = None,
) -> ProvisionOutcome:
    """Create the one group and everything in it. Idempotent -- ARM upserts."""
    complaint = validate_prefix(prefix)
    if complaint:
        return ProvisionOutcome(lines=(complaint,), exit_code=EXIT_USAGE)
    if not location:
        return ProvisionOutcome(
            lines=(
                "location is required. Pick ONE region and put training and serving "
                "both in it -- `ffsft-deploy check` in Lab 0 tells you which regions "
                "have the GPU quota.",
            ),
            exit_code=EXIT_USAGE,
        )

    tpl = template or template_path()
    if not tpl.exists():
        return ProvisionOutcome(
            lines=(f"template not found at {tpl}; run this from the repo checkout",),
            exit_code=EXIT_USAGE,
        )

    rg = group_name(prefix)
    result = runner(
        [
            "az", "deployment", "sub", "create",
            "--name", f"ffsft-{prefix}",
            "--location", location,
            "--template-file", str(tpl),
            "--parameters", f"prefix={prefix}", f"location={location}",
            "-o", "json",
        ]
    )
    if not result.ok:
        return ProvisionOutcome(
            resource_group=rg,
            lines=(
                f"deployment failed (rc={result.rc}).",
                result.stderr.strip() or result.stdout.strip(),
            ),
            exit_code=EXIT_COULD_NOT_LOOK,
        )

    body = result.json()
    if body is UNPARSED or not isinstance(body, dict):
        # The deployment reported success and the outputs did not parse. The
        # resources may well exist; what is certain is that this process cannot
        # name them, and printing an env block assembled from guesses is how a
        # participant ends up pointed at someone else's workspace.
        return ProvisionOutcome(
            resource_group=rg,
            lines=(
                f"deployment reported success but its output did not parse, so the "
                f"names below cannot be read back. Check the portal for {rg}.",
            ),
            exit_code=EXIT_COULD_NOT_LOOK,
        )

    outputs = {
        key: (value or {}).get("value", "")
        for key, value in ((body.get("properties") or {}).get("outputs") or {}).items()
    }
    return ProvisionOutcome(
        resource_group=outputs.get("resourceGroup") or rg,
        outputs=outputs,
        lines=(f"resource group {rg} is up in {location}.",),
    )


#: The variables `infra up` owns. Every OTHER line in the env file belongs to
#: whoever put it there -- Lab 0 §2 writes PATH, AZURE_CONFIG_DIR and
#: FFSFT_TENANT_ID into the same file, and an `infra up` that rewrote the file
#: wholesale would silently drop the isolated CLI profile the rest of the
#: workshop depends on.
MANAGED_KEYS = (
    "FFSFT_SUBSCRIPTION_ID",
    "FFSFT_RESOURCE_GROUP",
    "FFSFT_WORKSPACE",
    "FFSFT_LOCATION",
    "FFSFT_ACR",
)


def env_values(prefix: str, location: str, subscription: str, outputs: dict) -> dict[str, str]:
    """The values every later lab reads, taken from the deployment's own outputs.

    This is the seam that makes the labs one story: Lab 0 writes it, Labs 1-8
    source it, and nothing downstream ever asks a participant to invent a
    resource group again. The names come from what ARM RETURNED -- `uniqueString`
    is an ARM function and any local reimplementation of it would drift silently
    from the storage and Key Vault that actually exist.
    """
    values = {
        "FFSFT_SUBSCRIPTION_ID": subscription,
        "FFSFT_RESOURCE_GROUP": outputs.get("resourceGroup") or group_name(prefix),
        "FFSFT_WORKSPACE": outputs.get("workspaceName") or workspace_name(prefix),
        "FFSFT_LOCATION": location,
    }
    registry = outputs.get("registryName")
    if registry:
        values["FFSFT_ACR"] = registry
    return values


def merge_env(existing: str, values: dict[str, str], *, prefix: str = "") -> tuple[str, list[str]]:
    """Rewrite only the lines this command owns; keep every other line as it was.

    Returns (text, replaced_keys). Position is preserved for keys already
    present, so a participant's file does not reshuffle under them between
    runs and a diff shows only what actually changed.
    """
    lines = existing.splitlines() if existing.strip() else []
    replaced: list[str] = []
    seen: set[str] = set()

    for i, line in enumerate(lines):
        stripped = line.strip()
        for key, value in values.items():
            if stripped.startswith(f"export {key}=") or stripped.startswith(f"{key}="):
                if stripped != f"export {key}={value}":
                    replaced.append(key)
                lines[i] = f"export {key}={value}"
                seen.add(key)
                break

    missing = [k for k in values if k not in seen]
    if missing:
        if lines:
            lines.append("")
        lines.append("# written by `ffsft infra up` -- source this in every lab")
        lines += [f"export {k}={values[k]}" for k in missing]

    teardown_note = f"# teardown: ffsft infra down --prefix {prefix}" if prefix else ""
    if teardown_note and teardown_note not in lines:
        lines = [ln for ln in lines if not ln.startswith("# teardown: ffsft infra down")]
        lines.append(teardown_note)

    return "\n".join(lines) + "\n", replaced


def env_block(prefix: str, location: str, subscription: str, outputs: dict) -> str:
    """The whole file, for the case where there is not one yet."""
    text, _ = merge_env(
        "", env_values(prefix, location, subscription, outputs), prefix=prefix
    )
    return text


# --------------------------------------------------------------------------
# down
# --------------------------------------------------------------------------

#: What ARM says when the group is not there. Matched case-insensitively on
#: stderr because `az group show` exits non-zero for BOTH "it is gone" and "I
#: could not ask", and those two must not share a verdict -- the first is the
#: goal of this command and the second is the one failure it exists to report.
ABSENT_MARKERS = ("resourcegroupnotfound", "could not be found", "was not found")


@dataclasses.dataclass(frozen=True)
class TeardownOutcome:
    """Three separate registers, never merged into one 'clean' boolean.

    `unread` is the could-not-look half: a listing that errored, a body that
    did not parse. `leftover` is the looked-and-it-is-still-there half. An
    empty `leftover` means something only when `unread` is also empty, and the
    exit code below is ordered to say exactly that.
    """

    lines: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    unread: tuple[str, ...] = ()
    leftover: tuple[str, ...] = ()
    exit_code: int = 0


def _absent(result: CommandResult) -> bool:
    blob = f"{result.stderr}\n{result.stdout}".lower()
    return any(marker in blob for marker in ABSENT_MARKERS)


def _deleted_vault_names(runner: Runner, prefix: str) -> tuple[list[str] | None, str]:
    """Vaults in the soft-delete graveyard whose name this workshop would want.

    Returns (names, complaint). `None` names means the graveyard could not be
    read -- distinct from an empty list, which means it was read and is empty.
    A vault left here holds its name for the retention window and is the one
    thing that makes tomorrow's `infra up` fail with a name collision, so it
    is swept independently of the resource-group listing: an earlier `down`
    that deleted the group and then failed to purge leaves nothing in the
    group listing to find.
    """
    result = runner(["az", "keyvault", "list-deleted", "-o", "json"])
    if not result.ok:
        return None, f"soft-deleted Key Vault listing failed (rc={result.rc})"
    body = result.json()
    if not isinstance(body, list):
        return None, "soft-deleted Key Vault listing did not parse as a list"
    wanted = f"kv{prefix}"
    return [
        v.get("name", "")
        for v in body
        if isinstance(v, dict) and str(v.get("name", "")).startswith(wanted)
    ], ""


def teardown(
    prefix: str,
    *,
    runner: Runner = run_az,
    dry_run: bool = True,
) -> TeardownOutcome:
    """Delete the group, then purge the vaults it held, then check both.

    Ordering is load-bearing and not obvious: the vault names are READ FROM
    THE GROUP BEFORE THE GROUP IS DELETED. After the delete there is nothing
    left to enumerate them from, and recomputing them here would mean
    reimplementing ARM's `uniqueString`. That is why a failed pre-delete
    listing REFUSES to delete rather than deleting and hoping: deleting a
    group whose vaults you could not name leaves names held for the retention
    window with no local record of what to purge.
    """
    complaint = validate_prefix(prefix)
    if complaint:
        return TeardownOutcome(lines=(complaint,), exit_code=EXIT_USAGE)

    rg = group_name(prefix)
    lines: list[str] = []
    deleted: list[str] = []
    unread: list[str] = []
    leftover: list[str] = []

    show = runner(["az", "group", "show", "-n", rg, "-o", "json"])
    group_present = show.ok
    location = ""
    if show.ok:
        body = show.json()
        if body is UNPARSED or not isinstance(body, dict):
            unread.append(f"`az group show -n {rg}` succeeded but its body did not parse")
        else:
            location = body.get("location", "")
    elif not _absent(show):
        # Neither "it is gone" nor a body we can act on. Saying "already
        # deleted" here is the exact mistake the repo keeps paying for.
        unread.append(f"`az group show -n {rg}` failed (rc={show.rc}): {show.stderr.strip()}")
        return TeardownOutcome(
            lines=(f"cannot tell whether {rg} exists, so nothing was deleted.",),
            unread=tuple(unread),
            exit_code=EXIT_COULD_NOT_LOOK,
        )
    else:
        lines.append(f"resource group {rg} is already gone.")

    vaults: list[str] = []
    if group_present:
        listing = runner(["az", "resource", "list", "-g", rg, "-o", "json"])
        contents = listing.json() if listing.ok else UNPARSED
        if not isinstance(contents, list):
            unread.append(
                f"`az resource list -g {rg}` did not return a readable list "
                f"(rc={listing.rc})"
            )
            return TeardownOutcome(
                lines=(
                    f"refusing to delete {rg}: its contents could not be listed, and the "
                    "Key Vault names to purge afterwards are only knowable from that "
                    "listing. Fix the listing, then run this again.",
                ),
                unread=tuple(unread),
                exit_code=EXIT_COULD_NOT_LOOK,
            )
        vaults = [
            r.get("name", "")
            for r in contents
            if isinstance(r, dict)
            and str(r.get("type", "")).lower() == "microsoft.keyvault/vaults"
        ]
        lines.append(
            f"{rg} holds {len(contents)} resource(s), "
            f"including {len(vaults)} Key Vault(s)."
        )

        if dry_run:
            # Deliberately NOT an early return. A dry run that stops here never
            # mentions the Key Vault purge, and the purge is the step whose
            # failure blocks tomorrow's `infra up` -- the one thing a rehearsal
            # most needs to show. Fall through to the graveyard sweep below.
            names = ", ".join(sorted(f"{r.get('name')} [{r.get('type')}]" for r in contents))
            lines.append(f"WOULD DELETE: {names or '(nothing)'}")
            lines.append("re-run with --yes to actually delete.")
        else:
            drop = runner(["az", "group", "delete", "-n", rg, "--yes"])
            if not drop.ok:
                leftover.append(f"resource group {rg} (delete failed rc={drop.rc})")
                lines.append((drop.stderr or drop.stdout).strip())
            else:
                deleted.append(f"resource group {rg} and its {len(contents)} resource(s)")

            recheck = runner(["az", "group", "show", "-n", rg, "-o", "json"])
            if recheck.ok:
                leftover.append(f"resource group {rg} still exists after delete")
            elif not _absent(recheck):
                unread.append(
                    f"could not confirm {rg} is gone (rc={recheck.rc}): "
                    f"{recheck.stderr.strip()}"
                )
    elif dry_run:
        lines.append("nothing to delete in the group; re-run with --yes to purge Key Vaults.")

    # The graveyard sweep runs whether or not the group was there: a vault
    # soft-deleted by an earlier run is invisible to every check above and is
    # precisely what makes the next `infra up` collide.
    graveyard, graveyard_complaint = _deleted_vault_names(runner, prefix)
    if graveyard is None:
        unread.append(graveyard_complaint)
    else:
        for name in sorted(set(vaults) | set(graveyard)):
            if dry_run:
                lines.append(f"WOULD PURGE: Key Vault {name}")
                continue
            argv = ["az", "keyvault", "purge", "-n", name]
            if location:
                argv += ["--location", location]
            purge = runner(argv)
            if purge.ok:
                deleted.append(f"Key Vault {name} (purged)")
            else:
                leftover.append(
                    f"Key Vault {name} is soft-deleted and NOT purged "
                    f"(rc={purge.rc}) -- its name stays taken until it is"
                )

    if unread:
        lines.append(
            "this run could not read everything it needed, so it cannot say the "
            "subscription is clean."
        )
        code = EXIT_COULD_NOT_LOOK
    elif leftover:
        code = EXIT_LEFTOVER
    else:
        code = 0
        if not dry_run:
            lines.append(f"{rg} is gone and no Key Vault name from it is still held.")

    return TeardownOutcome(
        lines=tuple(lines),
        deleted=tuple(deleted),
        unread=tuple(unread),
        leftover=tuple(leftover),
        exit_code=code,
    )
