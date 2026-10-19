"""Guard: every ``--hosts`` example in the docs and the e2e tests resolves against lab data.

A ``--hosts`` entry written without ``@iface`` asks otto to pick the host's
interface, which only works while the host has at most one. That made the
docs quietly dependent on a fact nothing was watching: when the BusyBox bed
gave ``test1`` a second interface (``bbeth-1350``, needed because a link
endpoint's ip is resolved from the host's ``interfaces`` map and nowhere
else), three published tunnel examples and three e2e tests started failing
with ``ambiguous interface`` — and no gate noticed, because tunnel resolution
against a live host is not in the hostless lane.

This guard is the hostless half of that. It holds the published commands, and
the argv literals the e2e tests spawn ``otto`` with, to the lab data they are
written against, which is where the ambiguity is decided. The e2e literals
joined the scan after the fourth bare call site (``test_reboot_chaos.py``,
#640) sat unnoticed for seven weeks: the sweep that qualified the other three
gated on ``make coverage``, which never runs the chaos lane.

Deliberately NOT a check that the examples run: it asks only the question the
examples silently assume an answer to.
"""

import re
from pathlib import Path

import pytest

from otto.host.remote_host import make_host_id
from tests._fixtures.labdata import flat_hosts
from tests._fixtures.paths import CHECKOUT_ROOT

DOCS = CHECKOUT_ROOT / "docs"
# Scan every ``*.md`` page under docs/ rather than an enumerated list of
# trees, so this guard cannot go stale the next time a tree is renamed,
# promoted, or a new one grows a ``--hosts`` example — only the paths Sphinx
# itself treats as non-page content are excluded (see ``exclude_patterns``
# in docs/conf.py) plus its own build output.
_EXCLUDED_DIRS = {"superpowers", "_build", "_inventories", "examples"}

_HOSTS_RE = re.compile(r"--hosts\s+(\S+)")

# The e2e tests spawn otto with an argv LIST, so the value is the next string
# literal after ``"--hosts"``: ``["tunnel", "add", "--hosts", "test1,test2", ...]``.
E2E = CHECKOUT_ROOT / "tests" / "e2e"
_ARGV_HOSTS_RE = re.compile(r'"--hosts",\s*"([^"]+)"')

# Entries that name something other than a plain lab host, so the interface
# question does not apply to them. Container endpoints carry their address
# from the container, and a placeholder is not a host at all.
_NOT_A_PLAIN_HOST = (".compose.", "<", ">", "[", "]", "{", "}")


def _interface_counts() -> dict[str, int]:
    """host id -> how many interfaces its lab entry declares."""
    hosts = flat_hosts("tech1")
    counts = {}
    for h in hosts:
        # Host ids are built from element/board by otto's own composer, so
        # derive through it rather than re-spelling the rule here (an element
        # carrying an underscore slugs it to a hyphen: zephyr37_lfs -> zephyr37-lfs).
        host_id = make_host_id(h.get("element", ""), h.get("board"), None)
        counts[host_id] = len(h.get("interfaces") or {})
    return counts


def _pages() -> list[Path]:
    """Every doc page under docs/, minus the non-page trees Sphinx excludes."""
    return [
        page
        for page in sorted(DOCS.rglob("*.md"))
        if not _EXCLUDED_DIRS & set(page.relative_to(DOCS).parts[:-1])
    ]


def _sources() -> list[tuple[Path, re.Pattern[str]]]:
    """Every file the guard reads, paired with the ``--hosts`` shape it carries."""
    pages = [(page, _HOSTS_RE) for page in _pages()]
    e2e = [(module, _ARGV_HOSTS_RE) for module in sorted(E2E.rglob("*.py"))]
    return pages + e2e


def _examples() -> list[tuple[Path, str, str]]:
    """Every ``(source, entry, full-value)`` a ``--hosts`` example or argv names."""
    return [
        (source, entry, value)
        for source, rx in _sources()
        for value in rx.findall(source.read_text())
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
    assert any(source.is_relative_to(E2E) for source, _, _ in examples), (
        "no e2e argv literal found; the argv scan broke"
    )


def test_a_multi_homed_host_is_never_named_without_its_interface() -> None:
    """A bare entry naming a host with two interfaces is a published command
    that exits 1 with ``ambiguous interface``."""
    counts = _interface_counts()
    offenders = []
    for source, entry, value in _examples():
        if "@" in entry:
            continue
        count = counts.get(entry)
        if count is not None and count > 1:
            offenders.append(
                f"{source.relative_to(CHECKOUT_ROOT)}: --hosts {value} (entry {entry!r})"
            )
    assert not offenders, (
        "these documented or spawned commands name a multi-homed host without an "
        "interface and fail with 'ambiguous interface':\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("entry", ["test1", "test1@eth2"])
def test_the_guard_can_tell_the_two_apart(entry: str) -> None:
    """Discriminator: test1 really is the multi-homed host this pins, and the
    ``@iface`` form really is what clears it.

    Pinned here rather than assumed, because if test1 ever drops back to one
    interface the guard above keeps passing while watching nothing — and this
    fails loudly instead."""
    counts = _interface_counts()
    assert counts.get("test1", 0) > 1, (
        "test1 is no longer multi-homed — the guard above is now vacuous "
        "and this parametrization should be repointed at whatever host is"
    )
    ambiguous = "@" not in entry and counts.get(entry.split("@", maxsplit=1)[0], 0) > 1
    assert ambiguous == (entry == "test1")
