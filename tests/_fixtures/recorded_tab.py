"""The one whole-TAB recorder: a bash TAB through the real shim entry, in a fresh child.

The child records every open, listing and socket by audit hook, and every
``os.stat`` by wrapping it (CPython raises no audit event for a stat), and
reports its whole ``sys.modules``. A fresh process, so a module the TAB
imported is really in it, whatever the test process holds.

The TAB itself travels in the environment the caller passes, as a shell sends
it: ``_OTTO_COMPLETE``, ``COMP_WORDS`` and ``COMP_CWORD``, beside ``OTTOPATH``
and ``OTTO_HOME``.

- :func:`record_cold_tab` records the TAB as it finds the home (a cold cache
  hands over and rebuilds it).
- :func:`record_warm_tab` first establishes the warm starting state every
  recording shares, then proves the TAB took the stat leg.
- :func:`prime_tab` is one unrecorded TAB after a seed, so the ``names``
  marker exists before the first recording.
- :func:`assert_same_cost` compares two recordings' file work.
"""

import json
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from otto import _shim_complete
from tests._fixtures.budget_harness import load_harness

_CHILD = """
import json, os, sys
sys.argv = ["otto"]
import otto
from otto import _shim, _shim_complete
ops = {{"opens": [], "listdirs": [], "sockets": [], "stats": []}}
def hook(event, args):
    if event == "open" and args and isinstance(args[0], str):
        ops["opens"].append(args[0])
    elif event in ("os.listdir", "os.scandir"):
        ops["listdirs"].append(str(args[0]) if args else ".")
    elif event.startswith("socket."):
        ops["sockets"].append(event)
real_stat = os.stat
class CountingStat:  # a callable object, not a function: a class attribute never binds it
    def __call__(self, path, *args, **kwargs):
        ops["stats"].append(str(path))
        return real_stat(path, *args, **kwargs)
os.stat = CountingStat()
sys.addaudithook(hook)
try:
{inject}
    _shim.main()
except SystemExit:
    pass
finally:
    os.stat = real_stat
    ops["modules"] = sorted(sys.modules)
    with open({out!r}, "w") as fh:
        json.dump(ops, fh)
"""
"""The recorded child. ``{inject}`` is the positive controls' seam: code run inside
the recording, before the TAB (an extra ``os.stat``, a forbidden import)."""

_PRIME = "import sys\nsys.argv = ['otto']\nfrom otto import _shim\n_shim.main()\n"
"""The same TAB through the same entry, unrecorded."""

_HARNESS = load_harness()
"""The import-budget harness, which owns the warm stat leg's staging and verdict."""

COST_FIELDS = ("opens", "listdirs", "sockets", "stats")
"""The file work :func:`assert_same_cost` compares, each as a multiset of paths or events."""


@dataclass(frozen=True)
class RecordedTab:
    """What one recorded TAB answered and touched."""

    stdout: str
    opens: list[str]
    listdirs: list[str]
    sockets: list[str]
    stats: list[str]
    modules: list[str]
    """The child's whole ``sys.modules``, sorted, as the TAB left it."""


def _record(tmp_path: Path, env: dict[str, str], name: str, *, inject: str = "") -> RecordedTab:
    """Run the TAB *env* carries through the shim in a child named *name*; return what it did.

    Drops only the child's own ``<name>.ops.json`` open (its report, written
    after the TAB answered); every other path keeps its multiplicity.
    """
    out = tmp_path / f"{name}.ops.json"
    child = tmp_path / f"{name}.py"
    body = "".join(f"    {line}\n" for line in inject.splitlines()) or "    pass\n"
    child.write_text(_CHILD.format(out=str(out), inject=body.rstrip("\n")))
    done = subprocess.run(
        [sys.executable, str(child)], env=env, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    ops = json.loads(out.read_text())
    return RecordedTab(
        stdout=done.stdout,
        opens=[p for p in ops["opens"] if p != str(out)],
        listdirs=ops["listdirs"],
        sockets=ops["sockets"],
        stats=ops["stats"],
        modules=ops["modules"],
    )


def record_cold_tab(tmp_path: Path, env: dict[str, str], name: str) -> RecordedTab:
    """Record the TAB as it finds the home: on a cold cache, the handover that rebuilds it."""
    return _record(tmp_path, env, name)


def _cache_dir(env: dict[str, str]) -> str:
    """The directory of the cache the TAB in *env* reads, where its markers live."""
    return str(Path(_shim_complete.locate_cache(env)).parent)


def tab_marker(env: dict[str, str], which: str) -> Path:
    """The *which* marker (``names`` or ``tests``) of the cache the TAB in *env* reads."""
    return Path(_shim_complete.marker_path(_cache_dir(env), which))


def record_warm_tab(
    tmp_path: Path, env: dict[str, str], name: str, *, inject: str = ""
) -> RecordedTab:
    """Record a warm TAB from the one starting state, and prove it took the stat leg.

    The harness stages the leg and judges it (``stage_stat_leg`` and
    ``stat_leg_ending`` in ``scripts/import_budget.py``), the same pair its
    warm surfaces use: the ``names`` marker backdated, the ``tests`` one
    fresh, and the TAB must create or touch the ``names`` marker.
    """
    stamp = _HARNESS.stage_stat_leg(_cache_dir(env))
    tab = _record(tmp_path, env, name, inject=inject)
    ending = _HARNESS.stat_leg_ending(stamp)
    assert ending is None, ending
    return tab


def prime_tab(env: dict[str, str]) -> None:
    """Run the TAB *env* carries once, unrecorded, so the ``names`` marker exists after a seed.

    Otherwise the first recording is the one that opens the marker to create
    it, and every later one only touches it.
    """
    done = subprocess.run(
        [sys.executable, "-c", _PRIME], env=env, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    names = tab_marker(env, "names")
    assert names.exists(), f"the priming TAB left no names marker ({names})"


def assert_same_cost(a: RecordedTab, b: RecordedTab) -> None:
    """Assert *a* and *b* did the same file work: each :data:`COST_FIELDS` equal as a multiset."""
    differences = []
    for field in COST_FIELDS:
        left, right = Counter(getattr(a, field)), Counter(getattr(b, field))
        if left != right:
            differences.append(
                f"{field}: only in the first {dict(left - right)}, "
                f"only in the second {dict(right - left)}"
            )
    assert not differences, "the two TABs did different file work: " + "; ".join(differences)
