""""Could not look" must never leave a function as "looked, saw nothing".

This is the codebase's standing invariant (CLAUDE.md, "Invariants that cost real
money to rediscover"). It has now been violated, found and fixed in four
different files -- `deploy/logs.py`, `deploy/lifecycle.py` (twice),
`deploy/preflight.py`, `deploy/probes.py` -- and every one of those was found by
a human re-reading the code. Each round's manual sweep found instances the
previous round's sweep had walked straight past, because a sweep depends on
somebody noticing a shape, and the shape is quiet:

    try:
        rows = client.something.list()
    except Exception:
        return []          # <- the caller cannot tell this from "there are none"

An invariant written per-file is obeyed per-file (JOURNAL S73.7). This module is
the attempt to stop paying for the sweep: it walks every `except` handler under
`src/ffsft/` with `ast` -- stdlib, this repo adds no dependencies -- and flags
the ones that can hand a caller an empty collection or a falsy sentinel built
out of a read that did not happen.

WHAT THIS IS NOT. It is not a lint rule that every handler must satisfy. Plenty
of falsy returns are correct: a 404 that means "definitely absent" is a real
answer, and a `False` that means "this process did not publish" is a fact about
this process rather than about the world. Those live in `ALLOWLIST`, one line of
reasoning each. The detector deliberately still *flags* them, because the review
is the product: a new site of the same shape has to be argued about once, by the
person adding it, rather than found two rounds later by somebody reading for a
different reason.

Getting to zero findings by weakening the detector is the failure mode this
module fears most. `test_the_detector_still_fires_on_the_shapes_it_was_built_for`
runs the walker over the historical defects as literal source strings, through
the same function that scans `src/`, so neutering it turns those red first.

Census when this landed: 62 `except` handlers walked under `src/ffsft/`, 30 of
them flagged -- 22 allowlisted as genuinely fine and 8 recorded in `KNOWN_OPEN`
as real violations this change found but did not fix, in five files no earlier
sweep had covered.

Round 7 (S78) CLOSED TWO of those eight -- `azure_ml.py` (S78.4) and
`deploy/probes.py` (S78.2) -- and separately argued three further sites into the
allowlist: 22 + 3 = 25 allowlisted, 8 - 2 = 6 in `KNOWN_OPEN`. This paragraph
used to say round 7 "fixed three of those", conflating the three ALLOWLIST
additions with the two `KNOWN_OPEN` closures, and 8 - 3 is not 6. JOURNAL S78.5
states it correctly ("KNOWN_OPEN 이 8 → 6"), and the journal is append-only, so
the arithmetic was wrong here and only here. A guard that miscounts its own
ledger in the sentence operators read first is a bad guard -- and this miscount
read as one more violation having been fixed than was.

Round 8 (S79) split `read_identity_grants`'s roleAssignments read out of its
enclosing handler so a truncated listing on one scope stops taking the other
scope's measured reading down with it -- one more gated read, for 26
allowlisted. Round 8 also walked `docker/` for the first time, closed four
undeclared escape hatches, and taught `FALSY_ESCAPES` to follow tuple targets,
which surfaced one further site (`read_sku_availability`, argued below). Live at
that point: 27 allowlisted and 6 in `KNOWN_OPEN`.

Round 9 (S81) split `read_orphans`' three ARM listings out of ONE enclosing
handler, for the mirror-image reason S79 split `read_identity_grants`: one
listing raising discarded the two that had already returned, so a disk that WAS
read was reported as a resource group nobody could look at. The per-listing
handler is one more gated read.

Round 10 deleted `eval/run.py::publish`'s single shared `try` entirely -- it was
the exact bug this module's own docstring describes (JOURNAL §85), just in a
second, unmigrated copy: the metric loop and all three identity tags lived in
one `except`, so the first blocked `log_metric` on this tenant discarded
`eval.model`/`eval.adapter`/`eval.benchmarks` too, not just the deltas.
`publish` now flattens its report and calls the already-fixed
`mlflow_report.publish`, so the function has no `except` of its own left to
allowlist.

Live now: 27 allowlisted and 6 recorded in `KNOWN_OPEN`. The two dict sizes are
pinned below; the live handler and finding counts are not, because every handler
anyone adds anywhere would otherwise edit this paragraph.

Round 8 also built the sibling guard for the shape this one cannot see at all,
`test_no_arm_listing_is_read_one_page_deep_and_called_the_whole_list`: an ARM
read that SUCCEEDS and returns one page of a list. No exception is raised, so
nothing below can find it; the two guards enforce one invariant from two sides.

Housekeeping the walker cannot do for you: when a `KNOWN_OPEN` site is fixed,
delete its entry. A key that no longer matches anything is inert, not an error,
because failing on it would turn somebody else's fix into your red test.

**What this guard cannot see.** Written down because the invariant it enforces
applies to it: a walker that lists what it found, and says nothing about where
it cannot look, is a clean report from an incomplete scan.

Measured, not asserted: `/tmp/audit6/attack_guard.py` runs 19 synthetic swallows
through `_scan_tree` itself. It MISSED 8 of them before round 8 and misses 4
after, and those 4 are exactly the two entries below still marked open. An
undeclared hole is worse than a declared one, so the probe's own labels are used
here.

* The key is `file::function::except type::shape`, so a *second* handler of the
  same shape and caught type in the same function is absolved by the first
  one's entry. No round has found a real instance; nobody has looked for one
  with anything but this walker, which cannot.
* **Open (probe D1, D2).** Only `Name` targets are followed out of a handler --
  including, since round 8, the names inside a tuple target, so
  `entries, scan_complete = [], False` is now visible. `self.rows = []`,
  `state["rows"] = []` and a falsy value assigned to a closure variable are
  still not. Following attribute and subscript targets means deciding when two
  spellings name the same storage, and a wrong answer there flags the corrected
  code in `lifecycle.py::_section`, which assigns `scan.items` in the try and
  `scan.status` in the handler.
* **Open (probe B1, C1).** A read whose emptiness never passes through an
  `except` at all -- an `if not resp.ok: return []`, a `.get("value") or []` on
  a body nobody status-checked -- is out of scope by construction.
  `deploy/probes.py::_key_based_datastores` shipped exactly that second shape
  and this walker did not find it; a test did. The one *class* of it this repo
  has now paid for twice, a truncated ARM listing, has its own guard in
  `test_no_arm_listing_is_read_one_page_deep_and_called_the_whole_list`. The
  general shape is still nobody's.
* **Closed in round 8 (probe A1, A2, A3, I1).** Four ways to hand a caller an
  empty value that `_is_falsy` could not see, every one of which turned a red
  finding green without touching the walker or the allowlist: `return _empty()`,
  `return NOTHING` with `NOTHING = []` at module scope, `return list(NOTHING)`,
  and `return Result()` where `Result.__bool__` returns False. `_module_bindings`
  now resolves one file's own module scope, which is what all four needed. It
  stops at the file boundary: a falsy constant or sentinel class IMPORTED from
  another module is still invisible, and closing that would mean resolving the
  package to decide whether one `return` is empty.
* **Closed in round 8.** `docker/` is now walked (`_ROOTS`).
  `docker/verify_serve.py` had been reported in three separate rounds and fixed
  in none, and the mechanical reason was that it sat outside the only root this
  walker had. `scripts/` is still outside, and is now the only tree this module
  is silent about by omission rather than by argument.
"""

