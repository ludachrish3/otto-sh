"""Spec §6: for every generated case, the shim's answer equals bootstrapped Typer's.

Byte for byte: what ``answer_or_reason`` joins is what Typer's bash completer prints.
"""

import contextlib
import io
import itertools
import json
import os
import warnings

import pytest

from otto import _shim_complete as sc
from otto import bootstrap as bs
from otto.config import completion_cache as cc
from otto.config.completion_tree import build_shim_payload
from tests._fixtures.shim_repo import make_shim_repo

EXPECTED_HANDOVER_REASONS = {
    "live",  # a completer that must run product code (a live source)
    "list fragment past its first separator",  # `--hosts dut1,` : live past the separator
    "collected set cold",  # a tests site with no collected set in the cache
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

MIN_ANSWERED = 25000
"""A floor under the cases that actually COMPARE, measured at 29 472 of 36 824.

Loose enough that adding a command or an option cannot fail it, tight enough that a
generator change which turns real cases back into hand-overs does.
"""


def _write_cache_like_entry(repos) -> None:
    """The writer call ``otto.cli.main.entry()`` makes (src/otto/cli/main.py:997-1032).

    The writer's two remaining keywords are deliberately absent: ``digests=`` is the
    precomputed-digest optimisation ``cache_rebuild_is_worthwhile`` fills (this helper
    does not call it, so write_cache recomputes — same entry), and ``tainted=`` is
    ``bool(result.errors)``, which the ``world`` fixture asserts empty, i.e. the default.
    """
    instructions, suites = cc.collect_current_commands()
    backends = cc.collect_backend_names()
    scan = cc.scan_test_corpus(repos)
    cc.write_cache(
        repos,
        instructions,
        suites,
        cc.collect_host_ids(repos),
        docker_hosts=cc.collect_docker_capable_host_ids(repos),
        docker_use_cases=cc.collect_docker_use_case_names(repos),
        term_backends=backends["term_backends"],
        transfer_backends=backends["transfer_backends"],
        usernames=cc.collect_reservation_usernames(repos),
        commands=cc.collect_cli_commands(),
        labs=cc.collect_lab_names(repos),
        tests=scan.names,
        markers=cc.collect_marker_names(repos, scan=scan),
        hosts_by_lab=cc.collect_host_ids_by_lab(repos),
        host_drops=cc.collect_host_drops(repos),
        host_classes_by_id=cc.collect_host_classes_by_id(repos),
        projects=cc.collect_project_names(),
        links=cc.collect_links(repos),
        shim=build_shim_payload(repos),
    )


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
            # bootstrap() IMPORTS the SUT's top-level test files (Repo.import_test_file)
            # inside THIS pytest process, whose marker registry is otto-sh's own — so
            # `pytest.mark.slow`/`.smoke`, registered in the SUT's pyproject exactly as a
            # real repo registers them, warn here and this repo's `filterwarnings=["error"]`
            # would turn a foreign repo's perfectly valid marker into a BootstrapError.
            # Scoped to that one warning class around that one call; nothing else is muted.
            warnings.filterwarnings("ignore", category=pytest.PytestUnknownMarkWarning)
            result = bs.bootstrap()
        assert not result.errors, result.errors
        repos = result.repos
        _write_cache_like_entry(repos)
        cc._record_collected_tests(
            repos,
            ["test_one", "test_two", "TestShim::test_one", "test_deep", "test_gen"],
            markers=["smoke", "slow", "deep", "generated"],
        )

        def _no_warm(_repos):
            raise AssertionError("the collected set is warm; the warmer must not run")

        monkeypatch.setattr(cc, "maybe_warm_collected_tests", _no_warm)
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
    ("otto test --tests test_one,", 3),
    ("otto test --tests test_one,te", 3),
    ("otto -m ", 2),
    ("otto test -m ", 3),
    ("otto test -m", 2),
    ('otto test -m "smoke and not s', 3),
    ("otto test -m 'not (s", 3),
    ('otto test -m "smoke and s', 3),
    ('otto test -m "smoke and "', 3),
    ("otto test --tests=te", 2),
    ("otto test --markers=sl", 2),
    ("otto host dut1 run -- -", 5),
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
    ("otto --as-user ", 2),
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
    ("otto  host   dut1  ", 4),
    ("otto ho", 1),
    ("otto --bogus ", 2),
    ("otto nope ", 2),
]
ENVS = [{}, {"OTTO_LAB": "east"}, {"OTTO_LAB": "west east"}, {"OTTO_LAB": ""}]


# ~86 s uncontended: every ANSWERED case bootstraps Typer's completer. Under
# coverage + xdist on a shared box that is one contention factor from the
# 180 s default, so the corpus carries its own ceiling rather than a trim.
@pytest.mark.timeout(600)
def test_shim_equals_typer_over_the_generated_corpus(world, monkeypatch):
    _repo, cli = world
    data = json.loads(cc._cache_path().read_text())
    tree = data["sections"]["shim"]["payload"]["tree"]
    classes = data["sections"]["names"]["payload"]["host_classes_by_id"]
    cases = [*_corpus(tree, classes), *HAND_WRITTEN]
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
    assert answered >= MIN_ANSWERED, (answered, reasons)
    # Counted, not prefix-allowed: every hand-over falls in a named class, and the two
    # classes a corpus shape that never reaches Typer lands in are counted exactly, so
    # such a shape cannot creep back in as coverage.
    assert set(reasons) <= EXPECTED_HANDOVER_REASONS, reasons
    unknowns = {r: n for r, n in reasons.items() if r.startswith("unknown ")}
    assert unknowns == HAND_WRITTEN_UNKNOWNS, reasons


def test_a_cold_collected_set_hands_over_on_tests_sites_only(world, monkeypatch):
    _repo, cli = world
    data = json.loads(cc._cache_path().read_text())
    data.pop(cc.COLLECTED_TESTS_KEY, None)
    cc._cache_path().write_text(json.dumps(data))
    assert _shim("otto test --tests ", 3, {})[1] == "collected set cold"
    assert _shim("otto test -m ", 3, {})[1] == "collected set cold"
    got, _ = _shim("otto host ", 2, {})
    assert got == _typer(cli, "otto host ", 2, {}, monkeypatch)
