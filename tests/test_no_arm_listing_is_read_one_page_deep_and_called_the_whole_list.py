"""A successful read of PART of an ARM list must never be reported as the list.

The sibling guard,
`test_no_except_handler_hands_a_caller_an_empty_value_it_never_read`, enforces
this repo's standing invariant -- "could not look" must never leave a function
as "looked, saw nothing" -- for exactly ONE shape: emptiness that passes through
an `except`. Its own docstring lists as blind spot #3 the shape it cannot see:

    A read whose emptiness never passes through an `except` at all [...] is out
    of scope by construction.

Round 6's audit then found the invariant violated in precisely that blind spot,
twice, and neither instance raises anything. ARM answers a list one page at a
time:

    {"value": [ ... ], "nextLink": "https://management.azure.com/...&$skipToken=..."}

`nextLink` is absent when the page is the last one. A caller that reads
`resp.json()["value"]` and drops `nextLink` gets HTTP 200, no exception, a
short list, and then states a full-scan negative over it -- "this SKU is not
offered", "there are no orphans", "the identity holds no roles". The emptiness
is reported as a measured fact and nothing anywhere says otherwise. It is the
same invariant as the swallow guard's; it is a different shape, so it needs a
different detector.

Verified offline against the SDK actually installed in this venv rather than
from memory: `azure-ai-ml` 1.34.1 ships the generated ARM models, and
`azure/ai/ml/_restclient/v2024_01_01_preview/models/_models_py3.py:12523`
declares

    class DatastoreResourceArmPaginatedResult(msrest.serialization.Model):
        _attribute_map = {
            'next_link': {'key': 'nextLink', 'type': 'str'},
            'value': {'key': 'value', 'type': '[Datastore]'},
        }

-- the `value`/`nextLink` envelope this module keys on. What is NOT verified
here: whether any particular resource provider paginates at any particular
page size. `azure-mgmt-authorization`, `-compute` and `-network` are not
installed, this repo has no Azure access, and inventing a page size would be
inventing an API response. The guard therefore never asserts that a given
listing WILL be paged; it asserts that the code does not depend on it not being.

WHAT IT CHECKS. Every HTTP GET under `src/ffsft/` that reaches
`management.azure.com` is classified by what the caller does with the parsed
body's TOP-LEVEL keys:

  * reads `value`                -> it is consuming an ARM listing. Unless it
                                    went through `read_all_arm_pages`, it is
                                    reading one page and calling it the list.
  * reads other keys only        -> a single-resource GET. `Microsoft.Quota`'s
                                    `properties.limit.value` is the reason only
                                    TOP-LEVEL keys count: that `value` is a core
                                    count, not a page of items.
  * the walker cannot see either -> a finding, not a pass. A guard that reports
                                    only what it could follow is this repo's own
                                    failure mode wearing a lab coat.

`read_all_arm_pages` (src/ffsft/deploy/preflight.py:76) is the fix: it follows
`nextLink`, caps the walk at `MAX_ARM_PAGES`, refuses a `nextLink` it has
already fetched, and raises `TruncatedListing` rather than returning a short
list. Seven call sites use it as of round 8, and the behaviour is pinned by
`test_a_listing_that_arrived_in_pages_is_not_a_complete_one`.

Census, and the honest part of it. Round 6's audit derived by hand the sweep
this walker mechanises and named three violations:
`identity.py::read_identity_grants.roles_on`, `identity.py::ArmRoleAuth.list_roles`
and `lifecycle.py::read_orphans.fetch`. All three were fixed in round 8 by the
two changes that landed alongside this file -- S79 routed both identity listings
through `read_all_arm_pages` and made the roleAssignments read tri-state, and
`read_orphans.fetch` went the same way -- and all three keys are gone from
`KNOWN_OPEN`, which is the housekeeping the note below asks for.

Which means this guard's first green run is over a tree the defects have already
left, and that run proves nothing about the detector. Two things stand in for
the proof it cannot give:

  * all three sites are pinned as literal PRE-fix source in
    `test_the_listing_detector_still_fires_on_the_shapes_it_was_built_for`, run
    through the same `_scan_tree` the repo scan uses, so the walker is measured
    against the defects rather than against their absence; and
  * the three fixes were re-executed here against fakes built for this file
    rather than taken from anybody's report. A 2-page `roleAssignments` listing
    with `AcrPull` on page 2 gives `list_roles -> ['Reader', 'AcrPull']` over 2
    GETs and `read_identity_grants -> acr_roles = ['Reader', 'AcrPull']`; a
    2-page disk listing with the leftover on page 2 gives
    `read_orphans -> ['leftover-osdisk']` over 4 GETs. Each of those answered
    with page one before, and nothing said so.

Live when this landed: 8 ARM GETs walked and 7 `read_all_arm_pages` call sites,
1 allowlisted and 0 recorded in `KNOWN_OPEN`. The two dict sizes are pinned
below; the live counts are not, for the same reason the sibling guard does not
pin its own -- every ARM read anyone adds would otherwise edit this paragraph.

**What this guard cannot see.** Written down because the invariant it enforces
applies to it:

* Only `requests`-shaped GETs are walked (`<module>.get(url, ...)` where the
  module name is in `_HTTP_MODULES`). A listing read through a session object,
  `urllib`, or a helper this module does not know the name of is invisible.
* The body is followed through at most a chain of local `Name` bindings inside
  the one enclosing function. A body handed to a helper, stored on `self`, or
  returned to a caller that reads `value` there is not followed -- it lands in
  `BODY_THE_WALKER_COULD_NOT_FOLLOW` rather than being cleared, which is the
  safe direction, but the finding names the GET and not the real reader.
* SDK `.list()` calls are out of scope entirely. They return `ItemPaged`, which
  fetches the next page on iteration, so the truncation cannot happen there --
  but only while the result is actually iterated. `list(client.x.list())[:20]`
  is fine; a `.list()` whose ItemPaged is discarded after one `next()` is not,
  and this walker would not know.
* A hand-rolled `while next_link:` loop that is subtly wrong reads to this
  walker exactly like the single-page bug, which is deliberate -- there are no
  such loops in `src/ffsft/` outside `read_all_arm_pages`, and one added later
  should have to argue for itself here.
* Whether a URL is an ARM *collection* is never inferred from its path. It is
  read off what the caller does with the response, because f-strings assembled
  from three local variables do not have a readable tail and guessing at one
  would flag `?api-version=` on every single-resource GET in the tree.
* The root is `src/ffsft/` alone, and that is a **measured** claim rather than
  an assumed one. `docker/verify_serve.py` sat outside the sibling guard's root
  for three rounds and was reported and not fixed each time, purely because
  nothing ever went red over it -- so this root was checked instead of trusted:
  `grep -rn management.azure.com --include=*.py` over `scripts/`, `docker/` and
  `notebooks/` returns nothing today. If an ARM read is ever added to one of
  them, this walker will not see it and will not say so. Widen `_SRC` then.
"""

