"""Guard: every ``--hosts`` example in the docs still resolves against lab data.

A ``--hosts`` entry written without ``@iface`` asks otto to pick the host's
interface, which only works while the host has at most one. That made the
docs quietly dependent on a fact nothing was watching: when the BusyBox bed
gave ``carrot`` a second interface (``bbeth-1350``, needed because a link
endpoint's ip is resolved from the host's ``interfaces`` map and nowhere
else), three published tunnel examples and three e2e tests started failing
with ``ambiguous interface`` — and no gate noticed, because tunnel resolution
against a live host is not in the hostless lane.

This guard is the hostless half of that. It cannot see the e2e tests, but it
holds the published commands to the lab data they are written against, which
is where the ambiguity is decided. The e2e half needs ``make coverage``.

Deliberately NOT a check that the examples run: it asks only the question the
examples silently assume an answer to.
"""

import json
import re
from pathlib import Path

import pytest

from tests._fixtures.labdata import lab_data_path
from tests._fixtures.paths import PROJECT_ROOT

DOCS = PROJECT_ROOT / "docs" / "guide"

_HOSTS_RE = re.compile(r"--hosts\s+(\S+)")

# Entries that name something other than a plain lab host, so the interface
# question does not apply to them. Container endpoints carry their address
# from the container, and a placeholder is not a host at all.
_NOT_A_PLAIN_HOST = (".compose.", "<", ">", "[", "]", "{", "}")


def _interface_counts() -> dict[str, int]:
    """host id -> how many interfaces its lab entry declares."""
    hosts = json.loads(lab_data_path("tech1").read_text())["hosts"]
    counts = {}
    for h in hosts:
        # Host ids are built from element/board; the docs spell them the same
        # way the lab entry composes them, so derive rather than hardcode.
        element, board = h.get("element", ""), h.get("board", "")
        host_id = f"{element}_{board}" if board else element
        counts[host_id] = len(h.get("interfaces") or {})
    return counts


def _examples() -> list[tuple[Path, str, str]]:
    """Every ``(page, entry, full-value)`` a ``--hosts`` example names."""
    return [
        (page, entry, value)
        for page in sorted(DOCS.rglob("*.md"))
        for value in _HOSTS_RE.findall(page.read_text())
        for entry in value.split(",")
        if entry and not any(t in entry for t in _NOT_A_PLAIN_HOST)
    ]


def test_the_corpus_is_not_empty() -> None:
    """The scan finds examples at all.

    Without this the guard below passes vacuously the day the regex, the docs
    layout or the glob changes — the exact failure mode that makes a guard
    look green while watching nothing.
    """
    examples = _examples()
    assert len(examples) >= 5, f"only {len(examples)} --hosts entries found; the scan broke"
    assert any("@" in e for _, e, _ in examples), "no qualified entry found; the scan broke"
    assert any("@" not in e for _, e, _ in examples), "no bare entry found; nothing to guard"


def test_a_multi_homed_host_is_never_named_without_its_interface() -> None:
    """A bare entry naming a host with two interfaces is a published command
    that exits 1 with ``ambiguous interface``."""
    counts = _interface_counts()
    offenders = []
    for page, entry, value in _examples():
        if "@" in entry:
            continue
        count = counts.get(entry)
        if count is not None and count > 1:
            offenders.append(f"{page.relative_to(DOCS.parent)}: --hosts {value} (entry {entry!r})")
    assert not offenders, (
        "these documented commands name a multi-homed host without an interface "
        "and fail with 'ambiguous interface':\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("entry", ["carrot_seed", "carrot_seed@eth2"])
def test_the_guard_can_tell_the_two_apart(entry: str) -> None:
    """Discriminator: carrot really is the multi-homed host this pins, and the
    ``@iface`` form really is what clears it.

    Pinned here rather than assumed, because if carrot ever drops back to one
    interface the guard above keeps passing while watching nothing — and this
    fails loudly instead."""
    counts = _interface_counts()
    assert counts.get("carrot_seed", 0) > 1, (
        "carrot_seed is no longer multi-homed — the guard above is now vacuous "
        "and this parametrization should be repointed at whatever host is"
    )
    ambiguous = "@" not in entry and counts.get(entry.split("@", maxsplit=1)[0], 0) > 1
    assert ambiguous == (entry == "carrot_seed")
