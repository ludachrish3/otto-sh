"""``entry``'s cache write stores what the cache's sections collect.

``otto.cli.main.entry`` is the one writer: it rebuilds the cache on root
``--help`` through ``write_cache``. The ``names`` and ``shim`` sections in
``otto.config.cache_sections`` collect the same payloads, and are its parity
reference: nothing in ``src`` calls ``Section.collect``. Both take the CLI's
walkers, its root app and its dispatch key from
``otto.cli.main._completion_collectors``.

This test hands ``entry`` collectors that are all marked: walkers that return
marked values, a toy root app, and a dispatch key that app answers to only
when it is the one handed in. It lets ``entry`` write the cache in a child
process (the way a user's ``otto --help`` does) and requires the stored
``names`` and ``shim`` payloads to equal what the sections collect from the
same collectors, per section key. A writer that bypasses the collectors, or
passes the real app or key, stores something else and fails here. So does one
that passes its own repo list: of the two repos, the second's required
dependency is missing, so the dependency-ordered list (``bootstrap()``'s
``ordered_repos``) leaves it out while discovery, the list the sections read,
keeps it.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._fixtures.generated_repo import generate_repo
from tests._fixtures.sutrepo import make_sut_repo

PROBE = """
import json, sys

import typer
from typer.core import TyperGroup

import otto.cli.main as main
from otto.config.cache_sections import CliCollectors

real = main._completion_collectors
KEY = "zz-parity-key"


# Lists one child, and resolves it only under the marked dispatch key.
class KeyedGroup(TyperGroup):
    def list_commands(self, ctx):
        return ["zz-parity-app"]

    def get_command(self, ctx, name):
        if ctx.meta.get(KEY) == [name] == ["zz-parity-app"]:
            return TyperGroup(name=name)
        return None


toy = typer.Typer(cls=KeyedGroup)


@toy.callback()
def root():
    pass


def marked():
    cli = real()
    return CliCollectors(
        instructions=lambda: [*cli.instructions(), {"name": "zz-parity-instruction"}],
        test_options=lambda: [*cli.test_options(), {"name": "--zz-parity-option"}],
        commands=lambda: [*cli.commands(), {"name": "zz-parity-command"}],
        app=toy,
        pending_key=KEY,
    )


main._completion_collectors = marked
sys.argv = ["otto", "--help"]
try:
    main.entry()
except SystemExit:
    pass

from otto.config.repo import discover
from otto.config.cache_sections import section_by_name

repos = discover().repos
cli = marked()
print(json.dumps({
    "names": section_by_name("names").collect(repos, cli),
    "shim": section_by_name("shim").collect(repos, cli),
}, sort_keys=True))
"""


SKIPPED = "zz-parity-skipped"


@pytest.fixture
def repo_env(tmp_path):
    repo = generate_repo(tmp_path, files=4, dirs=1)
    skipped = make_sut_repo(
        tmp_path / "skipped",
        name=SKIPPED,
        extra='[dependencies]\nrequired = ["zz-parity-missing"]\n',
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTTO_")}
    env["OTTO_SUT_DIRS"] = os.pathsep.join([str(repo), str(skipped)])
    env["OTTO_HOME"] = str(tmp_path / "home")
    return env


def test_entry_stores_what_the_sections_collect(repo_env):
    p = subprocess.run(
        [sys.executable, "-c", PROBE],
        env=repo_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert p.returncode == 0, p.stderr
    collected = json.loads(p.stdout.strip().splitlines()[-1])
    caches = list(Path(repo_env["OTTO_HOME"]).rglob("completion_cache.json"))
    assert len(caches) == 1, caches
    stored = json.loads(caches[0].read_text())["sections"]
    names, shim = stored["names"]["payload"], stored["shim"]["payload"]
    assert {"name": "zz-parity-instruction"} in names["instructions"]
    assert {"name": "--zz-parity-option"} in names["test_options"]
    assert {"name": "zz-parity-command"} in names["commands"]
    assert list(shim["tree"]["commands"]) == ["zz-parity-app"]  # the toy app, under its key
    assert len(shim["tables"]) == 2  # the dependency-skipped repo too
    for section, payload in [["names", names], ["shim", shim]]:
        assert sorted(payload) == sorted(collected[section]), section
        for key in payload:
            assert payload[key] == collected[section][key], f"{section}.{key}"