from __future__ import annotations

import ast
import functools
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src" / "ffsft"

#: Module names whose `.get(url, ...)` is an outbound HTTP GET. Deliberately a
#: name list rather than a guess: `body.get("value")` is also `X.get(...)` and
#: the difference between the two is the whole detector.
_HTTP_MODULES = frozenset({"requests", "requests_mod", "httpx", "session"})

#: The helper that follows `nextLink`. A call to it is what "this listing was
#: read to the end" looks like in this codebase.
_PAGINATOR = "read_all_arm_pages"

#: The host every ARM control-plane read goes to.
_ARM_HOST = "management.azure.com"

#: The top-level key an `*ArmPaginatedResult` carries its items in. Reading it
#: is what makes a GET a listing read.
_ITEMS_KEY = "value"


# --------------------------------------------------------------------------- #
# the walker
# --------------------------------------------------------------------------- #


def _parents(tree: ast.AST) -> dict[int, ast.AST]:
    out: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            out[id(child)] = node
    return out


def _string_bindings(tree: ast.AST) -> dict[str, list[ast.expr]]:
    """`name -> every expression assigned to it` anywhere in the module.

    Module-wide rather than per-scope on purpose. `read_orphans` builds `base`
    in the outer function and the GET lives in a nested `fetch`, so a
    scope-correct resolver would lose the host and silently reclassify an ARM
    read as "some other host". Over-approximating errs toward flagging, which
    is the direction a guard is allowed to be wrong in.
    """
    out: dict[str, list[ast.expr]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out.setdefault(target.id, []).append(node.value)
    return out


def _literal_text(node: ast.expr | None, binds: dict[str, list[ast.expr]], depth: int = 0) -> str:
    """Every string literal reachable from `node`, concatenated.

    Depth-capped because `next_url = body.get("nextLink")` and `base = f"{base}/x"`
    are both self-referential in the binding map, and an uncapped walk of that
    map does not terminate.
    """
    if node is None or depth > 6:
        return ""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else ""
    if isinstance(node, ast.JoinedStr):
        return "".join(_literal_text(v, binds, depth + 1) for v in node.values)
    if isinstance(node, ast.FormattedValue):
        return _literal_text(node.value, binds, depth + 1)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_text(node.left, binds, depth + 1) + _literal_text(
            node.right, binds, depth + 1
        )
    if isinstance(node, ast.Name):
        return "".join(_literal_text(v, binds, depth + 1) for v in binds.get(node.id, ()))
    return ""


def _enclosing_function(node: ast.AST, parents: dict[int, ast.AST]) -> ast.AST | None:
    cur = parents.get(id(node))
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
        cur = parents.get(id(cur))
    return None


def _qualname(node: ast.AST, parents: dict[int, ast.AST]) -> str:
    parts: list[str] = []
    cur = parents.get(id(node))
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parts.append(cur.name)
        cur = parents.get(id(cur))
    return ".".join(reversed(parts)) or "<module>"


def _is_http_get(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in _HTTP_MODULES
    )


def _is_paginator_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Name):
        return node.func.id == _PAGINATOR
    if isinstance(node.func, ast.Attribute):
        return node.func.attr == _PAGINATOR
    return False


