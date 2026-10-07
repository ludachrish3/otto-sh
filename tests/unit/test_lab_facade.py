"""``otto.lab``: the public home of the lab and fleet API (spec 2026-10-04 §4, D1).

``tests/unit/test_lazy_packages.py`` holds the package to the lazy shape and
checks that every name is its defining module's object, and
``tests/unit/test_import_contracts.py`` that a bare import loads neither
``otto.config`` nor the host classes. This file pins what the declaration
promises: exactly appendix F's names; the root ``otto`` names that D1 keeps
resolving to the same objects; the old ``otto.config`` spellings gone in a
fresh interpreter, whichever import ran first; and a star import of
``otto.lab`` or ``otto.bootstrap`` binding exactly its ``__all__``.
"""

import importlib
import json
import subprocess
import sys

import pytest

import otto
import otto.lab
from scripts.docs_api_reference import (
    build_reference,
    defining_module,
    load_namespaces,
    skip_member,
)

APPENDIX_F = [
    "EmptySelectionError",
    "Lab",
    "all_hosts",
    "do_for_all_hosts",
    "fleet_of_interest",
    "get_host",
    "get_lab",
    "load_lab",
    "run_on_all_hosts",
]


def test_otto_lab_declares_exactly_appendix_f():
    assert sorted(otto.lab.__all__) == sorted(APPENDIX_F)


def test_the_root_names_are_the_otto_lab_objects():
    """The root keeps D1's five names: one object, two declared paths."""
    for name in ["all_hosts", "get_host", "get_lab", "load_lab", "run_on_all_hosts"]:
        assert getattr(otto, name) is getattr(otto.lab, name), name


_OLD_SPELLING_CHECK = """
import otto
import otto.lab

still = []
for name in ["get_lab", "get_repos"]:
    try:
        exec(f"from otto.config import {name}")
    except ImportError:
        continue
    still.append(name)
assert still == [], f"from otto.config import still works for {still}"
assert otto.get_lab is otto.lab.get_lab, "otto.get_lab is not otto.lab.get_lab"
from otto.config.lab import Lab

assert Lab is otto.lab.Lab
"""

# What runs first, alone, in each fresh interpreter, before the check.
_IMPORT_ORDERS = {
    "lab-first": "import otto.lab\n",
    "root-first": "import otto\notto.get_lab\n",
}


@pytest.mark.parametrize("first", sorted(_IMPORT_ORDERS))
def test_the_old_fleet_spelling_is_gone_in_either_import_order(first):
    """Importing ``get_lab`` or ``get_repos`` from ``otto.config`` fails, whatever loaded first.

    A lazy resolver, or a module that binds a name into its parent package when
    it loads, can make an old spelling work only after some other import ran.
    So each order runs alone in a fresh interpreter (spec 2026-10-04 §7: new
    names are imported in fresh interpreters, in both import orders). The
    retired deep path ``otto.config.lab:Lab`` still imports: it is no longer
    public, but no module moved.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _IMPORT_ORDERS[first] + _OLD_SPELLING_CHECK],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr


_STAR = (
    "import importlib, json, sys\n"
    "bound = {}\n"
    "exec(f'from {sys.argv[1]} import *', bound)\n"
    "bound.pop('__builtins__', None)\n"
    "listed = getattr(importlib.import_module(sys.argv[1]), '__all__', None)\n"
    "listed = None if listed is None else sorted(listed)\n"
    "print(json.dumps({'bound': sorted(bound), 'all': listed}))\n"
)


@pytest.mark.parametrize("module", ["otto.bootstrap", "otto.lab"])
def test_star_import_binds_exactly_the_new_all(module):
    """``from <module> import *`` binds the module's ``__all__`` and nothing else.

    ``tests/unit/test_module_all.py`` checks every other appendix-F module the
    same way, one fresh interpreter each.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _STAR, module],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got["all"] is not None, f"{module} has no __all__"
    assert got["bound"] == got["all"]


def test_each_otto_lab_object_is_documented_once():
    """Each of ``otto.lab``'s objects is indexed on exactly one public page: its home.

    ``otto`` holds five of the same objects. Indexing ``get_lab`` on both pages
    gives a bare ``get_lab`` reference two targets, which Sphinx reports as
    ambiguous and ``-W`` makes fatal. The reference's skip rule decides which
    holder documents an object: every other holder lists it under "Also
    exported here", and an Internals page leaves it out.
    """
    ref = build_reference(load_namespaces())
    for name in otto.lab.__all__:
        obj = getattr(otto.lab, name)
        assert id(obj) in ref.homes, f"otto.lab.{name} has no documented home"
        holders = []
        for namespace in sorted(ref.namespaces):
            module = importlib.import_module(namespace)
            if any(getattr(module, held) is obj for held in getattr(module, "__all__", [])):
                holders.append(namespace)
        documenting = [ns for ns in holders if skip_member(ref, ns, "module", obj, {}) is None]
        assert documenting == [ref.homes[id(obj)].namespace], f"{name}: documented on {documenting}"
        internals = {"ignore-module-all": True}
        assert skip_member(ref, defining_module(obj), "module", obj, internals) is True, name
