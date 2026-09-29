"""Spec §6: for every generated case, the shim's answer equals bootstrapped Typer's.

Byte for byte: what ``answer_or_reason`` joins is what Typer's bash completer prints.
"""

import contextlib
import io
import itertools
import json
import os
import time
import warnings

import pytest

from otto import _shim_complete as sc
from otto import bootstrap as bs
from otto.config import completion_cache as cc
from otto.config.completion_tree import build_shim_payload
from otto.config.repo import PYTEST_CONFIG_NAMES
from tests._fixtures.shim_repo import make_shim_repo

pytestmark = pytest.mark.interpreter_agnostic

EXPECTED_HANDOVER_REASONS = {
    "live",  # a completer that must run product code (a live source)
    "list fragment past its first separator",  # `--hosts dut1,` : live past the separator
    "value given to flag",  # `--debug=x` : click aborts the whole parse of that command
    "stacked short flags",  # `-hx` : click stacks them, the shim does not model it
    "unknown option",  # only the hand-written lines; counted below
    "unknown command",  # only the hand-written lines; counted below
}
"""Every hand-over CLASS the corpus may produce, bucketed by ``_reason_class``."""

HAND_WRITTEN_UNKNOWNS = {"unknown option": 12, "unknown command": 8}
"""The hand-written unknown-name lines, counted: 4 envs x (--bogus, --la, -m) and (nope, blink).

Counted rather than prefix-allowed because an "unknown" hand-over is what a corpus
shape that never reaches Typer looks like: while this was a prefix allow-list, ~880
generated cases walked ``otto host <verb>`` with no host id -- where Typer reads the
verb as the ``host_id`` positional and every option of that verb as an unknown option
of the host group. They handed over, compared nothing, and counted as coverage.
"""

CHUNKS = 8
"""The generated corpus is compared in this many interleaved slices, one test each.

One test over the whole corpus was 150-290 s on one xdist worker under coverage —
the long pole of every hostless leg, since nothing else on that worker could start
until it finished (measured 2026-09-11; ~86 s uncontended). Interleaved
(``cases[chunk::CHUNKS]``) rather than contiguous, so every slice walks every
command group and a generator change shows up in all of them, not in whichever
slice happened to hold the affected subtree. Eight slices on four workers keeps
each under a minute and lets xdist spread them; the corpus itself is unchanged.
"""

MIN_ANSWERED_PER_CHUNK = 3000
"""A floor under the cases that actually COMPARE, per slice.

The whole generated corpus answered 29 920 of 37 628 cases when measured
(2026-09-11; the hand-written lines below are counted separately: 284 cases, 236
answered, 20 of the hand-overs the unknown-name ones). Interleaving spreads that
evenly — 3 724 to 3 752 per slice, ~10.5 s each uncontended, ~21 s under coverage
and xdist. 3 000 is loose enough that adding a command or an option cannot fail
it, tight enough that a generator change which turns real cases back into
hand-overs does.
"""


def _write_cache_like_entry(repos) -> None:
    """The writer call ``otto.cli.main.entry()`` makes (src/otto/cli/main.py:997-1032).

    The writer's two remaining keywords are deliberately absent: ``digests=`` is the
    precomputed-digest optimisation ``cache_rebuild_is_worthwhile`` fills (this helper
    does not call it, so write_cache recomputes — same entry), and ``tainted=`` is
    ``bool(result.errors)``, which the ``world`` fixture asserts empty, i.e. the default.
    """
    instructions = cc.collect_current_commands()
    backends = cc.collect_backend_names()
    cc.write_cache(
        repos,
        instructions,
        cc.collect_host_ids(repos),
        docker_hosts=cc.collect_docker_capable_host_ids(repos),
        docker_use_cases=cc.collect_docker_use_case_names(repos),
        term_backends=backends["term_backends"],
        transfer_backends=backends["transfer_backends"],
        usernames=cc.collect_reservation_usernames(repos),
        commands=cc.collect_cli_commands(),
        labs=cc.collect_lab_names(repos),
        hosts_by_lab=cc.collect_host_ids_by_lab(repos),
        host_drops=cc.collect_host_drops(repos),
        host_classes_by_id=cc.collect_host_classes_by_id(repos),
        projects=cc.collect_project_names(),
        links=cc.collect_links(repos),
        logins_by_host=cc.collect_logins_by_host(repos),
        shim=build_shim_payload(repos),
    )