def _top_level_keys(call: ast.Call, func: ast.AST, parents: dict[int, ast.AST]) -> tuple[set, bool]:
    """`(keys, escaped)` -- the parsed body's top-level keys this function reads.

    `escaped` is True when some use of the body was not a top-level key read,
    which is the "and there may be more I could not follow" half of the answer.
    Only the FIRST subscript off `.json()` counts: `read_dedicated_quota` reads
    `resp.json()["properties"]["limit"]["value"]`, and that `value` is a core
    count. Counting nested keys would flag it as a listing and the fix would be
    to paginate a scalar.
    """
    response_names: set[str] = set()
    body_names: set[str] = set()

    def is_response(node: ast.expr) -> bool:
        return node is call or (isinstance(node, ast.Name) and node.id in response_names)

    def is_body(node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "json"
            and is_response(node.func.value)
        )

    # Fixpoint rather than one pass: `resp = requests.get(...)`, then
    # `body = resp.json()`, then `props = body` is three hops and the second
    # cannot be recognised before the first has been.
    for _ in range(4):
        for sub in ast.walk(func):
            if (
                isinstance(sub, ast.Assign)
                and len(sub.targets) == 1
                and isinstance(sub.targets[0], ast.Name)
            ):
                name = sub.targets[0].id
                if is_response(sub.value):
                    response_names.add(name)
                elif is_body(sub.value):
                    body_names.add(name)

    body_uses: list[ast.AST] = []
    for sub in ast.walk(func):
        if is_body(sub):
            body_uses.append(sub)
        elif isinstance(sub, ast.Name) and sub.id in body_names and isinstance(sub.ctx, ast.Load):
            body_uses.append(sub)

    keys: set[str] = set()
    escaped = False
    for use in body_uses:
        parent = parents.get(id(use))
        if (
            isinstance(parent, ast.Subscript)
            and isinstance(parent.slice, ast.Constant)
            and isinstance(parent.slice.value, str)
        ):
            keys.add(parent.slice.value)
        elif isinstance(parent, ast.Attribute) and parent.attr == "get":
            grand = parents.get(id(parent))
            if (
                isinstance(grand, ast.Call)
                and grand.args
                and isinstance(grand.args[0], ast.Constant)
                and isinstance(grand.args[0].value, str)
            ):
                keys.add(grand.args[0].value)
            else:
                escaped = True
        elif isinstance(parent, ast.Assign):
            # The binding that put this body in `body_names`; already followed.
            continue
        else:
            escaped = True
    return keys, escaped