from __future__ import annotations

import ast
import functools
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src" / "ffsft"
_DOCKER = _ROOT / "docker"

#: Every tree this walker is responsible for. `docker/` joined it in round 8:
#: `docker/verify_serve.py` had been reported as a violation in three separate
#: rounds and fixed in none of them, and the reason was mechanical -- it sat
#: outside the only root the walker had, so no run of this guard ever went near
#: it and every round rediscovered it by hand. `scripts/` is still outside, and
#: that is now the only undeclared-by-omission tree left; see the blind spots.
_ROOTS = (_SRC, _DOCKER)

#: Names that, called with no arguments, build an empty container.
_EMPTY_CTORS = frozenset({"list", "dict", "set", "tuple", "frozenset"})

#: Method names that record something into a collection. A handler that calls
#: one of these is writing the gap down (`blind.append(...)` in `cmd_check`), so
#: it is not a silent swallow even when it returns nothing.
_RECORDERS = frozenset({"append", "add", "update", "extend", "setdefault", "insert"})


# --------------------------------------------------------------------------- #
# the walker
# --------------------------------------------------------------------------- #


class _Falsy:
    """The module-level names, functions and classes that are falsy in one file.

    `_is_falsy` was purely local and an adversarial probe walked straight past
    it four ways (round 8, `/tmp/audit6/attack_guard.py`): `return _empty()`,
    `return NOTHING` where `NOTHING = []` sits at module scope, `return
    list(NOTHING)`, and `return Result()` where `Result.__bool__` returns False.
    None of those touches the walker or the allowlist and every one of them
    turns a red finding green, which makes them escape hatches rather than
    blind spots. Resolving one file's own module scope closes all four.

    Deliberately one file's scope and no further. Following an import into
    another module would mean resolving the whole package to decide whether a
    `return` is empty, and the first ambiguity would be argued by widening it
    until it flagged everything.
    """

    __slots__ = ("names", "funcs", "classes")

    def __init__(self, names=(), funcs=(), classes=()):
        self.names = frozenset(names)
        self.funcs = frozenset(funcs)
        self.classes = frozenset(classes)


#: Used by the control tests and by any call site with no module in hand.
_NO_MODULE = _Falsy()


def _returns_of(func: ast.AST) -> list[ast.Return]:
    """`return` statements belonging to `func` itself, not to anything nested."""
    out = []
    for child in _own_nodes(func):
        if isinstance(child, ast.Return):
            out.append(child)
    return out


def _module_bindings(tree: ast.AST) -> _Falsy:
    """Resolve the falsy module-scope bindings of one parsed file."""
    names: set[str] = set()
    # Twice: `A = []` then `B = A` needs the first pass to have landed.
    for _ in range(2):
        for node in tree.body if isinstance(tree, ast.Module) else []:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and _is_falsy(node.value, _Falsy(names)):
                        names.add(target.id)

    funcs: set[str] = set()
    classes: set[str] = set()
    base = _Falsy(names)
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            returns = _returns_of(node)
            # Every exit falsy, at least one exit, and not a generator: a
            # generator's `return` is a StopIteration value, not the result.
            if returns and all(_is_falsy(r.value, base) for r in returns):
                if not any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(node)):
                    funcs.add(node.name)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if item.name not in ("__bool__", "__len__"):
                    continue
                returns = _returns_of(item)
                if returns and all(_is_falsy(r.value, base) for r in returns):
                    classes.add(node.name)
    return _Falsy(names, funcs, classes)


