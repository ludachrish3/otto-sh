"""The subprocess helpers the docker e2e modules share.

Imported by name (the leading underscore keeps pytest from collecting this).
"""

import re
import subprocess
from pathlib import Path

from tests.e2e._otto_subprocess import CHECKOUT_ROOT, PROJECT1, run_otto

# The lab's default parent: the host every docker verb resolves to without
# `--parent`. The `unix` fixture lab has three docker-capable hosts, and test3
# is the one ranked by ``"docker_priority": 10`` in its lab.json, so it wins the
# one rule (see "Which host" in docs/cli/docker/index.md). Placeholder
# registration therefore only ever mints ``test3.<usecase>.<service>`` ids, so
# any test that addresses a container id in a SEPARATE otto process — `otto host
# <id> ...`, `otto run --on <id>` — must run against this host and no other:
# `--parent test1` registers `test1.project1.api` inside the invocation that
# deployed it, and the next process knows nothing about it. Tests that only
# drive `docker up/down/build/ps --parent <host>` keep the two-daemon pool of
# docker_host.
_DEFAULT_PARENT = "test3"

# The use-cases the sample projects declare (spec §14's "name the fragment after
# the project and container ids stay literally unchanged"): project1 declares `project1`
# and `integration`, project2 declares `project2` and `integration`. Two or more
# declared use-cases make a bare `otto docker compose up` ambiguous — it refuses,
# naming them — so every invocation in the docker e2e modules names the one it means.
_PROJECT1_USE_CASE = "project1"
_MERGED_USE_CASE = "integration"


PROJECT2 = CHECKOUT_ROOT / "tests" / "project2"

# rich sizes its tables to the terminal; a subprocess has none, so it falls
# back to 80 columns and truncates cells like `project1[core,edge]` mid-word.
# Pin a wide one so the assertions below read the values, not the ellipsis.
_WIDE = {"COLUMNS": "200"}


def _flat(text: str) -> str:
    """Collapse rich's wrapping so a rendered line can be matched as one string."""
    return " ".join(text.split())


# The three records the reservation gate and the fleet scoping print, on stdout,
# ahead of any docker verb's own output (the same ones `otto host ...` prints):
# the INFO fleet line, the WARN log record, and the bold ⚠ line `present_reservation_gate`
# prints. `-R` (every `_run_otto` call) makes the gate SKIP, so the two reservation
# records say "SKIPPED". Each is matched whole, over rich's wrapping (the log records
# wrap under a nine-column level gutter, the ⚠ line wraps unindented), so a record is
# consumed line by line until its flattened text is complete and nothing else is.
_GATE_RECORDS = (
    re.compile(
        r"INFO fleet of interest: \d+ of \d+ lab hosts \(\d+ project\(s\), \d+ excluded\)"
        r"(?:; outside it: .*)?"
    ),
    re.compile(
        r"WARN(?:ING)? Reservation check skipped for user '[^']*' on lab '[^']*'\. "
        r"Required: \[[^\]]*\]"
    ),
    re.compile(
        r"\N{WARNING SIGN} Reservation check SKIPPED for user '[^']*' on lab '[^']*'\. "
        r"Required resources: \[[^\]]*\]"
    ),
)


def _split_gate_preamble(stdout: str) -> tuple[str, str]:
    """Split *stdout* into ``(the gate's preamble, the command's own output)``.

    Removes exactly the records in ``_GATE_RECORDS`` from the START of the text, in
    whatever order they come, and nothing else: a docker verb's output is returned
    untouched, so a test keeps asserting every line of it.
    """
    lines = stdout.splitlines(keepends=True)
    consumed = 0
    pending = list(_GATE_RECORDS)
    while consumed < len(lines) and pending:
        # Blank lines between records belong to the preamble only if a record follows.
        start = consumed
        while start < len(lines) and not lines[start].strip():
            start += 1
        taken = ""
        end = start
        matched = None
        while end < len(lines) and matched is None:
            taken = _flat(taken + " " + lines[end])
            end += 1
            matched = next((r for r in pending if r.fullmatch(taken)), None)
        if matched is None:
            break
        pending.remove(matched)
        consumed = end
    return "".join(lines[:consumed]), "".join(lines[consumed:])


def _own_output(stdout: str) -> str:
    """The command's own stdout: *stdout* without the reservation gate's preamble."""
    return _split_gate_preamble(stdout)[1]


def _run_otto(
    *args: str,
    roots: str = str(PROJECT1),
    lab: str = "unix",
    xdir: Path | None = None,
    compose_suffix: str | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    """Run `otto -R --lab <lab> <args>` as a subprocess with a clean environment.

    *compose_suffix* gets baked into ``OTTO_COMPOSE_SUFFIX`` so every test
    can use a unique docker compose project name (e.g.
    ``unix-project1-<uuid>``) and never collide with concurrent runs on the
    same docker host. *env* merges last, for the few tests that need to pin
    something else about the child (``COLUMNS``, a ``pass_env`` value).

    ``OTTOPATH`` goes through ``extra_env`` rather than the runner's
    ``roots=``: the multi-project tests pass an ``os.pathsep``-joined *string*
    of two project roots, which is not a single path.
    """
    extra_env: dict[str, str] = {"OTTOPATH": roots}
    if compose_suffix is not None:
        extra_env["OTTO_COMPOSE_SUFFIX"] = compose_suffix
    if env is not None:
        extra_env.update(env)

    return run_otto(
        list(args),
        xdir=xdir,
        roots=None,
        lab=lab,
        extra_argv_prefix=["-R"],
        extra_env=extra_env,
        timeout=timeout,
    )


def staging_root_of(host: str) -> Path:
    """The private docker staging root of the lab's *host*, as otto resolves it.

    The same resolver the otto subprocess calls
    (:func:`~otto.host.docker_host.parent_staging_root`), over the host built
    from the lab data the subprocess reads, so a test names the tree otto
    staged without restating its path.
    """
    from otto.host.docker_host import parent_staging_root
    from tests._fixtures.labdata import make_host

    return parent_staging_root(make_host(host))
