"""Four places state how many tests this repo has, and they drifted apart.

Round 5 measured 945 and updated `CLAUDE.md:12`; `README.md:84`,
`docs/labs/lab0.md:7` and `docs/labs/lab0.md:44` still said 869. A participant
who runs `uv run pytest` as the first act of Lab 0 reads the number as the
check ("로컬에서 테스트 N개가 통과한다") -- a wrong N there is a green run that
reads as a broken checkout.

This does not, and cannot, verify the number against a live run: pytest is
already running. What it enforces is the property that actually broke -- that
every site states the SAME number -- and it finds new sites by pattern rather
than by a list, so a fifth place to update announces itself.

`docs/JOURNAL.md` is excluded on purpose: it is an append-only log where each
section records what was measured at the time, and old sections are supposed to
keep saying their old numbers.
"""

from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: `945 passed, 2 skipped` in prose or in a comment on the command itself.
_SUMMARY = re.compile(r"(\d+) passed, (\d+) skipped")
#: Lab 0's Korean phrasing, which carries the count without the pytest wording.
_KOREAN = re.compile(r"테스트 (\d+)개가 통과한다")


def _documents() -> list[pathlib.Path]:
    paths = [_ROOT / "README.md"]
    paths += sorted((_ROOT / "docs").rglob("*.md"))
    return [p for p in paths if p.name != "JOURNAL.md"]


def _stated_counts() -> dict[str, tuple[int, int]]:
    found: dict[str, tuple[int, int]] = {}
    for path in _documents():
        text = path.read_text()
        for line_no, line in enumerate(text.splitlines(), 1):
            match = _SUMMARY.search(line)
            if match:
                found[f"{path.name}:{line_no}"] = (int(match.group(1)), int(match.group(2)))
            korean = _KOREAN.search(line)
            if korean:
                found[f"{path.name}:{line_no} (ko)"] = (int(korean.group(1)), -1)
    return found


def test_every_document_that_states_a_test_count_states_the_same_one():
    counts = _stated_counts()
    assert counts, "no document states a test count any more -- has the wording changed?"
    passed = {site: value[0] for site, value in counts.items()}
    assert len(set(passed.values())) == 1, passed


def test_every_document_that_states_a_skip_count_states_the_same_one():
    """The skips are the half nobody looks at, which is how one of them would
    survive a re-count that fixed the other."""
    skipped = {site: value[1] for site, value in _stated_counts().items() if value[1] >= 0}
    assert len(set(skipped.values())) == 1, skipped


def test_the_three_known_sites_are_all_still_found_by_the_patterns():
    """The guard above passes trivially if the regexes stop matching anything.

    These three are the sites round 5 and round 6 had to update by hand; if a
    rewording drops one out of the scan, the drift becomes invisible again.
    """
    sites = set(_stated_counts())
    for expected in ("README.md", "lab0.md"):
        assert any(site.startswith(expected) for site in sites), (expected, sites)
    assert sum(1 for site in sites if site.startswith("lab0.md")) == 2, sites
