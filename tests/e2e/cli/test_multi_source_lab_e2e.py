"""Two [[lab.sources]] through the real binary: override wins, warning shows, completion unions."""

import json
from pathlib import Path

import pytest

from tests._fixtures.sutrepo import make_sut_repo
from tests.e2e._otto_subprocess import run_otto

pytestmark = pytest.mark.hostless

SETTINGS = """\
[[lab.sources]]
name = "global"
backend = "json"
paths = ["{global_file}"]

[[lab.sources]]
name = "virtual"
backend = "json"
paths = ["lab"]
"""


def _host(element: str, ip: str) -> dict:
    return {
        "ip": ip,
        "element": element,
        "creds": [{"login": "u", "password": "p"}],
        "resources": [element],
        "labs": ["site"],
    }


def _make_repo(root: Path, tmp_path: Path, *, local_hosts: list[dict]) -> Path:
    global_file = tmp_path / "global-hosts.json"
    global_file.write_text(json.dumps({"hosts": [_host("orange", "10.0.0.1")]}))
    return make_sut_repo(
        root,
        name="multisrc",
        extra=SETTINGS.format(global_file=global_file),
        files={"lab/lab.json": json.dumps({"hosts": local_hosts})},
    )


def test_list_hosts_unions_sources_and_surfaces_the_override(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path / "multisrc",
        tmp_path,
        local_hosts=[_host("orange", "10.9.9.9"), _host("tomato", "10.0.0.2")],
    )
    xdir = tmp_path / "xdir"
    xdir.mkdir()

    result = run_otto(["--list-hosts"], xdir=xdir, sut_dirs=repo, lab="site")
    combined = result.stdout + result.stderr

    assert result.returncode == 0, combined
    # Union: the repo-only host appears beside the overridden global one.
    assert "orange" in combined  # global record, overridden locally
    assert "tomato" in combined  # repo-local only
    # Transparency (spec §3): the override warning reaches the user, naming
    # both source labels. If this is red because the warning is emitted
    # before logging reaches the console, FIX THE EMISSION PATH — do not
    # weaken this assertion; the warning is the override's safety story.
    assert "overrides" in combined
    assert "multisrc/virtual" in combined  # the winning source
    assert "multisrc/global" in combined  # the source it shadowed


def test_show_lab_renders_the_winning_record_not_the_shadowed_one(tmp_path: Path) -> None:
    """The override's DIRECTION, through the CLI (spec §6.1): the LATER source wins.

    The sibling above proves both sources are live and that the override is
    announced naming both labels — but every one of its assertions holds just
    as well if the merge kept the WRONG record: "overrides" still prints, and
    both host ids are still listed either way. Only a rendered field can tell
    the two directions apart.

    ``--show-lab`` is the cheapest CLI surface that shows one:
    ``--list-hosts`` prints host ids and nothing else. Its default
    ``--lab-depth 3`` already reaches ``ip=`` on each host, so no extra flag
    is needed. The losing ip must be ABSENT, not merely out-ranked — an
    override replaces the record wholesale rather than blending fields.
    """
    repo = _make_repo(
        tmp_path / "multisrc",
        tmp_path,
        local_hosts=[_host("orange", "10.9.9.9"), _host("tomato", "10.0.0.2")],
    )
    xdir = tmp_path / "xdir"
    xdir.mkdir()

    result = run_otto(["--show-lab"], xdir=xdir, sut_dirs=repo, lab="site")
    combined = result.stdout + result.stderr

    assert result.returncode == 0, combined
    assert "10.9.9.9" in combined, combined  # repo-local record took effect
    assert "10.0.0.1" not in combined, combined  # the global record it shadowed is gone


def test_list_hosts_without_a_collision_says_nothing_about_overrides(tmp_path: Path) -> None:
    """Positive control: same two sources, no colliding host id, no override talk.

    Same repo shape, same flags, same banner — only the collision is removed
    (the local source drops its ``orange`` record). Anything the sibling test
    matches on must therefore come from the override itself and not from
    startup noise that a two-source repo prints regardless.

    Not "identical records in both sources": a colliding id warns per spec
    §6.1 whether or not the two records agree, and the composite deliberately
    does not compare them (an override that changes nothing is still an
    override taking effect). Removing the collision is the control that keeps
    the product rule intact.
    """
    repo = _make_repo(tmp_path / "multisrc", tmp_path, local_hosts=[_host("tomato", "10.0.0.2")])
    xdir = tmp_path / "xdir"
    xdir.mkdir()

    result = run_otto(["--list-hosts"], xdir=xdir, sut_dirs=repo, lab="site")
    combined = result.stdout + result.stderr

    assert result.returncode == 0, combined
    assert "orange" in combined  # global source
    assert "tomato" in combined  # repo-local source
    assert "overrides" not in combined


def test_host_completion_offers_the_union_without_warning_into_the_shell(tmp_path: Path) -> None:
    """`otto -l site host <TAB>` offers both sources' hosts, silently (spec §6.3).

    Completion reads ``list_host_summaries``, not ``load_lab``: the union is
    the same, but the override warning must NOT appear — a warning printed
    into a completing shell corrupts the candidate list the shell is parsing.
    """
    repo = _make_repo(
        tmp_path / "multisrc",
        tmp_path,
        local_hosts=[_host("orange", "10.9.9.9"), _host("tomato", "10.0.0.2")],
    )
    xdir = tmp_path / "xdir"
    xdir.mkdir()

    result = run_otto(
        [],
        xdir=xdir,
        sut_dirs=repo,
        extra_env={
            "_OTTO_COMPLETE": "complete_bash",
            "COMP_WORDS": "otto -l site host ",
            "COMP_CWORD": "4",
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    names = {line.split(",", 1)[-1] for line in result.stdout.splitlines() if line}
    assert {"orange", "tomato"} <= names, names
    assert "overrides" not in result.stdout + result.stderr