def _seed_matching_tables(repos) -> None:
    """A current per-file table, as a whole-tree collection of this SUT records it.

    Both sides answer a test-name or ``-m`` TAB from it: the shim by its own
    stat pass, Typer through ``completion_view``. ``test_gen`` stands for a
    generated test, which no reading of the source could find. The registered
    markers are what pytest registers here: the SUT's declared ``slow`` and
    ``smoke``, and otto's own (``OTTO_MARKERS``), which its plugin registers
    in every session. Built by hand rather than collected: this process's
    marker registry is otto-sh's own, and importing the SUT's test files here
    would warn about the SUT's markers.
    """
    from otto.config import collected_tests as ct
    from otto.suite.markers import OTTO_MARKERS

    [repo] = repos
    tests = repo.sut_dir / "tests"

    def stat(path):
        st = path.stat()
        return [st.st_mtime_ns, st.st_size]

    def recorded(path, tests_, markers):
        return ct.FileRecord(
            stat=stat(path),
            tests=[ct.RecordedTest(classes=c, name=n) for c, n in tests_],
            markers=markers,
        )

    records = {
        str(tests / "test_shim_suite.py"): recorded(
            tests / "test_shim_suite.py",
            [(["TestShim"], "test_one"), (["TestShim"], "test_two")],
            ["slow", "smoke"],
        ),
        str(tests / "sub" / "test_nested.py"): recorded(
            tests / "sub" / "test_nested.py",
            [([], "test_deep"), ([], "test_gen")],
            ["deep", "generated"],
        ),
    }
    table = ct.updated_table(
        repo,
        None,
        ct.classify(repo, None),
        records,
        registered_markers=sorted({"slow", "smoke", *OTTO_MARKERS}),
        whole_tree=True,
        dirs={str(tests): stat(tests), str(tests / "sub"): stat(tests / "sub")},
    )
    ct.write_tables([table])
    assert ct.classify(repo, table).is_current


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Bootstrapped repo + written cache + Typer's command tree, once per test.

    The Typer side runs BOOTSTRAPPED and COLD (``set_completion_names(None)``):
    spec §1 decision 7 makes bootstrapped Typer the equality target, and the
    warm-stub path (``_OttoGroup._real`` attaching cached stubs) is explicitly
    NOT it — a warm-side difference would be a stub defect, not a shim one.
    """
    repo = make_shim_repo(tmp_path)
    monkeypatch.setenv("OTTO_SUT_DIRS", str(repo))
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OTTO_LAB", raising=False)
    bs._reset()  # the bracket tests/unit/cli/test_default_instructions.py:198-202 uses
    try:
        with warnings.catch_warnings():
            # Should anything import the SUT's test files inside THIS pytest process,
            # whose marker registry is otto-sh's own, `pytest.mark.slow`/`.smoke`
            # (registered in the SUT's pyproject exactly as a real repo registers
            # them) would warn, and this repo's `filterwarnings=["error"]` would turn
            # a foreign repo's valid marker into a BootstrapError. Scoped to that one
            # warning class around the bootstrap; nothing else is muted. The cache
            # write below never imports a test file: it reads no test file at all.
            warnings.filterwarnings("ignore", category=pytest.PytestUnknownMarkWarning)
            result = bs.bootstrap()
        assert not result.errors, result.errors
        repos = result.repos
        _write_cache_like_entry(repos)
        _seed_matching_tables(repos)

        def _no_child():
            raise AssertionError("every table is warm; the collect child must not run")

        monkeypatch.setattr(cc, "run_collect_child", _no_child)
        monkeypatch.setattr(cc, "spawn_collect_child", _no_child)
        bs.set_completion_names(None)
        import typer
        from typer._completion_classes import completion_init

        from otto.cli.main import app

        completion_init()  # registers Typer's BashComplete in typer._click.shell_completion
        yield repo, typer.main.get_command(app)
    finally:
        bs._reset()


def _typer(cli, words: str, cword: int, env: dict[str, str], monkeypatch) -> str:
    """What Typer prints for this TAB — its bash completer, from Typer's vendored registry.

    ``complete()``'s RETURN VALUE is the answer under test. Its stdout is not: click
    still runs eager-option callbacks under ``resilient_parsing`` (it only swallows
    what they RAISE — ``Parameter.handle_parse_result``), so a corpus line carrying
    ``otto test --list-markers`` makes ``list_markers_callback`` — a value-only Typer
    callback with no ``ctx`` to check — render its rich panels. That is pre-existing
    product behaviour on the Typer side, identical with or without the shim; it is
    swallowed here so it cannot be mistaken for either side's answer.
    """
    from typer._click.shell_completion import get_completion_class

    monkeypatch.delenv("OTTO_LAB", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("_OTTO_COMPLETE", "complete_bash")
    monkeypatch.setenv("COMP_WORDS", words)
    monkeypatch.setenv("COMP_CWORD", str(cword))
    completer = get_completion_class("bash")(cli, {}, "otto", "_OTTO_COMPLETE")
    with contextlib.redirect_stdout(io.StringIO()):
        return completer.complete()


def _shim(words: str, cword: int, env: dict[str, str]) -> tuple[str | None, str]:
    environ = {k: v for k, v in os.environ.items() if k != "OTTO_LAB"}
    environ.update(env, _OTTO_COMPLETE="complete_bash", COMP_WORDS=words, COMP_CWORD=str(cword))
    out = sc.answer_or_reason(environ)
    return (None if out.items is None else "\n".join(out.items)), out.reason


def _walk(node: dict, path: list[str]):
    """Every node reachable BY NAME -- never a scoped group's subcommands.

    ``otto host`` consumes its next word as the ``host_id`` positional, so a path
    like ``["host", "cleanup"]`` never reaches the verb: Typer reads ``cleanup`` as
    the id, every option of that verb as an unknown option of the host GROUP and
    every word after it as an unknown command. Such a case hands over, so it
    compares nothing. The verbs are generated under REAL ids below instead.
    """
    yield path, node
    if node.get("scoped_by"):
        return
    for name, child in node["commands"].items():
        yield from _walk(child, [*path, name])


def _node_cases(add, path: list[str], node: dict) -> None:
    """Every fragment and complete-word shape this corpus knows, for one command."""
    for frag in ("", "-", "--", "="):
        add(path, frag)
    for name in node["commands"]:
        add(path, name[:1])
    value_flags = [p["flags"][0] for p in node["params"] if p["flags"] and p["takes_value"]]
    for param in node["params"]:
        if param["flags"]:
            for flag in param["flags"]:
                add(path, flag[:3])
                # click splits the fragment on `=` and RE-READS the value part with its
                # option-name rule, whatever the flag takes
                add(path, f"{flag}=-")
                if param["takes_value"]:
                    add([*path, flag], "")
                    add([*path, flag], "t")
                    add([*path, flag], "-")
                    add(path, f"{flag}=")
                    add(path, f"{flag}=d")
                    add([*path, flag, "x"], "-")
                    # a COMPLETE `--flag=value` word with words after it: the parser
                    # attaches the value and keeps going
                    add([*path, f"{flag}=x"], "")
                    add([*path, f"{flag}=x"], "-")
                    # two value-taking flags in a row: the parser eats the second as a
                    # value, click's completer still completes it (its textual rule)
                    for other in value_flags[:2]:
                        add([*path, flag, other], "")
                        add([*path, flag, other], "-")
                else:
                    add([*path, flag], "-")
                    add(path, f"{flag}=d")
                    # the same complete word on a flag that takes NO value: click raises
                    # BadOptionUsage and resilient parsing drops the rest of the line
                    add([*path, f"{flag}=d"], "")
                    add([*path, f"{flag}=d"], "-")
        else:
            add(path, "")
            add(path, "d")
            add([*path, "dut1"], "")
            add([*path, "dut2"], "")
            add([*path, "box"], "-")


def _corpus(tree: dict, classes: dict[str, str]) -> list[tuple[str, int]]:
    """(COMP_WORDS, COMP_CWORD) per spec §6, generated from the tree itself."""
    cases: list[tuple[str, int]] = []

    def add(words: list[str], frag: str) -> None:
        line = " ".join(["otto", *words, frag])
        cases.append((line, 1 + len(words)))

    for path, node in _walk(tree, []):
        _node_cases(add, path, node)
    # The host verbs, each under a real id: the class view for an id the cache knows
    # (`dut1` unix, `dut2` shimos, `box` zephyr), the union menu for one it does not
    # (`ghost`). This is the only path on which a verb node is reachable at all.
    host = tree["commands"]["host"]
    for host_id in ("dut1", "dut2", "box", "ghost"):
        view = tree.get("host_classes", {}).get(classes.get(host_id, ""), host["commands"])
        for verb, vnode in view.items():
            add(["host", host_id], verb[:2])
            _node_cases(add, ["host", host_id, verb], vnode)
    return cases


def _reason_class(reason: str) -> str:
    """Bucket a hand-over reason by CLASS: the quoted name varies case by case."""
    if reason.startswith("live source for "):
        return "live"
    return reason.split("'", maxsplit=1)[0].strip()


HAND_WRITTEN = [
    ('otto run "bl', 2),
    ("otto run blink-all --lev", 3),
    ("otto --lab=east host ", 3),
    ("otto -least host ", 2),
    ("otto -l=east host ", 2),
    ("otto -l east -l west host ", 6),
    ("otto --la east host ", 4),
    ("otto -l east+west host d", 4),
    ("otto -l east+ host ", 4),
    ("otto --xdir --lab ", 3),
    ("otto --xdir --lab e", 3),
    ("otto --xdir --debug=x", 2),
    ("otto test ", 2),
    ("otto test te", 2),
    ("otto test test_one ", 3),
    ("otto test --seed 5 te", 4),
    ("otto test -m slow te", 4),
    ("otto test --no-random Te", 3),
    ("otto test --list-tests te", 3),
    ("otto -m ", 2),
    ("otto test -m ", 3),
    ("otto test -m", 2),
    ('otto test -m "smoke and not s', 3),
    ("otto test -m 'not (s", 3),
    ('otto test -m "smoke and s', 3),
    ('otto test -m "smoke and "', 3),
    ("otto test --markers=sl", 2),
    ("otto host dut1 exec -- -", 5),
    ("otto -- -", 2),
    ("otto -- host -", 3),
    ("otto -- host dut1 --term ", 5),
    ("otto --xdir -", 2),
    ("otto host dut1 put a b ", 6),
    ("otto host dut1 put a b /", 6),
    ("otto tunnel add --hosts ", 4),
    ("otto tunnel add --hosts dut1,", 4),
    ("otto tunnel remove ", 3),
    ("otto link ", 2),
    ("otto -l west link ", 4),
    ("otto docker ", 2),
    ("otto docker up --on ", 4),
    ("otto -l west docker up --on ", 6),
    ("otto -I ", 2),
    ("otto -I s", 2),
    ("otto --holder ", 2),
    ("otto plug ", 2),
    ("otto plug nest leaf --kind f", 5),
    ("otto plug nest leaf ", 4),
    ("otto plug nest leaf --loud ", 5),
    ("otto --debug ", 2),
    ("otto --debug -", 2),
    ("otto -x /tmp -", 3),
    ("otto --field --", 2),
    ("otto host dut2 bl", 3),
    ("otto host dut1 blink ", 4),  # a shimos verb on a unix host: unknown command
    # a value attached to a flag that takes none: click raises BadOptionUsage and
    # resilient parsing abandons the parse of that whole command
    ("otto --debug=x host ", 3),
    ("otto --field=1 host ", 3),
    ("otto -l east --debug=x host ", 5),
    ("otto --debug=x -", 2),
    ("otto test --list-tests=1 TestShim ", 4),
    # the value part of a split fragment, read by click's option-name rule
    ("otto --lab=-", 1),
    ("otto --lab=--", 1),
    ("otto --lab=-l", 1),
    ("otto -l=-", 1),
    ("otto --xdir=-", 1),
    ("otto --xdir=--l", 1),
    ("otto host box ", 3),
    ("otto host ghost ", 3),
    ("otto host dut1 --term ", 4),
    ("otto host dut1 --transfer ", 4),
    ("otto test TestShim --de", 3),
    ("otto test --de", 2),
    ("otto  host   dut1  ", 4),
    ("otto ho", 1),
    ("otto --bogus ", 2),
    ("otto nope ", 2),
    ("otto host dut1 exec --user ", 5),
    ("otto host dut1 exec --user r", 5),
    ("otto host dut1 exec --user=", 4),
    ("otto host dut1 get --user ", 5),
    ("otto host dut1 put --user ", 5),
    ("otto host dut1 login --user ", 5),
    ("otto host dut1 probe --user ", 5),
    ("otto host --term ssh dut1 exec --user ", 7),
    ("otto host --term telnet dut1 exec --user ", 7),
    ("otto host --term=ssh dut1 get --user ", 6),
    ("otto host dut2 exec --user ", 5),
    ("otto host box get --user ", 5),
    ("otto host ghost exec --user ", 5),
    ("otto -l west host dut1 exec --user ", 7),
]
ENVS = [{}, {"OTTO_LAB": "east"}, {"OTTO_LAB": "west east"}, {"OTTO_LAB": ""}]


def _generated_corpus(world) -> list[tuple[str, int]]:
    data = json.loads(cc._cache_path().read_text())
    tree = data["sections"]["shim"]["payload"]["tree"]
    classes = data["sections"]["names"]["payload"]["host_classes_by_id"]
    return _corpus(tree, classes)


def _compare(cli, cases, monkeypatch) -> tuple[int, dict[str, int]]:
    """Shim vs Typer over ``cases`` x ``ENVS``; asserts equality, returns the census.

    Returns how many cases actually COMPARED and the hand-over reasons bucketed by
    class, for the caller to hold against its own floor and expected classes.
    """
    answered = 0
    reasons: dict[str, int] = {}
    mismatches: list[str] = []
    for (words, cword), env in itertools.product(cases, ENVS):
        got, reason = _shim(words, cword, env)
        if got is None:
            key = _reason_class(reason)
            reasons[key] = reasons.get(key, 0) + 1
            continue
        expected = _typer(cli, words, cword, env, monkeypatch)
        if got != expected:
            mismatches.append(
                f"{words!r} cword={cword} env={env}\n  shim : {got!r}\n  typer: {expected!r}"
            )
        answered += 1
    assert not mismatches, "\n".join(mismatches[:40]) + f"\n… {len(mismatches)} mismatches"
    return answered, reasons


# Every ANSWERED case bootstraps Typer's completer, ~10 s per slice uncontended.
# Under coverage + xdist on a shared box that is one contention factor from
# the 180 s default, so the slices carry their own ceiling rather than a trim.
@pytest.mark.timeout(600)
@pytest.mark.parametrize("chunk", range(CHUNKS))
def test_shim_equals_typer_over_the_generated_corpus(world, monkeypatch, chunk):
    _repo, cli = world
    cases = _generated_corpus(world)[chunk::CHUNKS]
    answered, reasons = _compare(cli, cases, monkeypatch)
    assert answered >= MIN_ANSWERED_PER_CHUNK, (answered, reasons)
    # Counted, not prefix-allowed: every hand-over falls in a named class, and the two
    # classes a corpus shape that never reaches Typer lands in are counted exactly, so
    # such a shape cannot creep back in as coverage. The generated corpus produces
    # NONE of them — the unknown-name hand-overs are the hand-written lines' alone.
    assert set(reasons) <= EXPECTED_HANDOVER_REASONS, reasons
    unknowns = {r: n for r, n in reasons.items() if r.startswith("unknown ")}
    assert unknowns == {}, reasons


def test_shim_equals_typer_over_the_hand_written_lines(world, monkeypatch):
    _repo, cli = world
    _answered, reasons = _compare(cli, HAND_WRITTEN, monkeypatch)
    assert set(reasons) <= EXPECTED_HANDOVER_REASONS, reasons
    unknowns = {r: n for r, n in reasons.items() if r.startswith("unknown ")}
    assert unknowns == HAND_WRITTEN_UNKNOWNS, reasons


def test_the_merged_test_flags_complete_from_the_shim(world, monkeypatch):
    """``otto test --de<TAB>`` offers ``--depth``, a flag the repo registered for ``test``.

    Pins that the differential is not vacuous for the verb-wide flags: both
    sides answer, and the answer holds the registered flag, not just whatever
    the two happen to agree on.
    """
    _repo, cli = world
    got, reason = _shim("otto test --de", 2, {})
    assert got is not None, reason
    assert "--depth" in got.split("\n")
    assert got == _typer(cli, "otto test --de", 2, {}, monkeypatch)


# ── a tests site answers from the per-file table (design 2026-09-27 §9.2) ──────

TESTS_SITE_LINES = [
    ("otto test ", 2),
    ("otto test Te", 2),
    ("otto test test_one ", 3),
    ("otto test -m ", 3),
    ('otto test -m "smoke and s', 3),
]
"""Test-name and ``-m`` sites: the two the table serves."""


def _outcome(words: str, cword: int) -> "sc.Outcome":
    environ = {k: v for k, v in os.environ.items() if k != "OTTO_LAB"}
    environ.update(_OTTO_COMPLETE="complete_bash", COMP_WORDS=words, COMP_CWORD=str(cword))
    return sc.answer_or_reason(environ)


def _bump(path, seconds: int = 5) -> None:
    """Move *path*'s mtime forward, as the seconds between two real edits would."""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + seconds * 1_000_000_000))