def _is_falsy(node: ast.expr | None, ctx: _Falsy = _NO_MODULE) -> bool:
    """True when `node` is an empty container or a falsy constant *here*.

    Still syntactic, and still refuses to guess: `return build(x)` is not falsy
    just because `build` might return `[]`. What `ctx` adds is the cases where
    the file itself says so -- a module-level `NOTHING = []`, a module-level
    `def _empty(): return []`, a class whose `__bool__` returns False. Those are
    not guesses, they are resolutions, and every one of them was a working way
    to hide a swallow from this walker until round 8.
    """
    if node is None:  # a bare `return`
        return True
    if isinstance(node, ast.Constant):
        value = node.value
        if value is None or value is False:
            return True
        if isinstance(value, bool):  # True
            return False
        if isinstance(value, (int, float)):
            return value == 0
        if isinstance(value, (str, bytes)):
            return len(value) == 0
        return False
    if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
        # An all-falsy tuple counts: `return False, ""` says "not taken, nothing
        # to report", which is a claim. `return None, detail` does not, and that
        # asymmetry is exactly `deploy/probes.py::_name_is_taken`'s two exits.
        return all(_is_falsy(e, ctx) for e in node.elts)
    if isinstance(node, ast.Dict):
        return not node.keys
    if isinstance(node, ast.Name):
        return node.id in ctx.names
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in _EMPTY_CTORS:
            if not node.args and not node.keywords:
                return True
            # `list(NOTHING)` is a copy of an empty container, which is empty.
            # `list(rows)` is not, because `rows` is not resolvable to empty.
            return not node.keywords and all(_is_falsy(a, ctx) for a in node.args)
        return node.func.id in ctx.funcs or node.func.id in ctx.classes
    return False