class Finding:
    """One ARM read whose completeness nobody has argued about."""

    def __init__(self, path: str, func: str, kind: str, line: int, what: str):
        self.path = path
        self.func = func
        self.kind = kind
        self.line = line
        self.what = what

    @property
    def key(self) -> str:
        # No line number, for the sibling guard's reason: this file is edited by
        # whoever is fixing the site it names, and a key that moves with the fix
        # goes stale on contact.
        return f"{self.path}::{self.func}::{self.kind}"

    def __repr__(self) -> str:
        return f"{self.path}:{self.line} {self.func} -- {self.kind}"


def _scan_tree(tree: ast.AST, rel: str) -> tuple[list[Finding], int, int]:
    """The whole walker. The repo scan and both control tests run this one body.

    Returns `(findings, arm_gets_walked, paginator_calls)`.
    """
    parents = _parents(tree)
    binds = _string_bindings(tree)
    findings: list[Finding] = []
    arm_gets = 0
    paginated = 0

    for node in ast.walk(tree):
        if _is_paginator_call(node):
            paginated += 1
            continue
        if not _is_http_get(node):
            continue

        arg = node.args[0] if node.args else None
        url = _literal_text(arg, binds)
        # Resolved a SECOND time with the module-wide binding map removed, so
        # `direct` holds only the literals written into the GET's own argument
        # expression. The two differ exactly when the host arrived through a
        # `Name`, and that is the case the branch below must not spend.
        direct = _literal_text(arg, {})
        if _ARM_HOST not in url:
            if "://" in direct:
                # A scheme written AT the call site that names a different host:
                # measured, and not this guard's business.
                continue
            if "://" in url:
                # A scheme reached only through `binds`, which is module-WIDE.
                # When the url is a parameter -- the generic-helper shape, which
                # is `read_all_arm_pages`'s own shape -- any assignment to the
                # same name anywhere in the file answers for it, so an unrelated
                # `url = f"https://{account}.blob.core.windows.net/..."` in a
                # sibling function resolves an ARM listing to a blob host. Round
                # 9 executed that: a probe whose `list_arm_collection(url, ...)`
                # did a one-page `json().get("value", [])` scored
                # `findings=[] arm_gets_walked=0` -- not merely missed, not even
                # COUNTED, so the census read complete while a listing was
                # dropped. `_string_bindings` over-approximates to keep an ARM
                # host findable (that is why it is module-wide, and it is right
                # to be); the bug was spending that same over-approximation as a
                # confident negative in the one direction where it silences the
                # guard. A read that SUCCEEDED but resolved the WRONG thing,
                # reported as a measured fact, is the round-8 invariant turned on
                # round 8's own guard. Fall through: "could not tell" is not
                # "not ARM".
                arm_gets += 1
                findings.append(
                    Finding(
                        rel,
                        _qualname(node, parents),
                        "URL_THE_WALKER_COULD_NOT_RESOLVE",
                        node.lineno,
                        f"host {url[:40]!r} was reached only through a module-wide "
                        f"binding of {ast.unparse(arg)[:40] if arg else '?'}, not from "
                        f"the call site, so it does not rule ARM out",
                    )
                )
                continue
            # No host recovered at all. "Could not tell" is not "not ARM" --
            # that substitution is the exact mistake this module exists to stop,
            # and the one site in this shape is `read_all_arm_pages` itself,
            # whose URL is the `nextLink` it is in the middle of following.
            arm_gets += 1
            findings.append(
                Finding(
                    rel,
                    _qualname(node, parents),
                    "URL_THE_WALKER_COULD_NOT_RESOLVE",
                    node.lineno,
                    f"no host literal reachable from {ast.unparse(arg)[:60]}"
                    if arg is not None
                    else "no url argument",
                )
            )
            continue

        arm_gets += 1
        func = _enclosing_function(node, parents)
        if func is None:
            keys, escaped = set(), True
        else:
            keys, escaped = _top_level_keys(node, func, parents)

        if _ITEMS_KEY in keys:
            findings.append(
                Finding(
                    rel,
                    _qualname(node, parents),
                    "SINGLE_PAGE_LISTING",
                    node.lineno,
                    f"reads the top-level `{_ITEMS_KEY}` array off one GET; "
                    f"top-level keys read: {sorted(keys)}",
                )
            )
        elif escaped or not keys:
            # `escaped` used to be computed, documented as "the 'and there may be
            # more I could not follow' half of the answer", and then spent only
            # on decorating a message -- there was no branch for `keys and
            # escaped`, so a function that read ONE other top-level key locally
            # AND handed the body to a helper that reads `value` was CLEARED.
            # Round 9 executed the most ordinary ARM-reading shape there is --
            # `body = resp.json()`, `if body.get("error"): return None`,
            # `return _rows(body)` where `_rows` does `body.get("value", [])` --
            # and got `keys=['error'] escaped=True -> findings=[]`: the walker
            # KNEW it could not follow the body and cleared the read anyway,
            # while `arm_gets` still counted it, so the census looked complete.
            # That is a guard making a false statement about its own coverage,
            # which is the exact defect round 8 had to correct in the sibling
            # guard's census. Measured before changing: all 7 ARM GETs the live
            # tree clears have `escaped=False`, so this widening flags nothing
            # that exists today and buys no allowlist churn.
            findings.append(
                Finding(
                    rel,
                    _qualname(node, parents),
                    "BODY_THE_WALKER_COULD_NOT_FOLLOW",
                    node.lineno,
                    (
                        f"the body escapes this function after only {sorted(keys)} "
                        f"was read off it here"
                        if keys
                        else "no top-level key read off this body inside this function"
                        + (" (the body escapes it)" if escaped else " (the body is never read)")
                    ),
                )
            )
    return findings, arm_gets, paginated