def _answers_like_typer(cli, monkeypatch) -> list["sc.Outcome"]:
    outcomes = []
    for words, cword in TESTS_SITE_LINES:
        out = _outcome(words, cword)
        assert out.items is not None, (words, out.reason)
        assert "\n".join(out.items) == _typer(cli, words, cword, {}, monkeypatch), words
        outcomes.append(out)
    return outcomes


def _check_marker():
    return cc._cache_path().parent / sc.MARKER_FILENAMES["tests"]


def _age_the_check(seconds: float) -> None:
    """Date the last check (the check marker) *seconds* ago."""
    then = time.time() - seconds
    os.utime(_check_marker(), (then, then))


def test_a_current_table_answers_every_tests_site_and_asks_for_no_check(world, monkeypatch):
    """A table written inside the check window: answered, and no background check."""
    _repo, cli = world
    outcomes = _answers_like_typer(cli, monkeypatch)
    assert not any(out.refresh for out in outcomes)
    names = _outcome("otto test ", 2).items
    assert {"TestShim", "TestShim::test_one", "test_deep", "test_gen"} <= set(names or [])


def test_the_tab_answers_last_known_names_and_never_stats_the_test_tree(world, monkeypatch):
    """Edited, added and deleted files do not change a TAB's answer; the check does.

    The TAB reads the table and the ``env`` (the pytest configs, settings and
    site-packages: a handful of stats, whatever the corpus size) and nothing
    the table tracks, so a deleted file's names are offered until the
    background check drops its record, and a new file's names are not
    offered until a collection has seen them. Typer's completer (zsh, fish,
    a handed-over bash TAB) answers the same, the same way.
    """
    repo, cli = world
    monkeypatch.setattr(cc, "_collect_refresh_requested", False)
    before = _outcome("otto test ", 2).items
    suite = repo / "tests" / "test_shim_suite.py"
    suite.write_text(suite.read_text() + "\n\ndef test_three():\n    pass\n")
    (repo / "tests" / "sub" / "test_nested.py").unlink()
    (repo / "tests" / "test_added.py").write_text("def test_added():\n    pass\n")

    stats: list[str] = []
    real = os.stat

    def spy(path, *args, **kwargs):
        stats.append(str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(sc.os, "stat", spy)
    _check_marker().unlink()  # whatever the check window says
    after = _outcome("otto test ", 2)
    assert after.items == before
    assert after.refresh is not None, "with no check recorded, the child checks"
    assert "\n".join(after.items) == _typer(cli, "otto test ", 2, {}, monkeypatch)
    assert cc._collect_refresh_requested, "and so does Typer's"
    tests_dir = str(repo / "tests")
    assert not [p for p in stats if p == tests_dir or p.startswith(tests_dir + os.sep)]


def test_writing_the_table_starts_the_check_window(world):
    """Whoever writes the table has just classified it (a run, a listing, the child),
    so a TAB right after it starts no check."""
    assert _check_marker().is_file()
    assert _outcome("otto test ", 2).refresh is None


@pytest.mark.parametrize("lapse", ["aged", "missing"])
def test_a_lapsed_check_window_starts_one_check_and_restarts_the_window(world, monkeypatch, lapse):
    """Past ``CHECK_WINDOW_SECONDS`` since the table was last checked (or with no
    record of a check), the answer asks for the collect child; starting it touches
    the check marker, so the TABs after it ask for nothing until the window lapses
    again."""
    started: list[list[str]] = []

    class _Popen:
        def __init__(self, argv, **_kwargs):
            started.append(argv)

    monkeypatch.setattr("subprocess.Popen", _Popen)
    monkeypatch.setattr(sc, "_started", [])

    _age_the_check(sc.CHECK_WINDOW_SECONDS - 30)
    assert _outcome("otto test ", 2).refresh is None, "inside the window"

    if lapse == "aged":
        _age_the_check(sc.CHECK_WINDOW_SECONDS + 1)
    else:
        _check_marker().unlink()
    due = _outcome("otto test ", 2)
    assert due.items
    assert due.refresh == str(cc._cache_path().parent)
    assert sc.spawn_refresh(due.refresh, dict(os.environ)) is True
    assert len(started) == 1
    assert time.time() - _check_marker().stat().st_mtime < 5, "the window restarts"

    for words, cword in TESTS_SITE_LINES:
        assert _outcome(words, cword).refresh is None, words
    assert len(started) == 1


def _rewrite_table(mutate) -> None:
    data = json.loads(cc._cache_path().read_text())
    [key] = data[cc.COLLECTED_TESTS_KEY]
    mutate(data[cc.COLLECTED_TESTS_KEY], key)
    cc._cache_path().write_text(json.dumps(data))


def _rewrite_env(field: str, value: str) -> None:
    _rewrite_table(lambda ns, key: ns[key]["env"].__setitem__(field, value))


def _rewrite_site_packages() -> None:
    def move(ns, key):
        sites = ns[key]["env"]["site_packages"]
        assert sites, "the table records where pytest and otto are installed"
        for site in sites:
            sites[site] = [0, 0]

    _rewrite_table(move)


@pytest.mark.parametrize(
    ("cold", "reason"),
    [
        (lambda repo: _rewrite_table(lambda ns, key: ns.pop(key)), "no test-names cache for "),
        (
            lambda repo: _rewrite_table(lambda ns, key: ns[key].__setitem__("generated_at", 0)),
            "test-names cache expired",
        ),
        (lambda repo: _bump(repo / "pyproject.toml"), "test-names cache env moved: "),
        (
            lambda repo: (repo / "pytest.ini").write_text("[pytest]\n"),
            "test-names cache env moved: ",
        ),
        (lambda repo: _rewrite_env("python", "0.0.0"), "test-names cache env moved: python"),
        (
            lambda repo: _rewrite_env("prefix", "/another/venv"),
            "test-names cache env moved: prefix",
        ),
        (lambda repo: _rewrite_site_packages(), "test-names cache env moved: "),
    ],
    ids=[
        "missing",
        "past-its-ttl",
        "config-edited",
        "config-added",
        "another-python",
        "another-venv",
        "site-packages-moved",
    ],
)
def test_a_cold_table_hands_over_stale_on_tests_sites_only(world, monkeypatch, cold, reason):
    """No table, one past its TTL, or one another interpreter, venv, installation or pytest
    config wrote: the full path seeds."""
    repo, cli = world
    cold(repo)
    for words, cword in TESTS_SITE_LINES:
        out = _outcome(words, cword)
        assert out.items is None, words
        assert out.reason.startswith(reason), out.reason
        assert out.stale is True
        assert out.refresh is None
    got, _ = _shim("otto host ", 2, {})
    assert got == _typer(cli, "otto host ", 2, {}, monkeypatch), "a names site is untouched"


def test_pytest_config_names_are_pytests_own():
    """Pinned literally: pytest keeps the list in a local of ``locate_config``
    (``_pytest.config.findpaths``), which no API exposes. Re-read it there when
    pytest is upgraded; a name missing here is a config edit no table sees."""
    from otto.config.repo import PYTEST_CONFIG_NAMES

    assert PYTEST_CONFIG_NAMES == (
        "pytest.toml",
        ".pytest.toml",
        "pytest.ini",
        ".pytest.ini",
        "pyproject.toml",
        "tox.ini",
        "setup.cfg",
    )


@pytest.mark.parametrize("name", PYTEST_CONFIG_NAMES)
def test_every_pytest_config_name_sends_the_table_back_to_a_whole_tree(world, name):
    """Adding (or, for one the SUT already has, editing) any of pytest's config
    files makes ``classify`` want a whole-tree collection and the shim hand a
    tests site over as stale: either may change what pytest collects."""
    from otto.config import collected_tests as ct
    from otto.config.repo import Repo

    repo, _cli = world
    path = repo / name
    if path.exists():
        _bump(path)
    else:
        path.write_text("")
    live = Repo(sut_dir=repo)
    assert ct.classify(live, ct.read_table(live)).whole_tree, name
    out = _outcome("otto test ", 2)
    assert out.items is None, name
    assert out.reason == f"test-names cache env moved: {path}", out.reason
    assert out.stale is True