def _own_nodes(node: ast.AST):
    """Walk `node`, not descending into nested function or lambda bodies."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield child
        yield from _own_nodes(child)


def _handler_nodes(handler: ast.ExceptHandler) -> list[ast.AST]:
    nodes: list[ast.AST] = []
    for stmt in handler.body:
        nodes.append(stmt)
        nodes.extend(_own_nodes(stmt))
    return nodes


def _writes_in(node: ast.AST) -> set[str]:
    """Local names this subtree fills in -- assigned, augmented, or appended to."""
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
            names.add(sub.id)
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr in _RECORDERS
            and isinstance(sub.func.value, ast.Name)
        ):
            names.add(sub.func.value.id)
    return names


def _assigned_pairs(assign: ast.Assign) -> list[tuple[str, ast.expr]]:
    """`(name, value)` for every simple name this `Assign` binds.

    Unpacks tuple targets. `best, best_free = None, 0.0` is the shape
    `train/preflight.py` uses, and following it is what makes that site visible
    at all -- and, from round 8, what makes `entries, scan_complete = [], False`
    visible on the FALSY_ESCAPES side too. Before that, `FALSY_ESCAPES` required
    a single `ast.Name` target, so any swallow that assigned its falsy value as
    half of a tuple was invisible to it while being visible here. One function,
    so the two sides cannot drift apart again.
    """
    pairs: list[tuple[str, ast.expr]] = []
    for target in assign.targets:
        if isinstance(target, ast.Name):
            pairs.append((target.id, assign.value))
        elif (
            isinstance(target, ast.Tuple)
            and isinstance(assign.value, ast.Tuple)
            and len(target.elts) == len(assign.value.elts)
        ):
            for elt, val in zip(target.elts, assign.value.elts, strict=True):
                if isinstance(elt, ast.Name):
                    pairs.append((elt.id, val))
    return pairs


def _falsy_inits_before(func: ast.AST, before_line: int, ctx: _Falsy) -> dict[str, int]:
    """`name -> lineno` for locals initialised to a falsy default before a line."""
    inits: dict[str, int] = {}
    for sub in ast.walk(func):
        if not isinstance(sub, ast.Assign) or sub.lineno >= before_line:
            continue
        for name, value in _assigned_pairs(sub):
            if _is_falsy(value, ctx):
                inits[name] = sub.lineno
    return inits


def _enclosing_loop(func: ast.AST, try_node: ast.Try) -> ast.AST | None:
    """The innermost `for`/`while` in `func` containing `try_node`, if any."""
    found = None
    for sub in ast.walk(func):
        if not isinstance(sub, (ast.For, ast.AsyncFor, ast.While)):
            continue
        if sub.lineno <= try_node.lineno and try_node.end_lineno <= sub.end_lineno:
            if found is None or sub.lineno > found.lineno:
                found = sub
    return found


def _default_left_unfilled(func: ast.AST, try_node: ast.Try, ctx: _Falsy) -> str:
    """Names a silent swallow leaves sitting at their falsy default.

    The name has to be (a) initialised falsy before the guarded region, (b)
    filled by work inside that region -- so the swallow is what skips it -- and
    (c) read after it. All three, or a big function's unrelated `count = 0`
    would make every handler in it a finding.
    """
    loop = _enclosing_loop(func, try_node)
    region_start = loop.lineno if loop is not None else try_node.lineno
    region_end = loop.end_lineno if loop is not None else try_node.end_lineno
    inits = _falsy_inits_before(func, region_start, ctx)
    if not inits:
        return ""
    filled = _writes_in(try_node) | (_writes_in(loop) if loop is not None else set())
    read_after = {
        sub.id
        for sub in ast.walk(func)
        if isinstance(sub, ast.Name)
        and isinstance(sub.ctx, ast.Load)
        and sub.lineno > region_end
    }
    return ", ".join(sorted(set(inits) & filled & read_after))


def _qualname(node: ast.AST, parents: dict[int, ast.AST]) -> str:
    parts: list[str] = []
    cur = parents.get(id(node))
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parts.append(cur.name)
        cur = parents.get(id(cur))
    return ".".join(reversed(parts)) or "<module>"


def _enclosing_function(node: ast.AST, parents: dict[int, ast.AST]) -> ast.AST | None:
    cur = parents.get(id(node))
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
        cur = parents.get(id(cur))
    return None


class Finding:
    """One handler-shaped blind spot, keyed so line drift cannot stale it out."""

    def __init__(self, path: str, func: str, caught: str, kind: str, line: int, what: str):
        self.path = path
        self.func = func
        self.caught = caught
        self.kind = kind
        self.line = line
        self.what = what

    @property
    def key(self) -> str:
        # Not the line number: this file is edited by whoever is fixing the
        # instance it names, and a key that moves with the fix is a key that
        # goes stale on contact.
        return f"{self.path}::{self.func}::except {self.caught}::{self.kind}"

    def __repr__(self) -> str:
        return f"{self.path}:{self.line} {self.func} except {self.caught} -- {self.kind}"


def _scan_tree(tree: ast.AST, rel: str) -> tuple[list[Finding], int]:
    """The whole walker. Both the repo scan and the two control tests run this.

    One body on purpose: a control test that re-implemented the rules would go
    on passing while the real scan was narrowed to nothing, which is the exact
    failure the controls exist to prevent.
    """
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    # One resolution of this file's own module scope, reused by every handler in
    # it: without it, `return _empty()` and `return NOTHING` are invisible.
    ctx = _module_bindings(tree)

    findings: list[Finding] = []
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    for handler in handlers:
        # A handler that re-raises unconditionally swallows nothing at all.
        if any(isinstance(stmt, ast.Raise) for stmt in handler.body):
            continue

        func = _enclosing_function(handler, parents)
        qual = _qualname(handler, parents)
        caught = ast.unparse(handler.type) if handler.type else "BARE"
        try_node = parents[id(handler)]
        nodes = _handler_nodes(handler)
        kinds: list[tuple[str, int, str]] = []

        # A `-> None` function's bare `return` hands nobody a value -- which was
        # the argument for skipping it, and it is wrong. `-> None` functions are
        # called for their side effects, so an early `return` out of a handler
        # is the caller being told the side effect happened. That is exactly
        # what `azure_ml.grant_compute_data_roles` did (round 7, S78.1): it was
        # annotated `-> None`, returned after a failed identity read, and
        # `ensure_compute` reported the cluster ready. This walker was blind to
        # it by construction, so the exclusion is gone. Measured cost of
        # removing it across `src/ffsft/`: one further site, argued below.
        # Round 8 deleted the last vestige of that exclusion: a
        # `returns_none = False` nothing ever set True, guarding a `continue`
        # that therefore could not run. It read like a live exemption and there
        # was none, which is the same kind of false statement about its own
        # coverage that this module exists to catch in `src/`.
        for node in nodes:
            if isinstance(node, ast.Return) and _is_falsy(node.value, ctx):
                kinds.append(("EMPTY_RETURN", node.lineno, ast.unparse(node)))

        for node in nodes:
            if not isinstance(node, ast.Assign) or func is None:
                continue
            read_later = {
                sub.id
                for sub in ast.walk(func)
                if isinstance(sub, ast.Name)
                and isinstance(sub.ctx, ast.Load)
                and sub.lineno > try_node.end_lineno
            }
            escaping = sorted(
                {
                    name
                    for name, value in _assigned_pairs(node)
                    if _is_falsy(value, ctx) and name in read_later
                }
            )
            if escaping:
                kinds.append(
                    (
                        "FALSY_ESCAPES",
                        node.lineno,
                        f"{ast.unparse(node)}  -- {', '.join(escaping)} read after the handler",
                    )
                )

        records = any(
            isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Return, ast.Raise))
            for n in nodes
        ) or any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in _RECORDERS
            for n in nodes
        )
        if not records and func is not None:
            left = _default_left_unfilled(func, try_node, ctx)
            if left:
                kinds.append(
                    ("SWALLOW_KEEPS_DEFAULT", handler.lineno, f"leaves `{left}` at its default")
                )

        # `except SomeNotFound: pass` over a try body that ran more than one
        # statement cannot know which statement raised. This is the shape that
        # cost `azure_ml.ensure_compute` a cluster: a 404 from the *repair* PUT
        # was read as "the compute does not exist" and the create path PUT a
        # fresh cluster over the same name.
        if len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass):
            if len(try_node.body) > 1:
                kinds.append(
                    (
                        "SWALLOW_WIDER_THAN_THE_READ",
                        handler.lineno,
                        f"`pass` over a try body of {len(try_node.body)} statements",
                    )
                )

        for kind, line, what in kinds:
            findings.append(Finding(rel, qual, caught, kind, line, what))
    return findings, len(handlers)


def _key_path(path: pathlib.Path) -> str:
    """The `file` half of a finding key.

    Files under `src/ffsft/` keep the bare relative path they have always had
    (`deploy/identity.py`), because every key in both dicts below is written
    that way and re-rooting them would invalidate the whole ledger to add one
    directory. Anything else is named from the repo root (`docker/verify_serve.py`).
    The two namespaces cannot collide: `src/ffsft/docker/` does not exist and
    `<repo>/deploy/` does not either.
    """
    if _SRC in path.parents:
        return path.relative_to(_SRC).as_posix()
    return path.relative_to(_ROOT).as_posix()


def _resolve(key_path: str) -> pathlib.Path:
    """The file a key names, whichever root it came from."""
    candidate = _SRC / key_path
    return candidate if candidate.is_file() else _ROOT / key_path


def _display(key_path: str) -> str:
    """What to print so somebody can paste it into an editor."""
    return f"src/ffsft/{key_path}" if (_SRC / key_path).is_file() else key_path


def _scan_module(path: pathlib.Path) -> tuple[list[Finding], int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    return _scan_tree(tree, _key_path(path))


@functools.lru_cache(maxsize=1)
def _scan() -> tuple[tuple[Finding, ...], int]:
    findings: list[Finding] = []
    handlers = 0
    seen: set[pathlib.Path] = set()
    for root in _ROOTS:
        for path in sorted(root.rglob("*.py")):
            if path in seen:
                continue
            seen.add(path)
            got, count = _scan_module(path)
            findings.extend(got)
            handlers += count
    return tuple(findings), handlers


# --------------------------------------------------------------------------- #
# the allowlist -- sites where the falsy value is a measured answer
# --------------------------------------------------------------------------- #

#: key -> why this emptiness is a fact about the world rather than a gap in it.
#: Re-derived by reading each site, not inherited. Two things earn a place here:
#: a not-found error that positively establishes absence, and a falsy value that
#: describes *this process's own action* rather than the world it was reading.
ALLOWLIST: dict[str, str] = {
    "azure_ml.py::ensure_compute::except ResourceNotFoundError::FALSY_ESCAPES": (
        "the try holds one `compute.get` and nothing else, so the 404 is that "
        "cluster's absence and the create path below is the right answer"
    ),
    "deploy/batch.py::read_batch_deployment::except ResourceNotFoundError::EMPTY_RETURN": (
        "round 7: 404 over a single `batch_deployments.get` is that deployment's "
        "absence, and `deployment_replacement_blocker` is written to take `None` "
        "only from this handler -- every other status propagates, so a 403 can "
        "never arrive there as 'there is nothing to overwrite'"
    ),
    "deploy/probes.py::_key_based_datastores::except Exception::EMPTY_RETURN": (
        "round 7 moved this out of KNOWN_OPEN: the site returns `None`, not `[]`, "
        "and None is the documented unread sentinel every caller branches on -- "
        "`classify_store` leaves `key_auth_refused` at None and `cmd_check` prints "
        "the COULD NOT LOOK row. `read_all_arm_pages` raises a listing it could "
        "not finish into this same handler, so a truncated page lands on None too"
    ),
    "deploy/batch.py::ensure_batch_endpoint::except ResourceNotFoundError::FALSY_ESCAPES": (
        "create-on-404 over a single `batch_endpoints.get`; the docstring above "
        "it already argues the case and every other status propagates (S76)"
    ),
    "deploy/endpoint.py::serve_environment::except ResourceNotFoundError::FALSY_ESCAPES": (
        "404 from a single `environments.get` means this image tag has no "
        "registered environment version yet; the next statement registers it"
    ),
    "deploy/endpoint.py::ensure_endpoint::except ResourceNotFoundError::FALSY_ESCAPES": (
        "create-on-404 over a single `online_endpoints.get`: absence is what a "
        "404 states, and `existing is None` only selects create over update"
    ),
    "deploy/identity.py::read_identity_grants::except ImportError::EMPTY_RETURN": (
        "None is this function's documented unread sentinel; `deploy_online` "
        "does `identity_blocker(grants) if grants else None`, so unread is "
        "never rendered as 'the identity holds no roles'"
    ),
    "deploy/identity.py::read_identity_grants::except Exception::EMPTY_RETURN": (
        "same sentinel: a preflight that guesses is worse than no preflight, and "
        "None reaches the caller as 'unknown', not as a finding"
    ),
    "deploy/identity.py::read_identity_grants.roles_on::except Exception::EMPTY_RETURN": (
        "round 8, option 1 and not option 2: `IdentityGrants.acr_roles` is now "
        "tri-state, `None` is its documented unread state, `can_pull_image` "
        "returns None on it and `identity_blocker` fires only on `is False`. "
        "The gap is said out loud by `identity_unread_note` + a log.warning at "
        "the handler, and it is scoped to ONE listing so the other scope's "
        "measured reading survives -- the enclosing handler would have taken it "
        "down too (S79)"
    ),
    "deploy/lifecycle.py::read_orphans.read_listing::except Exception::EMPTY_RETURN": (
        "round 9, option 2: `None` is this reader's documented unread sentinel "
        "and it never leaves `read_orphans` -- the same `except` appends to "
        "`failures`, which sets ScanStatus.FAILED and a detail naming THIS "
        "listing before the function returns, so an unread listing still reads "
        "as unread to `failed_scans`, `blind_spots` and `closing()`'s "
        "`rg_unread`, and rc stays EXIT_COULD_NOT_LOOK. It is scoped to one of "
        "three listings on purpose: the enclosing `_section` handler took the "
        "other two down with it, which discarded a disk that HAD been read "
        "(S81). `None` rather than `[]` is what lets the NIC gap withhold the "
        "attached public IPs instead of calling them orphans"
    ),
    "deploy/lifecycle.py::_nic_name_from_config_id::"
    "except (ValueError, IndexError)::EMPTY_RETURN": (
        "an unparseable ipConfiguration id fails the `nic_name in live_nics` "
        "test, so the IP is *reported* in LEFTOVERS rather than dropped from it "
        "-- the empty name errs loud, which is the direction S11.4 asks for"
    ),
    "deploy/lifecycle.py::effective_sku::except Exception::EMPTY_RETURN": (
        "`cmd_up` answers '' with 'billing rate UNKNOWN to this tool -- this "
        "endpoint is billing anyway', so '' is the unknown branch, not silence"
    ),
    "deploy/preflight.py::read_storage_reachability::except ImportError::EMPTY_RETURN": (
        "the None-means-unread contract this module is built on (CLAUDE.md's "
        "enforcement table); every caller branches on `is None` before reading"
    ),
    "deploy/preflight.py::read_storage_reachability::except Exception::EMPTY_RETURN": (
        "same contract: a transient ARM error must never be the reason a "
        "workable deployment does not happen"
    ),
    "deploy/preflight.py::read_sku_availability::except ImportError::EMPTY_RETURN": (
        "same contract: 'not measured' is not a finding, so None here, and "
        "`sku_advisory`/`online_endpoint_blocker` return None on it"
    ),
    "deploy/preflight.py::read_sku_availability::except Exception::EMPTY_RETURN": (
        "same contract -- an unreadable `restrictions` field stays unread rather "
        "than becoming 'this SKU is not restricted'"
    ),
    "deploy/probes.py::_name_is_taken::except ResourceNotFoundError::EMPTY_RETURN": (
        "404 on `compute.get` is the one answer that proves the probe name is "
        "free; the sibling `except Exception` returns `(None, detail)`, and that "
        "pair of exits is the invariant working, not failing"
    ),
    "deploy/probes.py::_discard_probe::except ResourceNotFoundError::EMPTY_RETURN": (
        "'' means 'no leftover', and already-absent is this function's goal "
        "state: a create refused before the record was written left nothing"
    ),
    "deploy/spec.py::ServingSpec.blocked_reason::except KeyError::EMPTY_RETURN": (
        "the KeyError comes from a lookup in this repo's own SKU core table, not "
        "from a read of Azure -- 'we do not model this SKU' is measured, and the "
        "fallback deliberately declines to block"
    ),
    "deploy/spec.py::ServingSpec.blocked_reason::except KeyError::FALSY_ESCAPES": (
        "`needed = 0` only reaches the blocker sentence when "
        "`dedicated_cores_available` was measured at 0, which is a real reading"
    ),
    "mlflow_report.py::publish::except ImportError::EMPTY_RETURN": (
        "the bool answers 'did this process publish', which this process "
        "measured by failing -- it is not a claim about MLflow's contents"
    ),
    "mlflow_report.py::publish::except Exception::SWALLOW_KEEPS_DEFAULT": (
        "covers all three per-call handlers (log_metric, its tag fallback, and "
        "the plain tag loop): `sent` is a local tally this same function reads "
        "at its own return, never handed to a caller, so a failed write leaving "
        "it unincremented is this call's own measured outcome, not an unread "
        "world -- the metric/tag that failed is still logged by name"
    ),
    "serve/bench_job.py::ensure_bench_environment::except ResourceNotFoundError::FALSY_ESCAPES": (
        "create-on-404 over a single `environments.get`, same shape as "
        "`train/aml_job.ensure_environment`"
    ),
    "serve/bench_report.py::publish_report::except Exception::EMPTY_RETURN": (
        "bool about this process's own publish attempt; reporting must never "
        "fail the run and the report is still on stdout"
    ),
    "train/aml_job.py::ensure_environment::except ResourceNotFoundError::FALSY_ESCAPES": (
        "create-on-404 over a single `environments.get`; the version is derived "
        "from the image tag, so a 404 means that exact version is unregistered"
    ),
    "deploy/preflight.py::read_sku_availability::except TruncatedListing::FALSY_ESCAPES": (
        "round 8, newly visible once FALSY_ESCAPES started following TUPLE "
        "targets: `entries, scan_complete = [], False` is the CORRECTION S78.3 "
        "made, not the defect -- `scan_complete=False` is the flag that stops "
        "the full-scan negative below from being stated, and `entries=[]` is "
        "only ever iterated under it. The walker flags it because it cannot "
        "tell a recorded gap from an unrecorded one at this altitude, and that "
        "is the trade this module already takes everywhere else"
    ),
    "azure_ml.py::ensure_workspace::except ResourceNotFoundError::SWALLOW_WIDER_THAN_THE_READ": (
        "the try's second statement is `return ws.id`, an attribute read on the "
        "value the guarded call just returned -- it cannot raise the caught type"
    ),
}


#: key -> what is actually wrong, and who is carrying it. These are NOT
#: exemptions. They are violations the walker found that this change did not
#: fix, listed so the guard can land without hiding them. This dict may only
#: shrink; anything added to it needs a reason naming the round that found it.
KNOWN_OPEN: dict[str, str] = {
    "data/korean.py::load_sft_dataset::except Exception::SWALLOW_KEEPS_DEFAULT": (
        "round 6: a dataset that fails to load is `continue`d, so the returned "
        "mix silently has fewer sources than `resolve_mix` promised and every "
        "row count downstream is reported as measured"
    ),
    "deploy/lifecycle.py::_orphaned_nic_names::except AttributeError::SWALLOW_KEEPS_DEFAULT": (
        "round 6: a NIC entry that is not a mapping drops out of the orphan set, "
        "so an IP attached to it reads as live and never reaches LEFTOVERS -- "
        "the quiet direction, and S11.4 is what that costs"
    ),
    "serve/bench_report.py::flatten_smoke::"
    "except (KeyError, IndexError, TypeError)::FALSY_ESCAPES": (
        "round 6: a reply whose JSON shape does not match drops the `reply` key "
        "entirely, and this is the only place a human sees what the model said, "
        "so 'unparseable' and 'said nothing' render identically"
    ),
    "serve/bench_report.py::_read_json::except (OSError, ValueError)::EMPTY_RETURN": (
        "round 6: a corrupt loadtest.json and a loadtest.json the job never "
        "wrote both return None, and the published report carries no marker "
        "telling the two apart"
    ),
    "serve/loadtest.py::_one_request::except json.JSONDecodeError::SWALLOW_KEEPS_DEFAULT": (
        "round 6: an unparseable SSE frame is skipped, so `tokens` can stay 0 "
        "and the request is reported `ok=False, error='no tokens streamed'` -- a "
        "verdict about the server. JOURNAL S55 and S68 are two prior rounds of "
        "this same undercount-reads-as-zero mistake, one field name apart"
    ),
    "train/preflight.py::check_disk::except OSError::SWALLOW_KEEPS_DEFAULT": (
        "round 6: a candidate whose `disk_usage` raises is skipped, and if all "
        "of them raise the self-test prints '-> largest writable: None (0.0 GB "
        "free)' and '!! under 120 GB' -- a verdict about a node it never read"
    ),
}


def _explain(finding: Finding) -> str:
    return "\n".join(
        [
            "",
            f"  {_display(finding.path)}:{finding.line}  in {finding.func}()",
            f"      catches : {finding.caught}",
            f"      shape   : {finding.kind} -- {finding.what}",
            "      why it matters: a caller cannot tell this value apart from the",
            "          same value produced by a read that succeeded and found nothing.",
            "      key     : " + finding.key,
        ]
    )


_WHAT_TO_DO = """
Do ONE of these, in this order of preference:

  1. Give the read a status the caller has to handle. The three shapes already
     in this repo:
       * a `None` that every caller branches on   -- deploy/preflight.py
       * a `(value, detail)` pair where detail names the exception type
                                                  -- deploy/probes.py::_name_is_taken
       * a status object kept next to its rows    -- deploy/lifecycle.py::SectionScan
     Then say so out loud in whatever the operator reads: "UNKNOWN -- could not
     look", never an empty table under a confident sentence.

  2. If the emptiness really is measured -- a 404 that positively establishes
     absence, or a falsy value describing this process's own action rather than
     the world -- add the key to ALLOWLIST in
     tests/test_no_except_handler_hands_a_caller_an_empty_value_it_never_read.py
     with a one-line reason. Argue it; do not inherit it.

  3. If it is a real violation you are not fixing in this change, add it to
     KNOWN_OPEN with the round that found it. That dict may only shrink.

