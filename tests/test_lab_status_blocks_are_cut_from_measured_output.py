"""A lab's `status` 기대 출력 must be the bytes the tool prints, not a retyping.

Four lab sites quoted the tool as printing `min_instances=0 (lowpriority)`. It
prints `(low_priority)`: the SDK returns snake_case and `collect_inventory` only
lowercases it. The spelling survived one round of "fixed" -- that round reasoned
from an assumed `"LowPriority".lower()` instead of looking at output -- and was
caught by a real Azure run, which is the most expensive possible way to find a
typo in a doc.

The root cause was upstream of the typo: docs/PERFORMANCE.md carried no `status`
output at all, so a lab author writing a 기대 출력 block had nothing to cut from
and retyped one. §13 of that file now holds a measured, GUID-redacted capture,
and the two lab blocks whose documented world state matches it are cut from it.

These tests pin both halves: the rendering the labs quote is the rendering the
current build produces, and the blocks that were NOT re-measured still say so
rather than quietly borrowing a capture of a different world state.
"""

from pathlib import Path

from ffsft.azure_ml import AzureTarget
from ffsft.deploy.lifecycle import collect_inventory, format_inventory

_ROOT = Path(__file__).resolve().parents[1]
_DOCS = _ROOT / "docs"

#: The world state docs/PERFORMANCE.md §13 was captured in, and the only one any
#: lab block here is allowed to quote: one idle low-priority cluster, no
#: endpoint, no orphans.
_MEASURED_TARGET = AzureTarget(
    subscription_id="<your-subscription-id>",
    resource_group="rg-ffsft-kc",
    workspace_name="mlw-ffsft",
    location="koreacentral",
)


class _FakeCompute:
    """What `compute.list()` yields for the cluster in that capture.

    `tier` is snake_case because that is what the SDK returns -- the whole point
    of the defect being pinned here.
    """

    name = "gpu-a100-lp"
    size = "Standard_NC24ads_A100_v4"
    min_instances = 0
    tier = "low_priority"
    type = "amlcompute"


class _FakeList:
    def __init__(self, items=()):
        self._items = list(items)

    def list(self, *args, **kwargs):
        return list(self._items)


class _FakeClient:
    def __init__(self):
        self.online_endpoints = _FakeList()
        self.batch_endpoints = _FakeList()
        self.compute = _FakeList([_FakeCompute()])
        self.jobs = _FakeList()


def _measured_rendering() -> str:
    return format_inventory(collect_inventory(_FakeClient()), _MEASURED_TARGET)


def _lab(name: str) -> str:
    return (_DOCS / "labs" / name).read_text()


def test_no_lab_quotes_the_tool_as_printing_lowpriority_without_the_underscore():
    rendering = _measured_rendering()
    assert "(low_priority)" in rendering
    assert "(lowpriority)" not in rendering
    for path in sorted((_DOCS / "labs").glob("*.md")):
        assert "(lowpriority)" not in path.read_text(), path.name


def test_performance_md_carries_the_rendering_the_labs_are_cut_from():
    # The root cause: with no status output in PERFORMANCE.md there was nothing
    # to cut, so the labs were retyped. Delete this and the typo comes back.
    assert _measured_rendering() in (_DOCS / "PERFORMANCE.md").read_text()


def test_lab0_and_lab7_status_blocks_are_byte_identical_to_the_tool_rendering():
    rendering = _measured_rendering()
    assert rendering in _lab("lab0.md")
    assert rendering in _lab("lab7.md")


def test_the_lab7_idle_block_follows_the_echo_line_the_lab_loop_prints():
    # The `==== ` line is the lab's own `echo`, not tool output; the tool's
    # leading blank line has to sit between it and `LOOKED IN:` or the block is
    # not what a participant will see.
    echo = "==== /home/you/.ffsft-env  rg=rg-ffsft-kc  ws=mlw-ffsft  loc=koreacentral\n"
    assert echo + _measured_rendering() in _lab("lab7.md")


#: The sections whose blocks document a world state nobody has re-measured on
#: this build -- an endpoint billing, a post-teardown resource group. Pasting
#: the idle capture into them would be inventing output, so each keeps a banner.
UNMEASURED_SECTIONS = [
    ("lab7.md", "### 2.1 "),
    ("lab7.md", "## 6. "),
    ("lab8.md", "## 7. "),
]


def _section(text: str, heading: str) -> str:
    body = text.split(heading, 1)[1]
    rest = body.split("\n## ", 1)[0]
    return rest.split("\n### ", 1)[0] if heading.startswith("### ") else rest


def test_blocks_that_were_not_remeasured_still_say_they_were_not_remeasured():
    # This was `count(warning) == 2` and `== 1`, which is the shape that blocks
    # the very remediation the comment prescribes: re-measure one of these
    # states, delete its banner with the old block, and the equality fails on a
    # doc that got MORE honest. Anchoring each banner to its own section keeps
    # the guard (a silently deleted banner still fails) and lets a re-measure
    # pass by editing this list, which is the deliberate act.
    warning = "다시 잰 적이 없습니다"
    for name, heading in UNMEASURED_SECTIONS:
        text = _lab(name)
        assert heading in text, (name, heading)
        assert warning in _section(text, heading), (name, heading)