def _scan_module(path: pathlib.Path) -> tuple[list[Finding], int, int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    return _scan_tree(tree, path.relative_to(_SRC).as_posix())


@functools.lru_cache(maxsize=1)
def _scan() -> tuple[tuple[Finding, ...], int, int]:
    findings: list[Finding] = []
    arm_gets = 0
    paginated = 0
    for path in sorted(_SRC.rglob("*.py")):
        got, gets, pages = _scan_module(path)
        findings.extend(got)
        arm_gets += gets
        paginated += pages
    return tuple(findings), arm_gets, paginated


# --------------------------------------------------------------------------- #
# the allowlist -- reads whose single page is the whole answer
# --------------------------------------------------------------------------- #

#: key -> why one page is the complete answer here. Re-derived by reading the
#: site. The bar is high on purpose: "this list is probably short" is not a
#: reason, because page size is ARM's choice and it is not published.
ALLOWLIST: dict[str, str] = {
    "deploy/preflight.py::read_all_arm_pages::URL_THE_WALKER_COULD_NOT_RESOLVE": (
        "this GET *is* the pagination -- its url is the `nextLink` the loop is "
        "in the middle of following, so no host literal is reachable from it "
        "and none should be; flagging the paginator for being unresolvable is "
        "the walker working, and this is the entry that says so"
    ),
}


#: key -> what is actually wrong and who is carrying it. NOT exemptions. This
#: dict may only shrink; an addition needs the round that found it.
KNOWN_OPEN: dict[str, str] = {
    # Empty, and that is a measurement rather than a starting condition. The
    # last entry to leave was `deploy/lifecycle.py::read_orphans.fetch` -- page
    # one each of disks, public IPs and NICs, under an `orphan_items` that
    # states a full-scan negative over the three, so a disk on page two was a
    # resource still billing that `down` reported as cleaned up. It now routes
    # through `read_all_arm_pages`; re-executed against a fake serving that disk
    # on page 2, `read_orphans` returns it. Keeping the entry after that would
    # be a ledger claiming a live violation that is fixed, which is precisely
    # the false statement round 8 had to correct in the sibling guard's census.
}


def _explain(finding: Finding) -> str:
    return "\n".join(
        [
            "",
            f"  src/ffsft/{finding.path}:{finding.line}  in {finding.func}()",
            f"      shape   : {finding.kind}",
            f"      detail  : {finding.what}",
            "      why it matters: ARM answers a list one page at a time. This read",
            "          succeeded, returned part of the list, and raised nothing, so the",
            "          shortfall reaches the caller as a measured fact.",
            "      key     : " + finding.key,
        ]
    )


_WHAT_TO_DO = """
Route it through `read_all_arm_pages` (src/ffsft/deploy/preflight.py:76), or add
an argued allowlist entry to
tests/test_no_arm_listing_is_read_one_page_deep_and_called_the_whole_list.py.

The helper takes the requests module as its first argument, so the
function-local import pattern this repo uses still works:

    from .preflight import TruncatedListing, read_all_arm_pages
    try:
        items = read_all_arm_pages(requests, url, headers=headers, timeout=30)
    except TruncatedListing as exc:
        ...        # say the listing stopped short; do NOT answer with a short list

It raises `TruncatedListing` rather than returning a short list, so a caller
that already has a "could not look" branch gets the partial read routed into it
for free -- see `deploy/probes.py::_key_based_datastores`, which answers `None`,
and `deploy/preflight.py::read_sku_availability`, which sets `scan_complete`.

If the single page really is the whole answer, say why in ALLOWLIST. "The list
is short" is not a reason: page size is ARM's choice and it is not published.
If it is a real violation you are not fixing here, add it to KNOWN_OPEN with the
round that found it. Weakening the walker is not option 4 -- the shapes it was
built for are pinned as literal source in
test_the_listing_detector_still_fires_on_the_shapes_it_was_built_for.
"""


def test_no_arm_listing_is_read_one_page_deep_and_called_the_whole_list():
    """Every ARM read whose completeness is in doubt has been argued about once."""
    findings, _, _ = _scan()
    known = set(ALLOWLIST) | set(KNOWN_OPEN)
    unreviewed = [f for f in findings if f.key not in known]
    assert not unreviewed, (
        f"\n{len(unreviewed)} ARM read(s) can hand a caller part of a list with no sign "
        "that it is part of one, and no one has argued about them yet:\n"
        + "\n".join(_explain(f) for f in unreviewed)
        + "\n"
        + _WHAT_TO_DO
    )


def test_no_arm_read_is_both_allowlisted_and_recorded_as_a_known_violation():
    """A key in both dicts is a site whose reviewer disagreed with themselves."""
    both = sorted(set(ALLOWLIST) & set(KNOWN_OPEN))
    assert not both, both


def test_every_reviewed_arm_read_carries_a_reason_somebody_can_argue_with():
    """A one-word reason is how an allowlist becomes a list of names."""
    thin = {
        key: reason
        for key, reason in {**ALLOWLIST, **KNOWN_OPEN}.items()
        if len(reason.split()) < 8
    }
    assert not thin, thin


def test_the_listing_detector_still_fires_on_the_shapes_it_was_built_for():
    """The anti-weakening control.

    Each of these is a shape this codebase actually shipped, run through the
    same `_scan_tree` the repo scan uses. Tuning the walker until `src/ffsft`
    is clean turns these red first and says why.
    """
    cases = {
        "the one-page listing lifecycle.py::read_orphans.fetch shipped": (
            "def fetch(path, api):\n"
            "    import requests\n"
            "    base = 'https://management.azure.com/subscriptions/s/resourceGroups/g'\n"
            "    resp = requests.get(f'{base}/{path}?api-version={api}', headers=h, timeout=30)\n"
            "    resp.raise_for_status()\n"
            "    return resp.json().get('value', [])\n"
        ),
        "the one-page listing identity.py::roles_on shipped": (
            "def roles_on(scope):\n"
            "    import requests\n"
            "    resp = requests.get(\n"
            "        f'https://management.azure.com{scope}/providers/"
            "Microsoft.Authorization/roleAssignments?api-version=2022-04-01',\n"
            "        headers=h, timeout=30)\n"
            "    resp.raise_for_status()\n"
            "    return [a['name'] for a in resp.json().get('value', [])]\n"
        ),
        "the subscript spelling of the same read": (
            "def f():\n"
            "    import requests\n"
            "    body = requests.get('https://management.azure.com/subs/x/disks').json()\n"
            "    return body['value']\n"
        ),
        "the inline spelling with no intermediate binding": (
            "def f():\n"
            "    import requests\n"
            "    return requests.get("
            "'https://management.azure.com/subs/x/disks').json()['value']\n"
        ),
        "an ARM GET whose body goes somewhere the walker cannot follow": (
            "def f(store):\n"
            "    import requests\n"
            "    resp = requests.get('https://management.azure.com/subs/x/disks')\n"
            "    store.absorb(resp.json())\n"
        ),
        "an ARM GET whose url is unreadable from here": (
            "def f(url):\n"
            "    import requests\n"
            "    resp = requests.get(url, headers=h)\n"
            "    return resp.json()['value']\n"
        ),
    }
    missed = [label for label, source in cases.items() if not _findings_for_source(source)]
    assert not missed, (
        "the listing walker no longer sees shapes this repo has already paid for: "
        + repr(missed)
    )


def test_the_listing_detector_leaves_a_finished_read_alone():
    """The precision control -- the corrected code must not read as a bug.

    A guard that flags `read_all_arm_pages` callers and single-resource GETs
    teaches nobody anything, and is the version that gets deleted.
    """
    clean = {
        "the paginated read identity.py::acr_id_for_image now does": (
            "def f(subscription_id):\n"
            "    import requests\n"
            "    from .preflight import read_all_arm_pages\n"
            "    return read_all_arm_pages(\n"
            "        requests,\n"
            "        f'https://management.azure.com/subscriptions/{subscription_id}"
            "/providers/Microsoft.ContainerRegistry/registries',\n"
            "        headers=h, timeout=30)\n"
        ),
        "the single-resource GET preflight.py::read_storage_reachability does": (
            "def f(base):\n"
            "    import requests\n"
            "    base = 'https://management.azure.com/subscriptions/s/workspaces/w'\n"
            "    ws = requests.get(f'{base}?api-version=2024-10-01', headers=h, timeout=30)\n"
            "    ws.raise_for_status()\n"
            "    return ws.json().get('properties', {})\n"
        ),
        "the nested `value` probes.py::read_dedicated_quota reads": (
            "def f():\n"
            "    import requests\n"
            "    url = 'https://management.azure.com/subs/s/providers/"
            "Microsoft.Quota/quotas/fam?api-version=2023-02-01'\n"
            "    resp = requests.get(url, headers=h, timeout=60)\n"
            "    resp.raise_for_status()\n"
            "    return int(resp.json()['properties']['limit']['value'])\n"
        ),
        "a GET to a host that is not ARM": (
            "def f(endpoint):\n"
            "    import requests\n"
            "    resp = requests.get('https://scoring.example.com/health', timeout=5)\n"
            "    return resp.json()['value']\n"
        ),
        "a plain dict `.get(\"value\")` with no HTTP anywhere near it": (
            "def f(body):\n"
            "    return body.get('value', [])\n"
        ),
    }
    noisy = {label: _findings_for_source(src) for label, src in clean.items()}
    noisy = {label: found for label, found in noisy.items() if found}
    assert not noisy, (
        "the listing walker flags reads that already finish the listing: " + repr(noisy)
    )


def test_the_census_this_module_states_is_the_one_it_is_keeping():
    """The prose at the top has to move when the dicts do.

    Static on both sides, for the sibling guard's reason: pinning the live
    finding count would make every ARM read added anywhere a failure here.
    """
    assert __doc__ is not None
    assert f"{len(ALLOWLIST)} allowlisted" in __doc__, (
        f"ALLOWLIST now holds {len(ALLOWLIST)} entries and the census paragraph at the "
        "top of this module still names a different number. Update the prose; the "
        "dicts are the record and this line only keeps them in step."
    )
    assert f"{len(KNOWN_OPEN)} recorded in `KNOWN_OPEN`" in __doc__, (
        f"KNOWN_OPEN now holds {len(KNOWN_OPEN)} entries and the census paragraph at "
        "the top of this module still names a different number. If you just deleted a "
        "fixed entry, that is the housekeeping this module asks for -- edit the "
        "paragraph to match rather than putting the entry back."
    )


def test_the_paginating_helper_is_still_there_and_still_used():
    """Zero findings is what a fixed tree and a deleted detector both look like.

    Deleting `read_all_arm_pages` and reverting its four callers to one-page
    reads would make every finding here a fresh `SINGLE_PAGE_LISTING`, so the
    scan above already catches that. This catches the quieter version: the
    helper renamed or the constant `_PAGINATOR` edited so the walker stops
    recognising the calls and scores the tree clean.
    """
    findings, arm_gets, paginated = _scan()
    preflight = (_SRC / "deploy" / "preflight.py").read_text(encoding="utf-8")
    assert f"def {_PAGINATOR}(" in preflight, (
        f"{_PAGINATOR} is gone from deploy/preflight.py, so nothing in this repo "
        "follows a nextLink and this guard is scoring an empty room"
    )
    assert paginated >= 7, (
        f"only {paginated} call site(s) route an ARM listing through {_PAGINATOR}; "
        "seven did when this guard landed and that number may not silently fall. "
        "Reverting one to a bare `requests.get` is the cheapest way to put the "
        "defect back and the scan above would catch it -- this catches the "
        "quieter version, where the helper is renamed and the walker simply "
        "stops recognising the call"
    )
    assert arm_gets >= 8, f"only {arm_gets} ARM GET(s) walked -- is the tree still there?"
    # Deliberately NOT a floor on findings. The whole point of this guard is for
    # its finding count to be able to reach zero; a floor there would turn a fix
    # somebody else lands into a red test in this file.
    assert all(f.key for f in findings), "every finding must be keyed"


def test_no_reviewed_arm_read_names_a_file_that_has_since_disappeared():
    """The one kind of rot that is safe to fail on.

    A stale key inside a file that still exists may just mean somebody fixed the
    site, which is good news and must not be a red test. A key naming a deleted
    module is different: nothing will ever match it again.
    """
    missing = sorted(
        {
            key.split("::", 1)[0]
            for key in (*ALLOWLIST, *KNOWN_OPEN)
            if not (_SRC / key.split("::", 1)[0]).is_file()
        }
    )
    assert not missing, missing


def _findings_for_source(source: str) -> list[str]:
    """Run the real walker over a source string, for the two control tests."""
    findings, _, _ = _scan_tree(ast.parse(source), "<inline>")
    return [f"{f.kind} {f.what}" for f in findings]