Weakening the walker until it finds nothing is not option 4: the shapes it was
built for are pinned as literal source in
test_the_detector_still_fires_on_the_shapes_it_was_built_for.
"""


def test_no_except_handler_hands_a_caller_an_empty_value_it_never_read():
    """Every flagged handler has been argued about once, by name."""
    findings, _ = _scan()
    known = set(ALLOWLIST) | set(KNOWN_OPEN)
    unreviewed = [f for f in findings if f.key not in known]
    assert not unreviewed, (
        f"\n{len(unreviewed)} except handler(s) can hand a caller an empty value built "
        "out of a read that did not happen, and no one has argued about them yet:\n"
        + "\n".join(_explain(f) for f in unreviewed)
        + "\n"
        + _WHAT_TO_DO
    )


def test_no_site_is_both_allowlisted_and_recorded_as_a_known_violation():
    """A key in both dicts is a site whose reviewer disagreed with themselves."""
    both = sorted(set(ALLOWLIST) & set(KNOWN_OPEN))
    assert not both, both


def test_every_reviewed_site_carries_a_reason_somebody_can_argue_with():
    """A one-word reason is how an allowlist becomes a list of names."""
    thin = {
        key: reason
        for key, reason in {**ALLOWLIST, **KNOWN_OPEN}.items()
        if len(reason.split()) < 8
    }
    assert not thin, thin


def test_the_detector_still_fires_on_the_shapes_it_was_built_for():
    """The anti-weakening control.

    Each of these is a shape this codebase actually shipped. If a future edit
    tunes the walker until `src/ffsft` is clean, these go red first and say why.
    """
    cases = {
        "the empty list a caller reads as 'there are none'": (
            "def f(client):\n"
            "    try:\n"
            "        return [d for d in client.datastores.list()]\n"
            "    except Exception:\n"
            "        log.warning('could not read datastores')\n"
            "        return []\n"
        ),
        "the pre-initialised collection a swallow leaves empty": (
            "def f(items):\n"
            "    out = []\n"
            "    for i in items:\n"
            "        try:\n"
            "            out.append(read(i))\n"
            "        except OSError:\n"
            "            continue\n"
            "    return out\n"
        ),
        "the falsy local that escapes the handler": (
            "def f(client):\n"
            "    try:\n"
            "        rows = client.list()\n"
            "    except Exception:\n"
            "        rows = []\n"
            "    return summarise(rows)\n"
        ),
        "the `pass` that cannot know which call raised": (
            "def f(client):\n"
            "    try:\n"
            "        existing = client.get(name)\n"
            "        existing.identity = repair()\n"
            "        return client.put(existing).result().name\n"
            "    except ResourceNotFoundError:\n"
            "        pass\n"
            "    return client.put(fresh()).result().name\n"
        ),
        "the unreadable-file skip docker/verify_serve.py shipped": (
            "def main():\n"
            "    corpus = []\n"
            "    for path in package_root.rglob('*.py'):\n"
            "        try:\n"
            "            corpus.append(path.read_text(errors='ignore'))\n"
            "        except OSError:\n"
            "            continue\n"
            "    blob = '\\n'.join(corpus)\n"
            "    print(f'scanned {len(corpus)} vllm source files')\n"
            "    return 0 if all(f in blob for f in REQUIRED_FLAGS) else 1\n"
        ),
        "the escape hatch of a helper that builds the empty value": (
            "def _empty():\n"
            "    return []\n"
            "def list_disks(client):\n"
            "    try:\n"
            "        return [d.name for d in client.disks.list()]\n"
            "    except Exception:\n"
            "        return _empty()\n"
        ),
        "the escape hatch of a module-level empty constant": (
            "NOTHING = []\n"
            "def list_disks(client):\n"
            "    try:\n"
            "        return [d.name for d in client.disks.list()]\n"
            "    except Exception:\n"
            "        return list(NOTHING)\n"
        ),
        "the escape hatch of an object that is falsy by __bool__": (
            "class Result:\n"
            "    def __bool__(self):\n"
            "        return False\n"
            "def read(client):\n"
            "    try:\n"
            "        return client.get()\n"
            "    except Exception:\n"
            "        return Result()\n"
        ),
        "the falsy half of a tuple assignment escaping the handler": (
            "def f(client):\n"
            "    rows, complete = None, True\n"
            "    try:\n"
            "        rows = list(client.list())\n"
            "    except Exception:\n"
            "        rows, complete = [], True\n"
            "    return summarise(rows, complete)\n"
        ),
        "the all-falsy tuple": (
            "def f(client):\n"
            "    try:\n"
            "        client.get(name)\n"
            "        return True, ''\n"
            "    except Exception:\n"
            "        return False, ''\n"
        ),
    }
    missed = [label for label, source in cases.items() if not _findings_for_source(source)]
    assert not missed, (
        "the walker no longer sees shapes this repo has already paid for: " + repr(missed)
    )


def test_the_detector_leaves_a_read_that_names_its_own_failure_alone():
    """The precision control -- the fixes this repo made must not read as bugs.

    A guard that flags the corrected code too teaches nobody anything, and is
    the version that gets deleted.
    """
    clean = {
        "the (value, detail) pair probes.py::_name_is_taken returns": (
            "def f(client):\n"
            "    try:\n"
            "        client.get(name)\n"
            "        return True, ''\n"
            "    except Exception as exc:\n"
            "        return None, f'{type(exc).__name__}: {exc}'\n"
        ),
        "the status object lifecycle.py::_section records": (
            "def f(client, scan):\n"
            "    try:\n"
            "        scan.items = list(client.list())\n"
            "    except Exception as exc:\n"
            "        scan.status = ScanStatus.FAILED\n"
            "        scan.detail = f'{type(exc).__name__}: {exc}'\n"
            "    return scan\n"
        ),
        "the blind-spot row cmd_check appends": (
            "def f(specs, client):\n"
            "    blind = []\n"
            "    for s in specs:\n"
            "        try:\n"
            "            print(client.check(s))\n"
            "        except Exception as exc:\n"
            "            blind.append(f'whether {s} can deploy ({exc})')\n"
            "            continue\n"
            "    return blind\n"
        ),
        "a handler that re-raises": (
            "def f(d, key):\n"
            "    try:\n"
            "        return d[key]\n"
            "    except KeyError:\n"
            "        raise KeyError(f'unknown {key}') from None\n"
        ),
    }
    noisy = {label: _findings_for_source(src) for label, src in clean.items()}
    noisy = {label: found for label, found in noisy.items() if found}
    assert not noisy, (
        "the walker flags code that already reports its own failed read: " + repr(noisy)
    )


def test_the_census_this_module_states_is_the_one_it_is_keeping():
    """The prose at the top has to move when the dicts do.

    Static on both sides on purpose. Pinning the *live* finding count instead
    would make every handler added anywhere in `src/ffsft` a failure here, and a
    guard that cries wolf on unrelated work is a guard that gets deleted.
    """
    assert __doc__ is not None
    # The message is the whole point of this test. A bare `AssertionError: 7` is
    # what somebody who has just DELETED a fixed KNOWN_OPEN entry sees, exactly
    # as this module's own housekeeping paragraph told them to, and it reads as
    # the guard punishing them for it.
    assert f"{len(ALLOWLIST)} allowlisted" in __doc__, (
        f"ALLOWLIST now holds {len(ALLOWLIST)} entries and the census paragraph at "
        "the top of this module still names a different number. Update the prose; "
        "the dicts are the record and this line only keeps them in step."
    )
    assert f"{len(KNOWN_OPEN)} recorded in `KNOWN_OPEN`" in __doc__, (
        f"KNOWN_OPEN now holds {len(KNOWN_OPEN)} entries and the census paragraph "
        "at the top of this module still names a different number. If you just "
        "deleted a fixed entry, that is the housekeeping this module asks for -- "
        "edit the paragraph to match rather than putting the entry back."
    )


def test_the_walker_still_reaches_the_whole_tree_and_still_finds_things():
    """Zero findings is what a narrowed detector and a deleted one both look like.

    The brief this module was written from names it as the failure mode: it is
    trivial to make the repo scan green by tightening the rules until nothing
    matches. The source-string controls above catch that from one side; the
    breadth floors here catch it from the other.
    """
    findings, handlers = _scan()
    assert len(list(_SRC.rglob("*.py"))) >= 30, "the source tree moved"
    assert handlers >= 50, f"only {handlers} handlers walked -- is the tree still there?"
    files = sorted({f.path for f in findings})
    assert len(files) >= 8, files
    # Round 8: the reason `docker/verify_serve.py` survived three reports is
    # that nothing walked it. Dropping a root is the cheapest possible way to
    # make this guard quiet again, so the roots are asserted, not assumed.
    assert _DOCKER in _ROOTS and _SRC in _ROOTS, _ROOTS
    walked = {
        _key_path(path) for root in _ROOTS for path in root.rglob("*.py")
    }
    assert "docker/verify_serve.py" in walked, sorted(walked)


def test_no_reviewed_site_names_a_file_that_has_since_disappeared():
    """The one kind of rot that is safe to fail on.

    A stale key inside a file that still exists may just mean somebody fixed the
    site, which is good news and must not be a red test. A key naming a deleted
    or renamed module is different: nothing will ever match it again.
    """
    missing = sorted(
        {
            key.split("::", 1)[0]
            for key in (*ALLOWLIST, *KNOWN_OPEN)
            if not _resolve(key.split("::", 1)[0]).is_file()
        }
    )
    assert not missing, missing


def _findings_for_source(source: str) -> list[str]:
    """Run the real walker over a source string, for the two control tests."""
    findings, _ = _scan_tree(ast.parse(source), "<inline>")
    return [f"{f.kind} {f.what}" for f in findings]
