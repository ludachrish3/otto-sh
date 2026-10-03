"""The subprocess helpers the docker e2e modules share.

Imported by name (the leading underscore keeps pytest from collecting this).
"""

import subprocess
from pathlib import Path

from tests.e2e._otto_subprocess import PROJECT_ROOT, REPO1, run_otto

# The one host repo1's fragments RESOLVE to without `--on`. Every
# ``[[docker.use_cases]]`` fragment of the sample repos declares
# ``role = "docker"``, and test3 is the only element in the `unix` fixture lab
# tagged ``"roles": ["docker"]`` (spec §5 knob 3). Placeholder registration
# therefore only ever mints ``test3.<usecase>.<service>`` ids, so any test that
# addresses a container id in a SEPARATE otto process — `otto host <id> ...`,
# `otto run --on <id>` — must run against this host and no other: `--on test1`
# registers `test1.repo1.api` inside the invocation that deployed it, and the
# next process knows nothing about it. Tests that only drive `docker up/down/
# build/ps --on <host>` keep the two-daemon pool of docker_host.
_ROLE_DOCKER_HOST = "test3"

# The use-cases the sample repos declare (spec §14's "name the fragment after
# the repo and container ids stay literally unchanged"): repo1 declares `repo1`
# and `integration`, repo2 declares `repo2` and `integration`. Two or more
# declared use-cases make a bare `otto docker compose up` ambiguous — it refuses,
# naming them — so every invocation in the docker e2e modules names the one it means.
_REPO1_USE_CASE = "repo1"
_MERGED_USE_CASE = "integration"


REPO2 = PROJECT_ROOT / "tests" / "repo2"

# rich sizes its tables to the terminal; a subprocess has none, so it falls
# back to 80 columns and truncates cells like `repo1[core,edge]` mid-word.
# Pin a wide one so the assertions below read the values, not the ellipsis.
_WIDE = {"COLUMNS": "200"}


def _flat(text: str) -> str:
    """Collapse rich's wrapping so a rendered line can be matched as one string."""
    return " ".join(text.split())


def _run_otto(
    *args: str,
    sut_dirs: str = str(REPO1),
    lab: str = "unix",
    xdir: Path | None = None,
    compose_suffix: str | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    """Run `otto -R --lab <lab> <args>` as a subprocess with a clean environment.

    *compose_suffix* gets baked into ``OTTO_COMPOSE_SUFFIX`` so every test
    can use a unique docker compose project name (e.g.
    ``unix-repo1-<uuid>``) and never collide with concurrent runs on the
    same docker host. *env* merges last, for the few tests that need to pin
    something else about the child (``COLUMNS``, a ``pass_env`` value).

    ``OTTO_SUT_DIRS`` goes through ``extra_env`` rather than the runner's
    ``sut_dirs=``: the multi-repo tests pass an ``os.pathsep``-joined *string*
    of two repo roots, which is not a single path.
    """
    extra_env: dict[str, str] = {"OTTO_SUT_DIRS": sut_dirs}
    if compose_suffix is not None:
        extra_env["OTTO_COMPOSE_SUFFIX"] = compose_suffix
    if env is not None:
        extra_env.update(env)

    return run_otto(
        list(args),
        xdir=xdir,
        sut_dirs=None,
        lab=lab,
        extra_argv_prefix=["-R"],
        extra_env=extra_env,
        timeout=timeout,
    )
